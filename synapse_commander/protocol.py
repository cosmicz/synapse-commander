"""Small, versioned values shared by the external gateway and engine port.

The smoke schema here is intentionally narrower than the Broken Relay mission
contract.  It proves the authority boundary; it is not a complete scenario API.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import re
from typing import Any, Mapping, Protocol, Sequence


SCHEMA_VERSION = "synapse-smoke/v1"
INTENT_SCHEMA_VERSION = "IntentEnvelope/v1"
MAX_ENVELOPE_BYTES = 4 * 1024
MAX_OBSERVATION_BYTES = 32 * 1024
MAX_OBSERVATION_AGE_FRAMES = 900
MAX_EXPIRY_AHEAD_FRAMES = 900
MAX_ENTITIES = 32
MAX_RECENT_EVENTS = 16

INTENT_TYPES = frozenset({"PRODUCE", "TASK", "INSTALL"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_CONTROLLERS = frozenset({"haul_v1", "deploy_relay_v1", "haul_detour_v2"})
_CHASSIS = frozenset({"hauler", "relay_scout"})
_VERSIONS = _CONTROLLERS
_REQUIRED_PAYLOAD_FIELDS = {
    "PRODUCE": frozenset({"factory", "chassis", "controller", "parameters"}),
    "TASK": frozenset({"unit", "verb", "args"}),
    "INSTALL": frozenset({"unit", "expected_version", "replacement_version", "parameters"}),
}


class ProtocolError(ValueError):
    """A stable, safe error returned at the untrusted API boundary."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail or code
        super().__init__(self.detail)


