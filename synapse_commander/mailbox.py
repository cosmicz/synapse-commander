"""Append-only synapse-mailbox/0 transport; the engine remains authoritative."""
from __future__ import annotations
from collections.abc import Mapping
import json
from pathlib import Path
from threading import RLock
import time
import re
from typing import Any
from .protocol import IntentEnvelope, IntentReceipt

MAX_LINE = 4096
MAX_OUTBOX_LINE = 32768
MAX_READ_BATCH = 256
OUTBOX_KINDS = frozenset({"hello", "observation", "receipt", "effect", "scenario", "bridge_error", "bye"})


class MailboxReadError(ValueError):
    """A damaged engine stream cannot be silently used as complete evidence."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _reject_constant(_value: str) -> None:
    raise MailboxReadError("invalid_json")


def _decode_event(raw: bytes) -> dict[str, Any]:
    try:
        event = json.loads(raw, parse_constant=_reject_constant)
        # Reject escaped lone surrogates as well as non-finite numbers before
        # admitting a record that a UTF-8 ledger cannot preserve.
        json.dumps(event, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise MailboxReadError("invalid_json") from exc
    if not isinstance(event, dict) or not isinstance(event.get("body"), dict):
        raise MailboxReadError("invalid_event")
    if not isinstance(event.get("kind"), str) or event["kind"] not in OUTBOX_KINDS:
        raise MailboxReadError("invalid_event_kind")
    for field in ("seq", "frame"):
        if type(event.get(field)) is not int or event[field] < (1 if field == "seq" else 0):
            raise MailboxReadError("invalid_" + field)
    if event["kind"] == "receipt":
        intent_id = event["body"].get("intent_id")
        if not isinstance(intent_id, str) or not intent_id:
            raise MailboxReadError("invalid_receipt")
    return event

class MailboxTransport:
    def __init__(self, mailbox_dir: Path, *, run_id: str, team: str | int = 0, ledger: Any | None = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id): raise ValueError("invalid run_id")
        self.run_id = run_id
        self.team, self.ledger = str(team), ledger
        self.dir = Path(mailbox_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.inbox, self.outbox = self.dir / "inbox.jsonl", self.dir / "outbox.jsonl"
        self.inbox.touch(); self.outbox.touch(); self._offset = 0; self._seq = 0; self._events: list[dict[str, Any]] = []; self._receipts: dict[str, dict[str, Any]] = {}; self._observation: dict[str, Any] | None = None; self._hello: dict[str, Any] | None = None
        self._read_lock = RLock()
        self._submit_lock = RLock()
        self._receipt_seq: dict[str, int] = {}
        self._pending_receipts: dict[str, int] = {}
    def _read(self) -> None:
        # One writer may be midway through a JSONL write while several HTTP
        # readers poll. Advance the cursor only after a whole validated line.
        with self._read_lock, self.outbox.open("rb") as file:
            if file.seek(0, 2) < self._offset:
                raise MailboxReadError("outbox_truncated")
            file.seek(self._offset)
            for _ in range(MAX_READ_BATCH):
                raw = file.readline(MAX_OUTBOX_LINE + 1)
                if len(raw) > MAX_OUTBOX_LINE:
                    raise MailboxReadError("outbox_record_too_large")
                if not raw.endswith(b"\n"):
                    return  # Includes EOF and an incomplete final record.
                event = _decode_event(raw)
                previous = self._events[-1] if self._events else {"seq": 0, "frame": 0}
                if event["seq"] != previous["seq"] + 1:
                    raise MailboxReadError("outbox_sequence_gap")
                if event["frame"] < previous["frame"]:
                    raise MailboxReadError("outbox_frame_backwards")
                event["wall_ns"] = time.monotonic_ns()
                body = event["body"]
                self._events.append(event)
                self._offset += len(raw)
                if event["kind"] == "hello" and body.get("run_id") == self.run_id:
                    self._hello = body
                elif event["kind"] == "observation" and str(body.get("team")) == self.team:
                    self._observation = body
                elif event["kind"] == "receipt":
                    self._receipts[body["intent_id"]] = body
                    self._receipt_seq[body["intent_id"]] = event["seq"]
    def hello(self) -> dict[str, Any] | None: self._read(); return self._hello
    def current_frame(self) -> int: self._read(); return int(self._events[-1].get("frame", 0)) if self._events else 0
    def observation_for(self, team: str) -> Mapping[str, object]:
        self._read()
        if str(team) != self.team or self._observation is None: raise KeyError(team)
        return self._observation
    def receipt(self, intent_id: str) -> dict[str, Any] | None: self._read(); return self._receipts.get(intent_id)
    def events_since(self, seq: int, limit: int = 256) -> list[dict[str, Any]]: self._read(); return [e for e in self._events if int(e.get("seq", 0)) > seq][:limit]
    def submit_intent(self, envelope: IntentEnvelope, wait_s: float = 1.0) -> IntentReceipt:
        # Engine receipts identify semantic intents, not individual attempts.
        # Serialize writes and do not send a retry while an older attempt has
        # no receipt: its delayed answer would otherwise be ambiguous.
        with self._submit_lock:
            intent_id = envelope.intent_id
            self._read()
            with self._read_lock:
                previous = self._receipt_seq.get(intent_id, 0)
                pending = self._pending_receipts.get(intent_id)
                if pending is not None and previous <= pending:
                    raise TimeoutError("previous engine receipt is still pending")
            wire = envelope.to_wire()
            wire["team"] = int(envelope.team)
            if envelope.type == "TASK":
                wire["payload"] = {"unit_id": int(envelope.payload["unit"]),
                                   "verb": envelope.payload["verb"], "args": envelope.payload["args"]}
            record = {"seq": self._seq + 1, "kind": "intent", "envelope": wire}
            raw = json.dumps(record, separators=(",", ":")).encode() + b"\n"
            if len(raw) > MAX_LINE:
                return IntentReceipt(intent_id, False, self.current_frame(), "mailbox", "too_large")
            self._seq += 1
            self._pending_receipts[intent_id] = previous
            with self.inbox.open("ab") as file:
                file.write(raw)
                file.flush()
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                self._read()
                with self._read_lock:
                    if self._receipt_seq.get(intent_id, 0) > previous:
                        receipt = self._receipts[intent_id]
                        del self._pending_receipts[intent_id]
                        return IntentReceipt(intent_id, receipt.get("status") == "accepted",
                                             int(receipt.get("frame") or 0), "engine-" + intent_id,
                                             receipt.get("reason"))
                time.sleep(.01)
            raise TimeoutError("engine receipt is pending")
    def close(self) -> None:
        with self._submit_lock:
            self._seq += 1
            with self.inbox.open("ab") as file:
                file.write(json.dumps({"seq": self._seq, "kind": "control", "op": "quit"}).encode() + b"\n")
