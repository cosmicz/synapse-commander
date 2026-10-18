"""Run manifest, append-only JSONL ledger and replay verification.

The recorder writes one JSON object per line.  The first record is the
pre-run ``MANIFEST``; exactly one ``RUN_START`` follows it; the last record
is a recorder-written ``RUN_END`` trailer carrying the record count and the
hash-chain head.  Every record carries the previous record's digest, so a
flipped byte, deleted, reordered or appended line, or a truncated tail is
detected by :func:`verify_ledger`.

Integrity is tamper-evident against accidental corruption and truncation,
not against deliberate forgery: there is no signing key, because credentials
never enter the ledger.  A rewritten file with recomputed digests is caught
only by the semantic checks (ordering, causal links, classification rules).

Classification is fail-closed.  A ledger that is not complete and valid is
not classified at all.  Only an affirmative, linked chain of live inference
for every caller-declared required decision yields ``agent_only``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

LEDGER_SCHEMA = "synapse.ledger/v1"
MANIFEST_SCHEMA = "synapse.manifest/v1"
GENESIS = "0" * 64
MAX_RECORD_BYTES = 256 * 1024
MAX_PAYLOAD_DEPTH = 32
MAX_COUNTER = 2 ** 63 - 1  # bound for seq, sim_frame and wall_ns

# Record kinds.  MANIFEST, RUN_START and RUN_END are written only by the
# recorder; all others arrive through Recorder.record().
MANIFEST = "MANIFEST"
RUN_START = "RUN_START"
RUN_END = "RUN_END"
OBSERVATION = "OBSERVATION"
INFERENCE = "INFERENCE"
INTENT_SUBMITTED = "INTENT_SUBMITTED"
INTENT_RECEIPT = "INTENT_RECEIPT"
RADIO = "RADIO"
CONTROLLER_STEP = "CONTROLLER_STEP"
EFFECT = "EFFECT"
SCENARIO_EVENT = "SCENARIO_EVENT"
OPERATOR_INTERVENTION = "OPERATOR_INTERVENTION"
CLOCK_MODE = "CLOCK_MODE"
PRESENTATION = "PRESENTATION"

# Permitted origins per caller-recorded kind.  Human and agent player
# commands share INTENT_SUBMITTED, distinguished by the gateway-derived
# origin, because both use the same intent envelope and dispatcher.
KIND_ORIGINS: dict[str, frozenset[str]] = {
    OBSERVATION: frozenset({"gateway", "engine"}),
    INFERENCE: frozenset({"agent"}),
    INTENT_SUBMITTED: frozenset({"agent", "human", "scripted"}),
    INTENT_RECEIPT: frozenset({"gateway", "engine"}),
    RADIO: frozenset({"engine", "gateway"}),
    CONTROLLER_STEP: frozenset({"controller"}),
    EFFECT: frozenset({"engine"}),
    SCENARIO_EVENT: frozenset({"scenario"}),
    OPERATOR_INTERVENTION: frozenset({"operator"}),
    CLOCK_MODE: frozenset({"engine", "scenario", "operator"}),
    PRESENTATION: frozenset({"spectator", "human"}),
}
RECORDER_KINDS = frozenset({MANIFEST, RUN_START, RUN_END})
MIND_EXTRACTED, MIND_LOST = "mind_extracted", "mind_lost"
TERMINAL_EFFECTS = frozenset({MIND_EXTRACTED, MIND_LOST})

# Kinds an EFFECT may cite: something that can make the engine change state.
_EFFECT_CAUSES = frozenset(
    {INTENT_RECEIPT, RADIO, CONTROLLER_STEP, SCENARIO_EVENT, EFFECT, OPERATOR_INTERVENTION}
)
_RADIO_CAUSES = frozenset({INTENT_RECEIPT, CONTROLLER_STEP, RADIO, EFFECT})
_RADIO_STATUSES = frozenset({"queued", "transmitted", "delivered", "expired"})
_RECEIPT_STATUSES = frozenset({"accepted", "rejected"})

MANIFEST_FIELDS: dict[str, type | tuple[type, ...]] = {
    "schema": str,
    "run_id": str,
    "rig_commit": str,
    "app_commit": str,
    "engine": dict,
    "game": dict,
    "map": dict,
    "scenario": dict,
    "controller_images": dict,
    "adapter_version": str,
    "principals": list,
    "config_hashes": dict,
    "model_id": (str, type(None)),
    "external_budgets": dict,
    "clock_mode": str,
    "fps": int,
    "presentation": str,
    "required_decisions": list,
}
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_PRINCIPAL = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
PRINCIPAL_SOURCES = frozenset({"scripted", "agent", "human"})
# In a required decision, the one realized optional principal (job-N) the chain is about.
CHILD = "$child"
# The labels that bear out each declared source; anything else is reported.
SUPPORTED_BY = {"scripted": frozenset({"scripted_replay", "scripted_fixture"}), "agent": frozenset({"agent_only"}),
                "human": frozenset({"human_only"})}
# With declared principals, every record of these kinds names the principal
# whose view or authority it belongs to (for an EFFECT: the viewer).
PRINCIPAL_KINDS = frozenset({OBSERVATION, INFERENCE, INTENT_SUBMITTED, INTENT_RECEIPT, EFFECT})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY = re.compile(
    r"(^|_)(api_?key|secret|password|passwd|credentials?|authorization|"
    r"auth_?token|access_?token|refresh_?token|bearer|private_?key|cookie)($|_)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(r"(\bsk-[A-Za-z0-9_-]{16,}|\bBearer\s+\S{8,}|-----BEGIN [A-Z ]*PRIVATE KEY)")
_PRIVATE_PATH = re.compile(
    r"(^|[\s\"'=:(,])(/Users/|/home/|/root/|/data/|/private/|/tmp/|/var/folders/|~/|[A-Za-z]:\\Users\\)"
)
_RAW_CONVERSATION_KEY = re.compile(r"^(conversation|transcript|messages|raw_prompt|chat_log)$", re.IGNORECASE)


class LedgerError(ValueError):
    """A record that would make the ledger invalid."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