def compact_json(value: Any) -> bytes:
    """Return deterministic JSON used for all public size limits."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _bounded_string(value: Any, field: str, limit: int = 128) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or len(value) > limit:
        raise ProtocolError("invalid_payload", f"{field} must be a non-empty string up to {limit} characters")
    return value


def _bounded_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("invalid_payload", f"{field} must be an object")
    if len(value) > 16:
        raise ProtocolError("invalid_payload", f"{field} has too many fields")
    # JSON was parsed at the boundary; this recursive size check prevents a
    # deeply nested object from becoming an unbounded engine message.
    if len(compact_json(value)) > 1024:
        raise ProtocolError("invalid_payload", f"{field} is too large")
    return value


def _safe_payload(value: Any, field: str, depth: int = 0) -> None:
    """Reject command-smuggling keys and unbounded nested payload structures."""
    if depth > 4:
        raise ProtocolError("invalid_payload", f"{field} is nested too deeply")
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str) or key.casefold() in {"lua", "raw", "command", "console", "engine_command"}:
                raise ProtocolError("invalid_payload", "raw engine commands are not permitted")
            _safe_payload(nested, field, depth + 1)
    elif isinstance(value, list):
        if len(value) > 16:
            raise ProtocolError("invalid_payload", f"{field} has too many list values")
        for nested in value:
            _safe_payload(nested, field, depth + 1)
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise ProtocolError("invalid_payload", f"{field} contains an unsupported value")


@dataclass(frozen=True)
class IntentEnvelope:
    schema: str
    intent_id: str
    principal: str
    team: str
    based_on_observation_id: str
    expires_at_frame: int
    type: str
    payload: dict[str, Any]

    @classmethod
    def from_untrusted(cls, value: Any, *, principal: str, team: str) -> "IntentEnvelope":
        """Parse a client request and inject, rather than trust, identity."""
        if not isinstance(value, dict):
            raise ProtocolError("malformed_json", "body must be a JSON object")
        allowed = {"schema", "intent_id", "based_on_observation_id", "expires_at_frame", "type", "payload", "principal", "team"}
        unknown = set(value) - allowed
        if unknown:
            raise ProtocolError("unknown_field", "unsupported envelope field")
        if value.get("schema") != INTENT_SCHEMA_VERSION:
            raise ProtocolError("unsupported_version", "unsupported intent schema")
        intent_type = value.get("type")
        if not isinstance(intent_type, str):
            raise ProtocolError("invalid_intent_type", "intent type must be a string")
        if intent_type not in INTENT_TYPES:
            # This includes raw engine/Lua/console command shapes.
            raise ProtocolError("intent_not_allowed", "only PRODUCE, TASK and INSTALL are exposed")
        payload = _bounded_object(value.get("payload"), "payload")
        required = _REQUIRED_PAYLOAD_FIELDS[intent_type]
        if set(payload) != required:
            raise ProtocolError("invalid_payload", "payload fields do not match the typed intent")
        for field in required - {"parameters", "args"}:
            _bounded_string(payload[field], field)
        if intent_type == "PRODUCE" and (payload["chassis"] not in _CHASSIS or payload["controller"] not in _CONTROLLERS):
            raise ProtocolError("invalid_payload", "unknown chassis or controller")
        if intent_type == "INSTALL" and (payload["expected_version"] not in _VERSIONS or payload["replacement_version"] not in _VERSIONS):
            raise ProtocolError("invalid_payload", "unknown controller version")
        if intent_type == "TASK" and payload["verb"] != "advance":
            raise ProtocolError("verb_not_supported", "only the engine-smoke advance task is exposed")
        for field in required & {"parameters", "args"}:
            _bounded_object(payload[field], field)
            if payload[field]:
                # The smoke schema deliberately has no controller-specific
                # argument vocabulary. Broken Relay parameters belong in its
                # later engine adapter, not an untyped escape hatch here.
                raise ProtocolError("invalid_payload", f"{field} is empty in the smoke schema")
        _safe_payload(payload, "payload")
        expiry = value.get("expires_at_frame")
        if not isinstance(expiry, int) or isinstance(expiry, bool) or expiry < 0:
            raise ProtocolError("invalid_expiry", "expires_at_frame must be a non-negative integer")
        envelope = cls(
            schema=INTENT_SCHEMA_VERSION,
            intent_id=_bounded_string(value.get("intent_id"), "intent_id"),
            principal=principal,
            team=team,
            based_on_observation_id=_bounded_string(value.get("based_on_observation_id"), "based_on_observation_id"),
            expires_at_frame=expiry,
            type=intent_type,
            payload=payload,
        )
        if len(compact_json(envelope.to_wire())) > MAX_ENVELOPE_BYTES:
            raise ProtocolError("envelope_too_large")
        return envelope

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IntentReceipt:
    intent_id: str
    accepted: bool
    frame: int
    receipt_id: str
    reason: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EffectEvent:
    """A later authoritative world event, never implied by an acceptance."""

    effect_id: str
    intent_id: str | None
    frame: int
    type: str
    subject: str

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


class LedgerSink(Protocol):
    """Narrow structural hook implemented by the optional run recorder."""

    def record(
        self,
        kind: str,
        *,
        sim_frame: int,
        origin: str,
        payload: dict[str, Any],
        causes: Sequence[str] = (),
    ) -> str: ...


def bounded_observation(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a port result into the small, bounded public smoke schema."""
    if not isinstance(value, Mapping):
        raise ProtocolError("invalid_observation", "transport returned no observation object")
    observation_id = _bounded_string(value.get("observation_id"), "observation_id")
    frame = value.get("frame")
    if not isinstance(frame, int) or isinstance(frame, bool) or frame < 0:
        raise ProtocolError("invalid_observation", "frame must be a non-negative integer")
    entities = value.get("entities", [])
    receipts = value.get("recent_receipts", [])
    effects = value.get("recent_effects", [])
    if not isinstance(entities, list) or not isinstance(receipts, list) or not isinstance(effects, list):
        raise ProtocolError("invalid_observation", "observation collections must be lists")
    # The port may gather concurrent reports. Canonical ordering makes the
    # bounded frame and its ledger evidence reproducible despite that order.
    def event_key(item: Any) -> tuple[int, bytes]:
        return (item.get("frame", -1) if isinstance(item, Mapping) and isinstance(item.get("frame", -1), int) else -1, compact_json(item))
    ordered_entities = sorted(entities, key=compact_json)
    ordered_receipts = sorted(receipts, key=event_key)
    ordered_effects = sorted(effects, key=event_key)
    clipped_entities = ordered_entities[:MAX_ENTITIES]
    clipped_receipts = ordered_receipts[:MAX_RECENT_EVENTS]
    clipped_effects = ordered_effects[:MAX_RECENT_EVENTS]
    result: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "observation_id": observation_id,
        "frame": frame,
        "resources": value.get("resources", {}),
        "entities": clipped_entities,
        "recent_receipts": clipped_receipts,
        "recent_effects": clipped_effects,
        "truncated": bool(value.get("truncated", False)) or len(entities) > len(clipped_entities) or len(receipts) > len(clipped_receipts) or len(effects) > len(clipped_effects),
        "omitted": {
            "entities": max(0, len(entities) - len(clipped_entities)),
            "recent_receipts": max(0, len(receipts) - len(clipped_receipts)),
            "recent_effects": max(0, len(effects) - len(clipped_effects)),
        },
    }
    if not isinstance(result["resources"], dict):
        raise ProtocolError("invalid_observation", "resources must be an object")
    # The smoke client needs its declared fixed goal, not the engine's private
    # scenario table. Copy only this public, typed field before the size check.
    scenario = value.get("scenario", {})
    if not isinstance(scenario, Mapping):
        raise ProtocolError("invalid_observation", "scenario must be an object")
    waypoints = scenario.get("waypoints", {})
    if not isinstance(waypoints, Mapping):
        raise ProtocolError("invalid_observation", "waypoints must be an object")
    if "W1" in waypoints:
        goal = waypoints["W1"]
        valid = isinstance(goal, list) and len(goal) == 3
        if valid:
            try:
                valid = all(type(n) in (int, float) and math.isfinite(n) for n in goal)
            except OverflowError:
                valid = False
        if not valid:
            raise ProtocolError("invalid_observation", "W1 must contain three finite coordinates")
        result["scenario"] = {"waypoints": {"W1": list(goal)}}
    permitted = value.get("permitted_intents", [])
    if not isinstance(permitted, list) or len(permitted) > 16:
        raise ProtocolError("invalid_observation", "invalid permitted intents")
    result["permitted_intents"] = []
    for intent in permitted:
        if not isinstance(intent, Mapping):
            raise ProtocolError("invalid_observation", "invalid permitted intent")
        verbs = intent.get("verbs", [])
        if not isinstance(verbs, list):
            raise ProtocolError("invalid_observation", "invalid permitted verbs")
        if intent.get("type") == "TASK" and "advance" in verbs:
            result["permitted_intents"] = [{"type": "TASK", "verbs": ["advance"]}]
            break
    if len(compact_json(result)) > MAX_OBSERVATION_BYTES:
        raise ProtocolError("observation_too_large", "transport observation exceeds public cap")
    return result
