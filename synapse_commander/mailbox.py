"""Append-only synapse-mailbox/0 transport; the engine remains authoritative."""
from __future__ import annotations
from collections.abc import Mapping
import json
from pathlib import Path
import time
from typing import Any
from .protocol import IntentEnvelope, IntentReceipt

MAX_LINE = 4096

class MailboxTransport:
    def __init__(self, write_dir: Path, *, run_id: str, team: str | int, ledger: Any | None = None) -> None:
        self.team, self.ledger = str(team), ledger
        self.dir = Path(write_dir) / "synapse" / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.inbox, self.outbox = self.dir / "inbox.jsonl", self.dir / "outbox.jsonl"
        self.inbox.touch(); self.outbox.touch(); self._offset = 0; self._seq = 0; self._events: list[dict[str, Any]] = []; self._receipts: dict[str, dict[str, Any]] = {}; self._observation: dict[str, Any] | None = None; self._hello: dict[str, Any] | None = None
    def _read(self) -> None:
        with self.outbox.open("rb") as file:
            file.seek(self._offset)
            for raw in file:
                self._offset += len(raw)
                if len(raw) > 32768: continue
                try: event = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError): continue
                if not isinstance(event, dict): continue
                event["wall_ns"] = time.monotonic_ns(); self._events.append(event)
                body = event.get("body", {})
                if event.get("kind") == "hello": self._hello = body
                elif event.get("kind") == "observation" and str(body.get("team")) == self.team: self._observation = body
                elif event.get("kind") == "receipt": self._receipts[str(body.get("intent_id"))] = body
    def hello(self) -> dict[str, Any] | None: self._read(); return self._hello
    def current_frame(self) -> int: self._read(); return int(self._events[-1].get("frame", 0)) if self._events else 0
    def observation_for(self, team: str) -> Mapping[str, object]:
        self._read()
        if str(team) != self.team or self._observation is None: raise KeyError(team)
        return self._observation
    def receipt(self, intent_id: str) -> dict[str, Any] | None: self._read(); return self._receipts.get(intent_id)
    def events_since(self, seq: int, limit: int = 256) -> list[dict[str, Any]]: self._read(); return [e for e in self._events if int(e.get("seq", 0)) > seq][:limit]
    def submit_intent(self, envelope: IntentEnvelope, wait_s: float = 1.0) -> IntentReceipt:
        self._seq += 1; record = {"seq": self._seq, "kind": "intent", "envelope": envelope.to_wire()}; raw = json.dumps(record, separators=(",", ":")).encode() + b"\n"
        if len(raw) > MAX_LINE: return IntentReceipt(envelope.intent_id, False, self.current_frame(), "mailbox", "too_large")
        with self.inbox.open("ab") as file: file.write(raw); file.flush()
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            receipt = self.receipt(envelope.intent_id)
            if receipt: return IntentReceipt(envelope.intent_id, receipt.get("status") == "accepted", int(receipt.get("frame") or 0), "engine-" + envelope.intent_id, receipt.get("reason"))
            time.sleep(.01)
        return IntentReceipt(envelope.intent_id, False, self.current_frame(), "mailbox", "pending")
    def close(self) -> None:
        self._seq += 1
        with self.inbox.open("ab") as file: file.write(json.dumps({"seq": self._seq, "kind": "control", "op": "quit"}).encode() + b"\n")