_RECORD_FIELD_TYPES: dict[str, type | tuple[type, ...]] = {
    "schema": str,
    "seq": int,
    "event_id": str,
    "kind": str,
    "origin": str,
    "sim_frame": int,
    "sim_time_s": (int, float),
    "wall_ns": int,
    "causes": list,
    "payload": dict,
    "prev": str,
    "digest": str,
}


def _one_of(value: Any, options: Iterable[str]) -> bool:
    """Membership that is safe for unhashable or wrong-typed input."""
    return isinstance(value, str) and value in options


def _depth_within(value: Any, limit: int) -> bool:
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            return False
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
    return True


def _reject_constant(name: str) -> Any:
    raise LedgerError("non_finite_number", name)


def parse_record(raw: bytes) -> Any:
    """Strict JSON: no NaN/Infinity.  Raises LedgerError on any bad line."""
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except LedgerError:
        raise
    # ValueError covers JSONDecodeError and over-long integer literals;
    # RecursionError covers pathological nesting.
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise LedgerError("not_json", type(exc).__name__) from None


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def record_digest(record: Mapping[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "digest"}
    try:
        encoded = canonical_json(body).encode("utf-8")
    # Lone surrogates, non-finite floats, over-long integers, unserializable
    # values and pathological nesting cannot have a stable digest.
    except (UnicodeEncodeError, TypeError, ValueError, RecursionError) as exc:
        raise LedgerError("invalid_record", f"not digestible: {type(exc).__name__}") from None
    return hashlib.sha256(encoded).hexdigest()


def scan_sensitive(value: Any, where: str = "payload") -> None:
    """Reject credential-shaped keys/values and absolute private paths."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LedgerError("invalid_payload", f"{where} has a non-string key")
            if _SECRET_KEY.search(key):
                raise LedgerError("credential_rejected", f"{where}.{key}")
            if _RAW_CONVERSATION_KEY.match(key):
                raise LedgerError("raw_conversation_rejected", f"{where}.{key}")
            scan_sensitive(item, f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            scan_sensitive(item, f"{where}[{index}]")
    elif isinstance(value, str):
        if _SECRET_VALUE.search(value):
            raise LedgerError("credential_rejected", where)
        if _PRIVATE_PATH.search(value):
            raise LedgerError("private_path_rejected", where)


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    if not isinstance(manifest, Mapping) or not all(isinstance(k, str) for k in manifest):
        raise LedgerError("invalid_manifest", "manifest must be an object with string keys")
    missing = sorted(set(MANIFEST_FIELDS) - set(manifest))
    if missing:
        raise LedgerError("invalid_manifest", "missing " + ",".join(missing))
    for name, expected in MANIFEST_FIELDS.items():
        value = manifest[name]
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise LedgerError("invalid_manifest", f"{name} has the wrong type")
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise LedgerError("invalid_manifest", "unsupported manifest schema")
    for name in ("rig_commit", "app_commit"):
        if not _COMMIT.match(manifest[name]):
            raise LedgerError("invalid_manifest", f"{name} must be a hex commit id")
    for name in ("engine", "game", "map"):
        if not all(isinstance(manifest[name].get(k), str) and manifest[name].get(k) for k in ("name", "version")):
            raise LedgerError("invalid_manifest", f"{name} needs name and version")
    scenario = manifest["scenario"]
    if not (isinstance(scenario.get("id"), str) and isinstance(scenario.get("version"), str)):
        raise LedgerError("invalid_manifest", "scenario needs id and version")
    if not isinstance(scenario.get("seed"), int) or isinstance(scenario.get("seed"), bool):
        raise LedgerError("invalid_manifest", "scenario seed must be an integer")
    for name, digest in manifest["controller_images"].items():
        if not isinstance(digest, str) or not _SHA256.match(digest):
            raise LedgerError("invalid_manifest", f"controller image {name} needs a sha256")
    for name, digest in manifest["config_hashes"].items():
        if not isinstance(digest, str) or not _SHA256.match(digest):
            raise LedgerError("invalid_manifest", f"config hash {name} needs a sha256")
    if not _one_of(manifest["clock_mode"], ("continuous", "paused")):
        raise LedgerError("invalid_manifest", "clock_mode must be continuous or paused")
    if manifest["fps"] <= 0:
        raise LedgerError("invalid_manifest", "fps must be positive")
    if not _one_of(manifest["presentation"], ("headless", "rendered")):
        raise LedgerError("invalid_manifest", "presentation must be headless or rendered")
    principals = manifest["principals"]
    if not all(isinstance(p, str) and p for p in principals) or len(set(principals)) != len(principals):
        raise LedgerError("invalid_manifest", "principals must be unique non-empty strings")
    sources = manifest.get("principal_sources")
    if sources is not None:
        # Per-principal views (rah26-mmn): the declared source of every
        # principal; the evidence, not this declaration, decides its label.
        if not isinstance(sources, Mapping) or set(sources) != set(principals) or not 1 <= len(sources) <= 16:
            raise LedgerError("invalid_manifest", "principal_sources must name exactly the manifest principals")
        for name, source in sources.items():
            if not _PRINCIPAL.match(name) or not _one_of(source, PRINCIPAL_SOURCES):
                raise LedgerError("invalid_manifest", f"principal {name!r} needs a source of scripted or agent")
    optional = manifest.get("optional_principals")
    if optional is not None and (not isinstance(optional, list) or len(optional) > 8 or len(set(optional)) != len(optional)
                                 or not all(isinstance(p, str) and p in principals for p in optional)):
        raise LedgerError("invalid_manifest", "optional_principals must be up to 8 distinct declared principals")
    labels = set()
    for decision in manifest["required_decisions"]:
        if not isinstance(decision, Mapping) or not isinstance(decision.get("label"), str):
            raise LedgerError("invalid_manifest", "required decision needs a label")
        if decision["label"] in labels:
            raise LedgerError("invalid_manifest", "duplicate required decision label")
        labels.add(decision["label"])
        after = decision.get("after_scenario_event")
        if after is not None and not isinstance(after, str):
            raise LedgerError("invalid_manifest", "after_scenario_event must be a string")
        if decision.get("type") is not None and not isinstance(decision["type"], str):
            raise LedgerError("invalid_manifest", "required decision type must be a string")
        if decision.get("to") is not None and not isinstance(decision["to"], str):
            raise LedgerError("invalid_manifest", "required decision recipient must be a string")
        owner = decision.get("principal")
        if CHILD in (owner, decision.get("to")) and not optional:
            raise LedgerError("invalid_manifest", f"{CHILD} needs declared optional_principals")
        if owner == CHILD:
            continue
        if sources is not None and len(sources) > 1 and not _one_of(owner, sources):
            raise LedgerError("invalid_manifest", "with several principals every required decision names its principal")
        if owner is not None and not _one_of(owner, sources or principals):
            raise LedgerError("invalid_manifest", "required decision names an unknown principal")
    scan_sensitive(manifest, "manifest")


class _LedgerState:
    """Incremental validator shared by the writer and the verifier."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        self.manifest: dict[str, Any] | None = None
        self.started = False
        self.ended = False
        self.head = GENESIS
        self.last_frame = -1
        self.last_wall = -1
        self.receipted: set[str] = set()  # submission event ids that have a receipt
        self.observation_principals: dict[str, str] = {}  # observation_id -> principal
        # Rescue outcomes (rah26-kbk), keyed by roster/principal name.
        self.extracted_views: set[tuple[str, str]] = set()  # (viewer, target)
        self.terminal: dict[str, tuple[str, int]] = {}      # target -> (mind_extracted|mind_lost, earliest frame)
        # Each principal's latest observation and accepted-receipt engine frames,
        # so a terminal fact recorded late still bounds what came before it.
        self.last_observed: dict[str, int] = {}
        self.last_accepted: dict[str, int] = {}

    def accept(self, record: Mapping[str, Any]) -> None:
        if not isinstance(record, Mapping):
            raise LedgerError("invalid_record", "record must be an object")
        seq = len(self.records)
        if self.ended:
            raise LedgerError("record_after_end", f"seq {seq}")
        expected = {"schema", "seq", "event_id", "kind", "origin", "sim_frame", "sim_time_s", "wall_ns",
                    "causes", "payload", "prev", "digest"}
        if set(record) != expected:
            raise LedgerError("invalid_record", f"seq {seq} has unexpected fields")
        for name, types in _RECORD_FIELD_TYPES.items():
            value = record[name]
            if not isinstance(value, types) or isinstance(value, bool):
                raise LedgerError("invalid_record", f"seq {seq} {name} has the wrong type")
        for name in ("seq", "sim_frame", "wall_ns"):
            if not 0 <= record[name] <= MAX_COUNTER:
                raise LedgerError("invalid_record", f"seq {seq} {name} is out of range")
        if not math.isfinite(record["sim_time_s"]):
            raise LedgerError("invalid_record", f"seq {seq} sim_time_s is not finite")
        if not all(isinstance(c, str) for c in record["causes"]):
            raise LedgerError("invalid_record", f"seq {seq} causes must be event ids")
        if not _depth_within(record["payload"], MAX_PAYLOAD_DEPTH):
            raise LedgerError("invalid_record", f"seq {seq} payload nests too deeply")
        if record["schema"] != LEDGER_SCHEMA:
            raise LedgerError("invalid_record", f"seq {seq} has an unsupported schema")
        if record["seq"] != seq or record["event_id"] != event_id_for(seq):
            raise LedgerError("sequence_gap", f"expected seq {seq}, found {record['seq']!r}")
        if record["prev"] != self.head:
            raise LedgerError("chain_broken", f"seq {seq}")
        if record["digest"] != record_digest(record):
            raise LedgerError("digest_mismatch", f"seq {seq}")
        kind, origin, payload = record["kind"], record["origin"], record["payload"]
        self._check_time(record, seq)
        causes = record["causes"]
        if len(set(causes)) != len(causes):
            raise LedgerError("invalid_causes", f"seq {seq} repeats a cause")
        for cause in causes:
            if cause not in self.by_id:
                raise LedgerError("unknown_cause", f"seq {seq} cites {cause}")
        scan_sensitive(payload)
        if kind in RECORDER_KINDS and causes:
            raise LedgerError("invalid_causes", f"seq {seq} {kind} cannot cite records")

        if seq == 0:
            if kind != MANIFEST or origin != "recorder":
                raise LedgerError("manifest_not_first")
            validate_manifest(payload)
            self.manifest = dict(payload)
        elif kind == MANIFEST:
            raise LedgerError("duplicate_manifest", f"seq {seq}")
        elif kind == RUN_START:
            if self.started or seq != 1 or origin != "recorder":
                raise LedgerError("run_start_misplaced", f"seq {seq}")
            self.started = True
        elif not self.started:
            raise LedgerError("record_before_run_start", f"seq {seq}")
        elif kind == RUN_END:
            if origin != "recorder":
                raise LedgerError("invalid_origin", f"seq {seq} RUN_END")
            if payload.get("record_count") != seq + 1 or payload.get("chain_head") != self.head:
                raise LedgerError("trailer_mismatch", f"seq {seq}")
            if not isinstance(payload.get("outcome"), dict):
                raise LedgerError("trailer_mismatch", "outcome must be an object")
            self.ended = True
        else:
            if not _one_of(kind, KIND_ORIGINS):
                raise LedgerError("unknown_kind", f"seq {seq} {kind!r}")
            if not _one_of(origin, KIND_ORIGINS[kind]):
                raise LedgerError("invalid_origin", f"seq {seq} {kind} from {origin!r}")
            self._check_kind(kind, payload, causes, seq)
            self._check_principal(kind, payload, causes, seq, record["wall_ns"])
            self._check_outcome(kind, payload, causes, seq, record["sim_frame"])

        stored = dict(record)
        if kind == INTENT_RECEIPT:
            self.receipted.update(c for c in causes if self.by_id[c]["kind"] == INTENT_SUBMITTED)
        self.records.append(stored)
        self.by_id[stored["event_id"]] = stored
        self.head = stored["digest"]
        self.last_frame = stored["sim_frame"]
        self.last_wall = stored["wall_ns"]

    def _check_time(self, record: Mapping[str, Any], seq: int) -> None:
        frame, wall = record["sim_frame"], record["wall_ns"]
        for name, value in (("sim_frame", frame), ("wall_ns", wall)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise LedgerError("invalid_time", f"seq {seq} {name}")
        if frame < self.last_frame:
            raise LedgerError("time_backwards", f"seq {seq} sim_frame {frame} < {self.last_frame}")
        if wall < self.last_wall:
            raise LedgerError("time_backwards", f"seq {seq} wall_ns")
        if seq == 0:
            fps = record["payload"].get("fps")
        else:
            fps = self.manifest["fps"] if self.manifest else None
        if isinstance(fps, int) and not isinstance(fps, bool) and fps > 0 \
                and record["sim_time_s"] != sim_time(frame, fps):
            raise LedgerError("invalid_time", f"seq {seq} sim_time_s does not match frame")

    def _cited(self, causes: Iterable[str], kinds: frozenset[str] | set[str]) -> list[dict[str, Any]]:
        return [self.by_id[c] for c in causes if self.by_id[c]["kind"] in kinds]

    def _check_kind(self, kind: str, payload: dict[str, Any], causes: list[str], seq: int) -> None:
        if kind == OBSERVATION:
            if not isinstance(payload.get("observation_id"), str):
                raise LedgerError("invalid_payload", f"seq {seq} observation_id")
        elif kind == INFERENCE:
            if not isinstance(payload.get("inference_id"), str) or not isinstance(payload.get("live"), bool):
                raise LedgerError("invalid_payload", f"seq {seq} inference needs inference_id and live")
            if not self._cited(causes, {OBSERVATION}):
                raise LedgerError("incompatible_cause", f"seq {seq} inference must cite an observation")
        elif kind == INTENT_SUBMITTED:
            for name in ("intent_id", "type", "based_on_observation_id"):
                if not isinstance(payload.get(name), str):
                    raise LedgerError("invalid_payload", f"seq {seq} {name}")
            # A repeated intent_id is a new attempt with its own event; the
            # dispatcher's duplicate rejection is evidence, not dropped.
        elif kind == INTENT_RECEIPT:
            if not _one_of(payload.get("status"), _RECEIPT_STATUSES) or not isinstance(payload.get("intent_id"), str):
                raise LedgerError("invalid_payload", f"seq {seq} receipt status")
            submitted = [r for r in self._cited(causes, {INTENT_SUBMITTED})
                         if r["payload"]["intent_id"] == payload.get("intent_id")]
            if len(submitted) != 1:
                raise LedgerError("incompatible_cause", f"seq {seq} receipt must cite exactly its submission")
            if submitted[0]["event_id"] in self.receipted:
                raise LedgerError("duplicate_receipt", f"seq {seq}")
        elif kind == RADIO:
            if not _one_of(payload.get("status"), _RADIO_STATUSES):
                raise LedgerError("invalid_payload", f"seq {seq} radio status")
            cited = self._cited(causes, _RADIO_CAUSES)
            if not cited:
                raise LedgerError("incompatible_cause", f"seq {seq} radio record needs a cause")
            self._no_rejected_receipt(cited, seq)
        elif kind == EFFECT:
            if not isinstance(payload.get("type"), str):
                raise LedgerError("invalid_payload", f"seq {seq} effect type")
            cited = self._cited(causes, _EFFECT_CAUSES)
            if not cited:
                raise LedgerError("incompatible_cause", f"seq {seq} effect needs a state-changing cause")
            self._no_rejected_receipt(cited, seq)
        elif kind == SCENARIO_EVENT:
            if not isinstance(payload.get("name"), str):
                raise LedgerError("invalid_payload", f"seq {seq} scenario event name")
        elif kind == CLOCK_MODE:
            if not _one_of(payload.get("mode"), ("continuous", "paused")) or not isinstance(payload.get("declared"), bool):
                raise LedgerError("invalid_payload", f"seq {seq} clock mode")
        elif kind == OPERATOR_INTERVENTION:
            if not isinstance(payload.get("action"), str):
                raise LedgerError("invalid_payload", f"seq {seq} intervention action")

    def _check_principal(self, kind: str, payload: dict[str, Any], causes: list[str], seq: int, wall: int) -> None:
        """With declared principals, keep each principal's view and authority separate."""
        sources = (self.manifest or {}).get("principal_sources")
        if sources is None or kind not in PRINCIPAL_KINDS:
            return
        principal = payload.get("principal")
        if not _one_of(principal, sources):
            raise LedgerError("unknown_principal", f"seq {seq} {kind} principal {principal!r}")
        # received_wall_ns is the gateway's receive time in this ledger's own
        # run-relative monotonic domain (Recorder.now_ns), so never after the record.
        stamp = payload.get("received_wall_ns")
        if stamp is not None and (not isinstance(stamp, int) or isinstance(stamp, bool) or not 0 <= stamp <= wall):
            raise LedgerError("invalid_payload", f"seq {seq} received_wall_ns must lie in [0, record wall_ns]")
        # client is a bounded, non-secret label for the client session, never its bearer token.
        if payload.get("client") is not None and not (isinstance(payload["client"], str) and _PRINCIPAL.match(payload["client"])):
            raise LedgerError("invalid_payload", f"seq {seq} client must be a short label")
        if kind in (INFERENCE, INTENT_SUBMITTED):
            # A decision may rest only on what its own principal could see: an
            # inference on its observations, an intent on those or its inferences.
            allowed = {OBSERVATION} if kind == INFERENCE else {OBSERVATION, INFERENCE}
            for cause in causes:
                if self.by_id[cause]["kind"] not in allowed:
                    raise LedgerError("incompatible_cause",
                                      f"seq {seq} {kind} of {principal} cites a {self.by_id[cause]['kind']}")
            for cited in self._cited(causes, allowed):
                if cited["payload"].get("principal") != principal:
                    raise LedgerError("cross_principal_citation",
                                      f"seq {seq} {principal} cites {cited['payload'].get('principal')}'s {cited['event_id']}")
        elif kind == INTENT_RECEIPT:
            submitted = self._cited(causes, {INTENT_SUBMITTED})[0]
            if submitted["payload"].get("principal") != principal:
                raise LedgerError("principal_mismatch", f"seq {seq} receipt for another principal's submission")
            # An intent may rest only on its principal's own recorded observation;
            # another principal's, or one never recorded, is valid only as a refused probe.
            owner = self.observation_principals.get(submitted["payload"]["based_on_observation_id"])
            if payload["status"] == "accepted" and owner != principal:
                raise LedgerError("cross_principal_citation",
                                  f"seq {seq} accepted {principal} intent based on "
                                  + (f"{owner}'s observation" if owner else "an unrecorded observation"))
        elif kind == OBSERVATION:
            # A re-delivered observation keeps its id; an id never changes owner.
            owner = self.observation_principals.setdefault(payload["observation_id"], principal)
            if owner != principal:
                raise LedgerError("observation_owner_conflict", f"seq {seq} {payload['observation_id']}")

    def _check_outcome(self, kind: str, payload: dict[str, Any], causes: list[str], seq: int, sim_frame: int) -> None:
        """A mind is extracted or lost at most once, never both, and then acts no more."""
        if kind == EFFECT and _one_of(payload.get("type"), TERMINAL_EFFECTS):
            effect, detail = payload["type"], payload.get("detail")
            target = detail.get("target") if isinstance(detail, dict) else None
            if not isinstance(target, str):
                raise LedgerError("invalid_payload", f"seq {seq} {effect} needs detail.target")
            viewer = payload.get("principal")
            if viewer is not None and not isinstance(viewer, str):
                raise LedgerError("invalid_payload", f"seq {seq} {effect} viewer")
            frame = _engine_frame(payload, sim_frame)
            previous = self.terminal.get(target)
            if previous and previous[0] != effect:
                raise LedgerError("extraction_after_loss" if effect == MIND_EXTRACTED else "loss_after_extraction",
                                  f"seq {seq} {target}")
            if effect == MIND_EXTRACTED and (viewer, target) in self.extracted_views:
                raise LedgerError("duplicate_extraction", f"seq {seq} {target} seen by {viewer}")
            # The same terminal fact may reach several viewers; it keeps its
            # earliest engine frame, whatever order the records arrive in.
            earliest = (effect, frame) if previous is None or frame < previous[1] else previous
            self._terminal_bound(target, seq, terminal=earliest)
            if effect == MIND_EXTRACTED:
                self.extracted_views.add((viewer, target))
            self.terminal[target] = earliest
            return
        principal = payload.get("principal")
        if not isinstance(principal, str) or kind not in (OBSERVATION, INTENT_RECEIPT):
            return
        if kind == INTENT_RECEIPT and payload.get("status") != "accepted":
            return  # a refusal is never an action
        latest = self.last_observed if kind == OBSERVATION else self.last_accepted
        frame = _engine_frame(payload, sim_frame)
        frame = max(frame, latest.get(principal, frame))
        self._terminal_bound(principal, seq, **{"observed" if kind == OBSERVATION else "accepted": frame})
        latest[principal] = frame  # only once the record is admitted

    def _terminal_bound(self, principal: str, seq: int, *, observed: int | None = None,
                        accepted: int | None = None, terminal: tuple[str, int] | None = None) -> None:
        """K5 by engine frame: one final observation at the terminal frame and no
        accepted intent after it, regardless of the order records were written."""
        effect, frame = terminal or self.terminal.get(principal, (None, None))
        if effect is None:
            return
        observed = self.last_observed.get(principal, frame) if observed is None else observed
        accepted = self.last_accepted.get(principal, frame) if accepted is None else accepted
        if observed > frame:
            raise LedgerError("terminal_principal_observed", f"seq {seq} {principal} after {effect}")
        if accepted > frame:
            raise LedgerError("terminal_principal_acted", f"seq {seq} {principal} after {effect}")

    @staticmethod
    def _no_rejected_receipt(cited: list[dict[str, Any]], seq: int) -> None:
        for record in cited:
            if record["kind"] == INTENT_RECEIPT and record["payload"]["status"] != "accepted":
                raise LedgerError("incompatible_cause", f"seq {seq} cites a rejected receipt")


def _engine_frame(payload: Mapping[str, Any], sim_frame: int) -> int:
    """The engine frame a record describes; the ledger frame is its ingestion frame."""
    for value in (payload.get("engine_frame"), payload.get("frame"), (payload.get("observation") or {}).get("frame")
                  if isinstance(payload.get("observation"), Mapping) else None):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return sim_frame


def _json_copy(payload: Any, seq: int) -> Any:
    try:
        text = canonical_json(payload)
        text.encode("utf-8")
        return json.loads(text)
    except (UnicodeEncodeError, TypeError, ValueError, RecursionError) as exc:
        raise LedgerError("invalid_payload", f"seq {seq} is not finite JSON: {type(exc).__name__}") from None


def event_id_for(seq: int) -> str:
    return f"ev-{seq:06d}"


def sim_time(frame: int, fps: int) -> float:
    return round(frame / fps, 6)


class Recorder:
    """Append-only writer.  Implements the gateway's ``LedgerSink.record``.

    Thread-safe: a threaded gateway may call ``record`` concurrently; the
    clock read, validation and write happen under one lock, so sequence,
    chain and monotonic wall time stay consistent with file order.
    """

    def __init__(self, handle: Any, manifest: Mapping[str, Any], clock: Callable[[], int], fsync: bool) -> None:
        self._lock = threading.RLock()
        self._handle = handle
        self._clock = clock
        self._epoch = clock()
        self._fsync = fsync
        self._state = _LedgerState()
        self._append(MANIFEST, origin="recorder", sim_frame=0, payload=dict(manifest), causes=(), sync=True)

    @classmethod
    def create(
        cls,
        path: str | os.PathLike[str],
        manifest: Mapping[str, Any],
        *,
        clock: Callable[[], int] = time.monotonic_ns,
        fsync: bool = True,
    ) -> "Recorder":
        """Create a new ledger; refuses to overwrite or extend an existing file."""
        validate_manifest(manifest)
        handle = open(path, "x", encoding="utf-8", newline="\n")
        try:
            return cls(handle, manifest, clock, fsync)
        except BaseException:
            handle.close()
            raise

    @property
    def manifest(self) -> dict[str, Any]:
        assert self._state.manifest is not None
        return dict(self._state.manifest)

    def now_ns(self) -> int:
        """Current time in this ledger's run-relative monotonic domain, for receive stamps."""
        return self._clock() - self._epoch

    def run_start(self, sim_frame: int = 0) -> str:
        return self._append(RUN_START, origin="recorder", sim_frame=sim_frame, payload={}, causes=(), sync=True)

    def record(
        self,
        kind: str,
        *,
        sim_frame: int,
        origin: str,
        payload: dict[str, Any],
        causes: Sequence[str] = (),
    ) -> str:
        if _one_of(kind, RECORDER_KINDS):
            raise LedgerError("reserved_kind", kind)
        return self._append(kind, origin=origin, sim_frame=sim_frame, payload=payload, causes=causes,
                            sync=kind == OPERATOR_INTERVENTION)

    def run_end(self, sim_frame: int, outcome: Mapping[str, Any]) -> str:
        with self._lock:
            seq = len(self._state.records)
            payload = {"record_count": seq + 1, "chain_head": self._state.head, "outcome": dict(outcome)}
            event_id = self._append(RUN_END, origin="recorder", sim_frame=sim_frame, payload=payload, causes=(),
                                    sync=True)
            self.close()
            return event_id

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.close()

    def __enter__(self) -> "Recorder":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _append(self, kind: str, *, origin: str, sim_frame: int, payload: Mapping[str, Any],
                causes: Sequence[str], sync: bool) -> str:
        with self._lock:
            return self._append_locked(kind, origin=origin, sim_frame=sim_frame, payload=payload, causes=causes,
                                       sync=sync)

    def _append_locked(self, kind: str, *, origin: str, sim_frame: int, payload: Mapping[str, Any],
                       causes: Sequence[str], sync: bool) -> str:
        if self._handle.closed:
            raise LedgerError("ledger_closed")
        seq = len(self._state.records)
        wall = self._clock() - self._epoch
        fps = payload.get("fps") if kind == MANIFEST else self.manifest["fps"]
        record: dict[str, Any] = {
            "schema": LEDGER_SCHEMA,
            "seq": seq,
            "event_id": event_id_for(seq),
            "kind": kind,
            "origin": origin,
            "sim_frame": sim_frame,
            "sim_time_s": (sim_time(sim_frame, fps)
                           if isinstance(sim_frame, int) and not isinstance(sim_frame, bool)
                           and 0 <= sim_frame <= MAX_COUNTER else None),
            "wall_ns": wall,
            "causes": list(causes) if isinstance(causes, (list, tuple)) else causes,
            # Round-trip through JSON so the stored record is exactly what a reader sees.
            "payload": _json_copy(payload, seq),
            "prev": self._state.head,
        }
        record["digest"] = record_digest(record)
        line = canonical_json(record)
        if len(line.encode("utf-8")) + 1 > MAX_RECORD_BYTES:
            raise LedgerError("record_too_large", f"seq {seq}")
        self._state.accept(record)  # raises before anything is written
        try:
            self._handle.write(line + "\n")
            self._handle.flush()
            if sync and self._fsync:
                os.fsync(self._handle.fileno())
        except BaseException:
            # State already advanced; refuse further appends so the file ends
            # without a trailer and verifies as incomplete, never as complete.
            self._handle.close()
            raise
        return record["event_id"]


@dataclass
class Verification:
    ok: bool
    status: str  # complete | truncated | incomplete | corrupt | invalid | empty
    errors: list[str] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    manifest: dict[str, Any] | None = None
    classification: str | None = None
    autonomous: bool = False
    evidence_limits: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    outcome: dict[str, Any] | None = None
    principal_classifications: dict[str, str] = field(default_factory=dict)
    principals: dict[str, dict[str, Any]] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "errors": self.errors,
            "classification": self.classification,
            "autonomous": self.autonomous,
            "evidence_limits": self.evidence_limits,
            "counts": self.counts,
            "outcome": self.outcome,
            "principal_classifications": self.principal_classifications,
            "principals": self.principals,
            "run_id": self.manifest.get("run_id") if self.manifest else None,
        }


_CORRUPT_CODES = frozenset({"digest_mismatch", "chain_broken", "sequence_gap", "record_after_end",
                            "trailer_mismatch", "invalid_record", "not_json", "non_finite_number"})


def verify_ledger(path: str | os.PathLike[str]) -> Verification:
    """Re-validate every record, the chain and the trailer, then classify."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return Verification(False, "invalid", [f"unreadable: {exc.strerror}"])
    if not data:
        return Verification(False, "empty", ["ledger is empty"])
    state = _LedgerState()
    lines = data.split(b"\n")
    tail = lines.pop()  # b"" when the file ends with a newline
    for number, raw in enumerate(lines, start=1):
        try:
            record = parse_record(raw)
        except LedgerError as exc:
            return _failed(state, "corrupt", f"line {number}: not a strict JSON record ({exc})")
        try:
            state.accept(record)
        except LedgerError as exc:
            status = "corrupt" if exc.code in _CORRUPT_CODES else "invalid"
            return _failed(state, status, f"line {number}: {exc}")
    if tail:
        return _failed(state, "truncated", f"line {len(lines) + 1}: partial record without newline")
    if not state.ended:
        return _failed(state, "incomplete", "no RUN_END trailer; the capture may be truncated")
    problem = _principal_problem(state)
    if problem:
        return _failed(state, "invalid", problem)
    result = Verification(True, "complete", records=state.records, manifest=state.manifest)
    result.outcome = state.records[-1]["payload"]["outcome"]
    _classify(result)
    return result


def replay(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Return verified records in order, or raise; never replays a partial run."""
    result = verify_ledger(path)
    if not result.ok:
        raise LedgerError(f"replay_refused_{result.status}", "; ".join(result.errors))
    return result.records


def _principal_problem(state: _LedgerState) -> str | None:
    """End-of-ledger check: a foreign-cited intent must have been refused."""
    if (state.manifest or {}).get("principal_sources") is None:
        return None
    answered = {}
    for record in state.records:
        if record["kind"] == INTENT_RECEIPT:
            for cause in record["causes"]:
                answered[cause] = record["payload"]["status"]
    for record in state.records:
        if record["kind"] != INTENT_SUBMITTED:
            continue
        payload = record["payload"]
        owner = state.observation_principals.get(payload["based_on_observation_id"])
        if owner != payload["principal"] and answered.get(record["event_id"]) != "rejected":
            cited = f"{owner}'s observation" if owner else "an unrecorded observation"
            return (f"cross_principal_citation: {record['event_id']} {payload['principal']} intent based on "
                    f"{cited} was {answered.get(record['event_id'], 'never answered')}")
    return None


def _failed(state: _LedgerState, status: str, error: str) -> Verification:
    return Verification(False, status, [error], records=list(state.records), manifest=state.manifest)


def _classify(result: Verification) -> None:
    manifest = result.manifest or {}
    records = result.records
    by_id = {r["event_id"]: r for r in records}
    counts: dict[str, int] = {}
    for record in records:
        counts[record["kind"]] = counts.get(record["kind"], 0) + 1
    result.counts = counts

    interventions = [r for r in records if r["kind"] == OPERATOR_INTERVENTION]
    interventions += [r for r in records if r["kind"] == CLOCK_MODE and not r["payload"]["declared"]]
    intents = [r for r in records if r["kind"] == INTENT_SUBMITTED]
    # Keyed by the submission event each receipt answers, so a repeated
    # intent_id attempt never borrows another attempt's acceptance.
    receipts = {c: r for r in records if r["kind"] == INTENT_RECEIPT
                for c in r["causes"] if by_id[c]["kind"] == INTENT_SUBMITTED}
    observations = {r["event_id"]: r for r in records if r["kind"] == OBSERVATION}

    def live_chain(intent: dict[str, Any]) -> list[dict[str, Any]]:
        """Observations reached via a cited live inference of the declared model."""
        found = []
        for cause in intent["causes"]:
            inference = by_id[cause]
            if inference["kind"] != INFERENCE or not inference["payload"]["live"]:
                continue
            if manifest.get("model_id") is None or inference["payload"].get("model_id") != manifest["model_id"]:
                continue
            for observed in inference["causes"]:
                observation = observations.get(observed)
                if observation and observation["payload"]["observation_id"] == intent["payload"]["based_on_observation_id"]:
                    found.append(observation)
        return found

    chains = {r["event_id"]: live_chain(r) for r in intents if r["origin"] == "agent"}

    # Optional principals (job-N slots) are present only once the engine bound
    # one and the gateway recorded its own observation stream.
    optional = manifest.get("optional_principals") or []
    realized = sorted({r["payload"].get("principal") for r in records if r["kind"] == OBSERVATION} & set(optional))
    absent = set(optional) - set(realized)

    def alternatives(decisions: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Expand $child against ONE realized child at a time: a chain met by two children is not met."""
        if not any(CHILD in (d.get("principal"), d.get("to")) for d in decisions):
            return [decisions]
        return [[{**d, **{k: child for k in ("principal", "to") if d.get(k) == CHILD}} for d in decisions]
                for child in realized]

    def label(subset: list[dict[str, Any]], decision_sets: list[list[dict[str, Any]]]) -> str:
        origins = {r["origin"] for r in subset}
        if interventions:
            return "assisted"
        if "scripted" in origins:
            return "scripted_replay"
        if any(not chains[r["event_id"]] for r in subset if r["origin"] == "agent"):
            return "unattributed"
        if {"agent", "human"} <= origins:
            return "centaur"
        if origins == {"human"}:
            return "human_only"
        if origins == {"agent"}:
            met = any(_required_decisions_met(decisions, records, subset, receipts, chains) for decisions in decision_sets)
            return "agent_only" if met else "agent_partial"
        return "scripted_fixture"

    required = manifest.get("required_decisions") or []
    classification = label(intents, alternatives(required))
    sources = manifest.get("principal_sources")
    if sources is not None:
        # Every principal is labelled from its own intents and decisions; a
        # decision without a principal belongs to the sole principal. An
        # optional slot that was never realized is absent, not a failure.
        for name in sorted(set(sources) - absent):
            mine = [r for r in intents if r["payload"]["principal"] == name]
            sets = [[d for d in decisions if d.get("principal", name if len(sources) == 1 else None) == name]
                    for decisions in alternatives(required)]
            result.principal_classifications[name] = label(mine, sets)
            result.principals[name] = _principal_summary(name, records, mine, receipts)
    result.classification = classification
    result.autonomous = classification == "agent_only" and all(
        c == "agent_only" for c in result.principal_classifications.values())

    limits = []
    if manifest.get("presentation") == "headless":
        limits.append("headless capture: not a visual demo")
    if classification in ("scripted_fixture", "scripted_replay"):
        limits.append("scripted: not autonomous gameplay")
    if classification == "assisted":
        limits.append("operator intervention after start: not autonomous")
    if classification in ("unattributed", "agent_partial"):
        limits.append("agent decisions lack complete live-inference evidence: not autonomous")
    accepted: dict[str, int] = {}
    for receipt in receipts.values():
        if receipt["payload"]["status"] == "accepted":
            accepted[receipt["payload"]["intent_id"]] = accepted.get(receipt["payload"]["intent_id"], 0) + 1
    for intent_id in sorted(i for i, n in accepted.items() if n > 1):
        # Recorded faithfully rather than refused; the dispatcher should not do this.
        limits.append(f"intent_id {intent_id} accepted more than once")
    for name, declared in sorted((sources or {}).items()):
        if name in absent:
            continue
        got = result.principal_classifications[name]
        if got not in SUPPORTED_BY[declared]:
            # The declaration never upgrades a label; the disagreement is reported.
            limits.append(f"principal {name} declared {declared} but its evidence classifies as {got}")
    limits.append("hash chain detects accidental corruption, not deliberate forgery")
    result.evidence_limits = limits


def _principal_summary(name: str, records: list[dict[str, Any]], intents: list[dict[str, Any]],
                       receipts: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """Counts and observation-to-receive latency for one principal's stream."""
    mine = [r for r in records if r["kind"] == OBSERVATION and r["payload"]["principal"] == name]
    latencies, inconsistent = [], 0
    for intent in intents:
        # The cited observation as it stood when the intent arrived: the latest
        # recording of that id before the intent, never a later re-delivery.
        seen = None
        for observation in mine:
            if observation["seq"] < intent["seq"] and \
                    observation["payload"]["observation_id"] == intent["payload"]["based_on_observation_id"]:
                seen = observation
        if seen is None:
            continue
        received = intent["payload"].get("received_wall_ns", intent["wall_ns"])
        if received < seen["wall_ns"]:
            inconsistent += 1  # received before the observation it cites: reported, not hidden
            continue
        latencies.append((received - seen["wall_ns"]) / 1e9)
    latencies.sort()
    median = None
    if latencies:
        middle = len(latencies) // 2
        median = latencies[middle] if len(latencies) % 2 else (latencies[middle - 1] + latencies[middle]) / 2
    return {
        "observations": sum(1 for r in records if r["kind"] == OBSERVATION and r["payload"]["principal"] == name),
        "effects": sum(1 for r in records if r["kind"] == EFFECT and r["payload"]["principal"] == name),
        "intents": len(intents),
        "accepted": sum(1 for r in intents if receipts.get(r["event_id"], {}).get("payload", {}).get("status") == "accepted"),
        "decision_latency_s": {"samples": len(latencies), "median": median, "inconsistent": inconsistent},
    }


def _recipient(payload: Mapping[str, Any]) -> Any:
    """The addressee of a MESSAGE intent, from its payload or its submitted envelope."""
    for body in (payload.get("payload"), (payload.get("envelope") or {}).get("payload")):
        if isinstance(body, Mapping) and "to" in body:
            return body["to"]
    return None


def _required_decisions_met(required: list[dict[str, Any]], records: list[dict[str, Any]],
                            intents: list[dict[str, Any]], receipts: Mapping[str, dict[str, Any]],
                            chains: Mapping[str, list[dict[str, Any]]]) -> bool:
    if not required:
        return False
    for decision in required:
        after = decision.get("after_scenario_event")
        after_seq = None
        if after is not None:
            fired = [r["seq"] for r in records if r["kind"] == SCENARIO_EVENT and r["payload"]["name"] == after]
            if not fired:
                return False
            after_seq = fired[0]
        satisfied = False
        for intent in intents:
            payload = intent["payload"]
            if payload.get("decision") != decision["label"]:
                continue
            if decision.get("principal") is not None and payload.get("principal") != decision["principal"]:
                continue  # a decision binds its principal, whoever the caller passes
            if decision.get("type") is not None and payload["type"] != decision["type"]:
                continue
            # A delegation names its recipient: a message to anyone else is not
            # it, and it counts only once the engine delivered that message into
            # the recipient's own stream (an accepted but dropped message does not).
            if decision.get("to") is not None and (_recipient(payload) != decision["to"] or not any(
                    r["kind"] == EFFECT and r["payload"].get("type") == "message_delivered"
                    and r["payload"].get("intent_id") == payload["intent_id"]
                    and r["payload"].get("principal") == decision["to"]
                    and (r["payload"].get("detail") or {}).get("to") == decision["to"] for r in records)):
                continue
            chain = chains.get(intent["event_id"], [])
            if after_seq is not None:
                # The decision must respond to the event: its observation (and
                # therefore its inference and the intent) all follow it.
                chain = [observation for observation in chain if observation["seq"] > after_seq]
            if not chain:
                continue
            receipt = receipts.get(intent["event_id"])
            if receipt and receipt["payload"]["status"] == "accepted":
                satisfied = True
                break
        if not satisfied:
            return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "verify":
        print("usage: python -m synapse_commander.recording verify LEDGER.jsonl", file=sys.stderr)
        return 2
    result = verify_ledger(args[1])
    print(json.dumps(result.summary(), indent=2, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
