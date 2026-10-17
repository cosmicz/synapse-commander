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

from .scenarios import load_scenario, supported_chassis, supported_controllers


SCHEMA_VERSION = "synapse-smoke/v1"
INTENT_SCHEMA_VERSION = "IntentEnvelope/v1"
MAX_ENVELOPE_BYTES = 4 * 1024
MAX_OBSERVATION_BYTES = 32 * 1024
MAX_OBSERVATION_AGE_FRAMES = 900
MAX_EXPIRY_AHEAD_FRAMES = 900
MAX_ENTITIES = 32
MAX_RECENT_EVENTS = 16
MAX_FACTORIES = 4
MAX_FACTORY_JOBS = 8
MAX_BUDGET = 2**31 - 1

INTENT_TYPES = frozenset({"PRODUCE", "TASK", "INSTALL"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
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
    def from_untrusted(cls, value: Any, *, principal: str, team: str, chassis: frozenset[str] | None = None, controllers: frozenset[str] | None = None) -> "IntentEnvelope":
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
        if intent_type == "PRODUCE" and chassis is not None and controllers is not None and (payload["chassis"] not in chassis or payload["controller"] not in controllers):
            raise ProtocolError("invalid_payload", "unknown chassis or controller")
        if intent_type == "INSTALL" and controllers is not None and (payload["expected_version"] not in controllers or payload["replacement_version"] not in controllers):
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


def _public_count(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_BUDGET:
        raise ProtocolError("invalid_observation", f"{field} must be a bounded non-negative integer")
    return value


def _public_identifier(value: Any, field: str) -> str:
    try:
        return _bounded_string(value, field)
    except ProtocolError as error:
        raise ProtocolError("invalid_observation", error.detail) from error


def _bounded_factory_observation(value: Any, scenario: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int, int]:
    """Project only public factory/job fields from an engine observation."""
    if value is None:
        return [], 0, 0
    if not isinstance(value, list):
        raise ProtocolError("invalid_observation", "factories must be a list")
    if any(not isinstance(factory, Mapping) for factory in value):
        raise ProtocolError("invalid_observation", "factory must be an object")
    factories = sorted(value, key=lambda factory: compact_json(factory))
    output: list[dict[str, Any]] = []
    job_omitted = 0
    for factory in factories[:MAX_FACTORIES]:
        if set(factory) != {"name", "unit_id", "jobs"}:
            raise ProtocolError("invalid_observation", "factory has unsupported fields")
        name = _public_identifier(factory.get("name"), "factory name")
        if name != scenario["factory"]["name"]:
            raise ProtocolError("invalid_observation", "unknown factory")
        unit_id = _public_count(factory.get("unit_id"), "factory unit_id")
        jobs = factory.get("jobs")
        if not isinstance(jobs, list) or any(not isinstance(job, Mapping) for job in jobs):
            raise ProtocolError("invalid_observation", "factory jobs must be a list")
        ordered_jobs = sorted(jobs, key=lambda job: compact_json(job))
        public_jobs: list[dict[str, Any]] = []
        for job in ordered_jobs[:MAX_FACTORY_JOBS]:
            if set(job) != {"job_id", "intent_id", "chassis", "controller", "started_frame", "done_frame"}:
                raise ProtocolError("invalid_observation", "factory job has unsupported fields")
            chassis = _public_identifier(job.get("chassis"), "job chassis")
            controller = _public_identifier(job.get("controller"), "job controller")
            if chassis not in scenario["chassis"] or controller not in scenario["chips"]:
                raise ProtocolError("invalid_observation", "unknown factory job vocabulary")
            public_jobs.append({
                "job_id": _public_identifier(job.get("job_id"), "job_id"),
                "intent_id": _public_identifier(job.get("intent_id"), "intent_id"),
                "chassis": chassis,
                "controller": controller,
                "started_frame": _public_count(job.get("started_frame"), "started_frame"),
                "done_frame": None if job.get("done_frame") is None else _public_count(job.get("done_frame"), "done_frame"),
            })
        job_omitted += max(0, len(jobs) - len(public_jobs))
        output.append({"name": name, "unit_id": unit_id, "jobs": public_jobs})
    return output, max(0, len(factories) - len(output)), job_omitted


def _bounded_provisioning(value: Mapping[str, Any], result: dict[str, Any], scenario_table: Mapping[str, Any] | None) -> None:
    """Add Loop 1 data only when it matches the public scenario declaration."""
    source_scenario = value.get("scenario", {})
    if not isinstance(source_scenario, Mapping):
        raise ProtocolError("invalid_observation", "scenario must be an object")
    provisioning_fields = {"commander", "budgets", "factories"}
    selected = "id" in source_scenario or "version" in source_scenario or bool(provisioning_fields & set(value))
    if not selected:
        return
    if scenario_table is None:
        raise ProtocolError("invalid_observation", "provisioning requires a selected scenario")
    declared = source_scenario.get("id") == scenario_table["id"] and source_scenario.get("version") == scenario_table["version"]
    if not declared:
        raise ProtocolError("invalid_observation", "provisioning observation lacks declared scenario")
    result.setdefault("scenario", {})["id"] = scenario_table["id"]
    result["scenario"]["version"] = scenario_table["version"]
    commander = value.get("commander")
    if commander is not None:
        if not isinstance(commander, Mapping) or set(commander) != {"unit_id", "chip"}:
            raise ProtocolError("invalid_observation", "invalid commander")
        chip = _public_identifier(commander.get("chip"), "commander chip")
        if chip not in scenario_table["chips"]:
            raise ProtocolError("invalid_observation", "unknown commander chip")
        result["commander"] = {"unit_id": _public_count(commander.get("unit_id"), "commander unit_id"), "chip": chip}
    elif "commander" in value:
        result["commander"] = None
    budgets = value.get("budgets")
    if budgets is not None:
        if not isinstance(budgets, Mapping) or set(budgets) != {"bodies_remaining", "chips"} or not isinstance(budgets.get("chips"), Mapping):
            raise ProtocolError("invalid_observation", "invalid budgets")
        chips = budgets["chips"]
        if len(chips) > 16 or set(chips) - set(scenario_table["chips"]):
            raise ProtocolError("invalid_observation", "unknown chip budget")
        result["budgets"] = {
            "bodies_remaining": _public_count(budgets.get("bodies_remaining"), "bodies_remaining"),
            "chips": {name: _public_count(chips[name], f"chips.{name}") for name in sorted(chips)},
        }
    factories, factory_omitted, job_omitted = _bounded_factory_observation(value.get("factories"), scenario_table)
    if "factories" in value:
        result["factories"] = factories
        result["omitted"]["factories"] = factory_omitted
        result["omitted"]["factory_jobs"] = job_omitted
        result["truncated"] = result["truncated"] or factory_omitted > 0 or job_omitted > 0


def _bounded_entities(value: list[Any], scenario_table: Mapping[str, Any] | None) -> list[Any]:
    """Preserve the Loop 0 entity view while filtering the new identity field."""
    public: list[Any] = []
    controllers = frozenset() if scenario_table is None else frozenset(scenario_table["chips"])
    for entity in value:
        if not isinstance(entity, Mapping):
            public.append(entity)
            continue
        projected = dict(entity)
        if "controller" in projected:
            if projected.get("source") != "own_unit":
                projected.pop("controller")
            elif not isinstance(projected["controller"], str) or projected["controller"] not in controllers:
                raise ProtocolError("invalid_observation", "unknown entity controller")
        public.append(projected)
    return public


def bounded_observation(value: Mapping[str, Any], *, scenario_table: Mapping[str, Any] | None = None) -> dict[str, Any]:
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
    clipped_entities = _bounded_entities(ordered_entities[:MAX_ENTITIES], scenario_table)
    clipped_receipts = ordered_receipts[-MAX_RECENT_EVENTS:]
    clipped_effects = ordered_effects[-MAX_RECENT_EVENTS:]
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
    _bounded_provisioning(value, result, scenario_table)
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
            result["permitted_intents"].append({"type": "TASK", "verbs": ["advance"]})
            continue
        if intent.get("type") == "PRODUCE":
            factories = intent.get("factories", [])
            chassis = intent.get("chassis", [])
            controllers = intent.get("controllers", [])
            if not (isinstance(factories, list) and len(factories) <= 4 and isinstance(chassis, list) and len(chassis) <= 8 and isinstance(controllers, list) and len(controllers) <= 8):
                raise ProtocolError("invalid_observation", "invalid PRODUCE permission")
            if scenario_table is None:
                raise ProtocolError("invalid_observation", "PRODUCE permission requires a selected scenario")
            table = scenario_table
            if not all(isinstance(item, str) for item in [*factories, *chassis, *controllers]):
                raise ProtocolError("invalid_observation", "invalid PRODUCE permission")
            public_factories = sorted({item for item in factories if item == table["factory"]["name"]})
            public_chassis = sorted({item for item in chassis if item in table["chassis"]})
            public_controllers = sorted({item for item in controllers if item in table["chips"]})
            if public_factories and public_chassis and public_controllers:
                result["permitted_intents"].append({
                    "type": "PRODUCE",
                    "factories": public_factories,
                    "chassis": public_chassis,
                    "controllers": public_controllers,
                })
    if len(compact_json(result)) > MAX_OBSERVATION_BYTES:
        raise ProtocolError("observation_too_large", "transport observation exceeds public cap")
    return result
