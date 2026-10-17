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
MAX_REPORTS = 32

INTENT_TYPES = frozenset({"PRODUCE", "TASK", "INSTALL", "MESSAGE"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_REQUIRED_PAYLOAD_FIELDS = {
    "PRODUCE": frozenset({"factory", "chassis", "controller", "parameters"}),
    "TASK": frozenset({"unit", "verb", "args"}),
    "INSTALL": frozenset({"unit", "expected_version", "replacement_version", "parameters"}),
    "MESSAGE": frozenset({"to", "text"}),
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
    def from_untrusted(cls, value: Any, *, principal: str, team: str, chassis: frozenset[str] | None = None, controllers: frozenset[str] | None = None, principals: frozenset[str] | None = None) -> "IntentEnvelope":
        """Parse a client request and inject, rather than trust, identity."""
        if not isinstance(value, dict):
            raise ProtocolError("malformed_json", "body must be a JSON object")
        allowed = {"schema", "intent_id", "based_on_observation_id", "expires_at_frame", "type", "payload", "principal", "team"}
        unknown = set(value) - allowed
        if unknown:
            raise ProtocolError("unknown_field", "unsupported envelope field")
        if value.get("schema") != INTENT_SCHEMA_VERSION:
            raise ProtocolError("unsupported_version", "unsupported intent schema")
        if principals is not None and value.get("principal") != principal:
            # In a multi-principal scenario the client must name the identity
            # which its Bearer session is bound to.  The gateway still injects
            # that identity into the wire envelope; this check makes missing
            # and forged client identities equally non-ambiguous.
            raise ProtocolError("principal_mismatch", "session principal does not match envelope")
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
        for field in required - {"parameters", "args", "text"}:
            _bounded_string(payload[field], field)
        if intent_type == "MESSAGE":
            if principals is None:
                raise ProtocolError("intent_not_allowed", "MESSAGE requires a principal scenario")
            if payload["to"] not in principals:
                raise ProtocolError("unknown_principal", "unknown message recipient")
            if not isinstance(payload["text"], str) or len(payload["text"]) > 256:
                raise ProtocolError("invalid_parameters", "message text must be at most 256 characters")
        if intent_type == "PRODUCE" and chassis is not None and controllers is not None:
            if payload["chassis"] not in chassis:
                raise ProtocolError("unknown_chassis", "unknown chassis")
            if payload["controller"] not in controllers:
                raise ProtocolError("unknown_controller", "unknown controller")
        if intent_type == "INSTALL" and controllers is not None:
            if payload["expected_version"] not in controllers or payload["replacement_version"] not in controllers:
                raise ProtocolError("unknown_controller", "unknown controller version")
        if intent_type == "TASK":
            args = _bounded_object(payload["args"], "args")
            if payload["verb"] == "advance":
                if args:
                    raise ProtocolError("invalid_parameters", "advance requires args is empty")
            elif payload["verb"] == "move_to" and principals is not None:
                if (set(args) != {"x", "z"}
                        or any(not isinstance(args[axis], int) or isinstance(args[axis], bool)
                               for axis in ("x", "z"))):
                    raise ProtocolError("invalid_parameters", "move_to requires integer x and z")
                if any(not 0 <= args[axis] <= 65536 for axis in ("x", "z")):
                    raise ProtocolError("invalid_parameters", "move_to coordinates are outside the map bounds")
            else:
                raise ProtocolError("verb_not_supported", "task verb is not exposed for this principal")
        for field in required & {"parameters", "args"}:
            _bounded_object(payload[field], field)
            if field == "args" and intent_type == "TASK":
                continue
            if payload[field]:
                # The smoke schema deliberately has no controller-specific
                # argument vocabulary. Broken Relay parameters belong in its
                # later engine adapter, not an untyped escape hatch here.
                raise ProtocolError("invalid_parameters", f"{field} is empty in the smoke schema")
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
        if set(factory) not in ({"name", "unit_id"}, {"name", "unit_id", "jobs"}):
            raise ProtocolError("invalid_observation", "factory has unsupported fields")
        name = _public_identifier(factory.get("name"), "factory name")
        if name != scenario["factory"]["name"]:
            raise ProtocolError("invalid_observation", "unknown factory")
        # A destroyed or never-created factory has no live engine identity.
        # Preserve that explicit public condition instead of turning every
        # later observation into a gateway failure.
        raw_unit_id = factory.get("unit_id")
        unit_id = None if raw_unit_id is None else _public_count(raw_unit_id, "factory unit_id")
        # A principal view may expose the factory identity without granting its
        # private queue.  Its public projection is an explicitly empty queue;
        # the richer Loop 1 view still has to supply every bounded job field.
        jobs = factory.get("jobs", [])
        if not isinstance(jobs, list) or any(not isinstance(job, Mapping) for job in jobs):
            raise ProtocolError("invalid_observation", "factory jobs must be a list")
        def job_key(job: Mapping[str, Any]) -> tuple[int, bytes]:
            started = job.get("started_frame")
            return (started if isinstance(started, int) and not isinstance(started, bool) else -1, compact_json(job))
        ordered_jobs = sorted(jobs, key=job_key)
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
    fields = frozenset({
        "id", "pos", "source", "team", "observed_frame", "age_frames",
        "def", "health", "max_health", "commands", "controller", "sensor_range", "link",
    })
    for entity in value:
        if not isinstance(entity, Mapping):
            public.append(entity)
            continue
        projected = {key: item for key, item in entity.items() if key in fields}
        if "def" in projected:
            projected["def"] = _public_identifier(projected["def"], "entity def")
        for field in ("health", "max_health"):
            if field in projected:
                amount = projected[field]
                if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
                    raise ProtocolError("invalid_observation", f"entity {field} must be finite and non-negative")
        if "commands" in projected:
            projected["commands"] = _public_count(projected["commands"], "entity commands")
        if "sensor_range" in projected:
            radius = projected["sensor_range"]
            if type(radius) not in (int, float) or not math.isfinite(radius) or radius < 0:
                raise ProtocolError("invalid_observation", "entity sensor_range must be finite and non-negative")
            if projected.get("source") != "own_body":
                projected.pop("sensor_range")
        if "link" in projected:
            if not isinstance(projected["link"], bool):
                raise ProtocolError("invalid_observation", "entity link must be boolean")
            if projected.get("source") != "own_body":
                projected.pop("link")
        if "controller" in projected:
            if scenario_table is None or projected.get("source") not in {"own_unit", "own_body"}:
                projected.pop("controller")
            elif not isinstance(projected["controller"], str) or projected["controller"] not in controllers:
                raise ProtocolError("invalid_observation", "unknown entity controller")
        if projected.get("source") not in {"own_unit", "own_body"}:
            for field in ("health", "max_health", "commands"):
                projected.pop(field, None)
        public.append(projected)
    return public


def _bounded_principal_reports(value: Mapping[str, Any], result: dict[str, Any],
                               scenario_table: Mapping[str, Any] | None) -> None:
    """Expose only delivered, typed reports in an embodied principal stream."""
    reports = value.get("reports", [])
    if not isinstance(reports, list):
        raise ProtocolError("invalid_observation", "reports must be a list")
    public: list[dict[str, Any]] = []
    for report in reports[-MAX_REPORTS:]:
        if not isinstance(report, Mapping):
            raise ProtocolError("invalid_observation", "report must be an object")
        kind = report.get("kind")
        observed = report.get("observed_frame")
        delivered = report.get("delivered_frame")
        age = report.get("age_frames")
        if (not isinstance(kind, str) or kind not in {"status", "detection", "message", "distress"}
                or not isinstance(observed, int) or isinstance(observed, bool)
                or not isinstance(age, int) or isinstance(age, bool) or age < 0):
            raise ProtocolError("invalid_observation", "invalid delivered report")
        if kind != "distress" and (not isinstance(delivered, int) or isinstance(delivered, bool) or delivered < 0):
            raise ProtocolError("invalid_observation", "invalid report delivery frame")
        if kind in {"status", "detection", "message"}:
            expected = ({"kind", "from", "observed_frame", "delivered_frame", "age_frames", "body"}
                        if kind == "status" else
                        {"kind", "from", "observed_frame", "delivered_frame", "age_frames", "entity"}
                        if kind == "detection" else
                        {"kind", "from", "text", "observed_frame", "delivered_frame", "age_frames"})
            if set(report) != expected:
                raise ProtocolError("invalid_observation", "report has unsupported fields")
            item: dict[str, Any] = {"kind": kind, "from": _public_identifier(report.get("from"), "report source"),
                                    "observed_frame": observed, "delivered_frame": delivered, "age_frames": age}
            if kind == "message":
                text = report.get("text")
                if not isinstance(text, str) or len(text) > 256:
                    raise ProtocolError("invalid_observation", "invalid message report")
                item["text"] = text
            else:
                entity = report.get("body" if kind == "status" else "entity")
                projected = _bounded_entities([entity], scenario_table)
                if len(projected) != 1 or not isinstance(projected[0], dict):
                    raise ProtocolError("invalid_observation", "invalid report entity")
                item["body" if kind == "status" else "entity"] = projected[0]
            public.append(item)
            continue
        # The commander's initial distress report is deliberately a distinct,
        # public report type.  It never contains the hidden cause or truth.
        if scenario_table is None or set(report) != {"kind", "asset", "position", "observed_frame", "age_frames", "symptom"}:
            raise ProtocolError("invalid_observation", "invalid distress report")
        distress = scenario_table.get("distress", {})
        position = report.get("position")
        if (report.get("asset") != distress.get("asset") or report.get("symptom") != distress.get("symptom")
                or not isinstance(position, list) or len(position) != 3
                or any(type(n) not in (int, float) or not math.isfinite(n) for n in position)):
            raise ProtocolError("invalid_observation", "invalid distress report")
        public.append({"kind": "distress", "asset": report["asset"], "position": list(position),
                       "observed_frame": observed, "age_frames": age, "symptom": report["symptom"]})
    result["reports"] = public
    result["omitted"]["reports"] = max(0, len(reports) - len(public))
    result["truncated"] = result["truncated"] or len(reports) > len(public)


def _bounded_principal_body(value: Mapping[str, Any], result: dict[str, Any],
                            scenario_table: Mapping[str, Any] | None) -> None:
    body = value.get("body")
    if not isinstance(body, Mapping):
        raise ProtocolError("invalid_observation", "principal observation needs an own body")
    projected = _bounded_entities([body], scenario_table)
    if len(projected) != 1 or not isinstance(projected[0], dict) or projected[0].get("source") != "own_body":
        raise ProtocolError("invalid_observation", "invalid principal own body")
    result["body"] = projected[0]
    _bounded_principal_reports(value, result, scenario_table)
    if "link_state" in value:
        links = value["link_state"]
        if not isinstance(links, list) or len(links) > MAX_ENTITIES:
            raise ProtocolError("invalid_observation", "invalid link state")
        public_links = []
        for link in links:
            if not isinstance(link, Mapping) or set(link) != {"unit_id", "linked"}:
                raise ProtocolError("invalid_observation", "invalid link state entry")
            if not isinstance(link.get("linked"), bool):
                raise ProtocolError("invalid_observation", "invalid link state entry")
            public_links.append({"unit_id": _public_count(link.get("unit_id"), "link unit_id"),
                                 "linked": link["linked"]})
        result["link_state"] = sorted(public_links, key=lambda item: item["unit_id"])


def _bounded_expedition(value: Mapping[str, Any], result: dict[str, Any], scenario_table: Mapping[str, Any] | None) -> None:
    """Project only the public roster and explicitly delivered distress reports."""
    if "roster" not in value and "reports" not in value:
        return
    if scenario_table is None:
        raise ProtocolError("invalid_observation", "expedition data requires a selected scenario")
    predeployed = scenario_table.get("predeployed", [])
    distress = scenario_table.get("distress", {})
    if not isinstance(predeployed, list) or not isinstance(distress, Mapping):
        raise ProtocolError("invalid_observation", "selected scenario has no expedition declaration")
    if len(predeployed) > 8:
        raise ProtocolError("invalid_observation", "expedition declaration exceeds roster limit")
    known = {item.get("name"): item for item in predeployed if isinstance(item, Mapping) and isinstance(item.get("name"), str)}
    if (len(known) != len(predeployed)
            or any(not _IDENTIFIER.fullmatch(name) for name in known)
            or not isinstance(distress.get("symptom"), str)
            or not _IDENTIFIER.fullmatch(distress["symptom"])):
        raise ProtocolError("invalid_observation", "invalid expedition declaration")
    roster = value.get("roster", [])
    if not isinstance(roster, list) or len(roster) > 8:
        raise ProtocolError("invalid_observation", "invalid roster")
    public_roster: list[dict[str, Any]] = []
    roster_names: set[str] = set()
    for item in roster:
        if not isinstance(item, Mapping):
            raise ProtocolError("invalid_observation", "invalid roster entry")
        status = item.get("status")
        expected = {"name", "def", "controller", "status", "unit_id"} if status == "in_contact" else {"name", "def", "controller", "status"}
        if set(item) != expected or status not in {"in_contact", "no_contact"}:
            raise ProtocolError("invalid_observation", "invalid roster entry")
        name = _public_identifier(item.get("name"), "roster name")
        if name in roster_names:
            raise ProtocolError("invalid_observation", "duplicate roster name")
        roster_names.add(name)
        declared = known.get(name)
        if (declared is None
                or not isinstance(declared.get("def"), str)
                or not _IDENTIFIER.fullmatch(declared["def"])
                or (declared.get("controller") is not None
                    and (not isinstance(declared["controller"], str)
                         or not _IDENTIFIER.fullmatch(declared["controller"])))
                or item.get("def") != declared.get("def")
                or item.get("controller") != declared.get("controller")):
            raise ProtocolError("invalid_observation", "unknown roster entry")
        public = {"name": name, "def": item["def"], "controller": item["controller"], "status": status}
        if status == "in_contact":
            public["unit_id"] = _public_count(item.get("unit_id"), "roster unit_id")
        public_roster.append(public)
    reports = value.get("reports", [])
    if not isinstance(reports, list) or len(reports) > 16:
        raise ProtocolError("invalid_observation", "invalid reports")
    public_reports: list[dict[str, Any]] = []
    for report in reports:
        if not isinstance(report, Mapping) or set(report) != {"kind", "asset", "position", "observed_frame", "age_frames", "symptom"}:
            raise ProtocolError("invalid_observation", "invalid report")
        asset = _public_identifier(report.get("asset"), "report asset")
        position = report.get("position")
        observed = report.get("observed_frame")
        if report.get("kind") != "distress" or asset not in known or report.get("symptom") != distress.get("symptom") or not isinstance(position, list) or len(position) != 3 or any(type(number) not in (int, float) or not math.isfinite(number) for number in position) or not isinstance(observed, int) or isinstance(observed, bool):
            raise ProtocolError("invalid_observation", "invalid report")
        public_reports.append({"kind": "distress", "asset": asset, "position": list(position), "observed_frame": observed, "age_frames": _public_count(report.get("age_frames"), "age_frames"), "symptom": report["symptom"]})
    result["roster"] = sorted(public_roster, key=lambda item: item["name"])
    result["reports"] = sorted(public_reports, key=compact_json)


def bounded_observation(value: Mapping[str, Any], *, scenario_table: Mapping[str, Any] | None = None, principal: str | None = None) -> dict[str, Any]:
    """Normalize a port result into the small, bounded public smoke schema."""
    if not isinstance(value, Mapping):
        raise ProtocolError("invalid_observation", "transport returned no observation object")
    observation_id = _bounded_string(value.get("observation_id"), "observation_id")
    if principal is not None and value.get("principal") != principal:
        raise ProtocolError("invalid_observation", "principal stream mismatch")
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
    if principal is not None:
        result["principal"] = principal
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
    if principal == "commander" and "objective" in value:
        objective = value["objective"]
        declared = None if scenario_table is None else scenario_table.get("objective")
        if (not isinstance(objective, Mapping) or set(objective) != {"type", "target", "status"}
                or not isinstance(declared, Mapping)
                or objective.get("type") != declared.get("type")
                or objective.get("target") != declared.get("target")
                or objective.get("status") not in {"pending", "extracted", "lost", "timeout"}):
            raise ProtocolError("invalid_observation", "invalid commander objective")
        result["objective"] = {"type": objective["type"], "target": objective["target"],
                               "status": objective["status"]}
    if principal == "commander" and "extraction" in scenario:
        extraction = scenario["extraction"]
        declared = None if scenario_table is None else scenario_table.get("objective")
        if (not isinstance(extraction, Mapping) or set(extraction) != {"id", "position", "radius"}
                or not isinstance(declared, Mapping) or extraction.get("id") != declared.get("extraction")
                or not isinstance(extraction.get("position"), list) or len(extraction["position"]) != 2
                or any(type(number) not in (int, float) or not math.isfinite(number)
                       for number in extraction["position"])
                or type(extraction.get("radius")) not in (int, float)
                or not math.isfinite(extraction["radius"]) or extraction["radius"] <= 0):
            raise ProtocolError("invalid_observation", "invalid extraction scenario")
        result.setdefault("scenario", {})["extraction"] = {
            "id": extraction["id"], "position": list(extraction["position"]), "radius": extraction["radius"],
        }
    _bounded_provisioning(value, result, scenario_table)
    if principal is None:
        _bounded_expedition(value, result, scenario_table)
    else:
        # The engine's pre-run distress item is historical: it has an observed
        # frame but no separate delivery timestamp.  Its age is public and
        # derivable from this observation frame, so add only that bounded value
        # before handing reports to the strict typed projection.
        principal_value: Mapping[str, Any] = value
        reports = value.get("reports")
        if isinstance(reports, list):
            normalized_reports = []
            for report in reports:
                # The engine records delivery timing for the initial distress
                # packet too.  It is bounded provenance, not part of the
                # public historical-report shape; validate it and remove it
                # before the shared roster and principal projections enforce
                # their exact public fields.
                if isinstance(report, Mapping) and report.get("kind") == "distress" and "delivered_frame" in report:
                    delivered = report.get("delivered_frame")
                    if (set(report) != {"kind", "asset", "position", "observed_frame", "delivered_frame",
                                       "age_frames", "symptom"}
                            or not isinstance(delivered, int) or isinstance(delivered, bool)
                            or delivered < 0 or delivered > frame):
                        raise ProtocolError("invalid_observation", "invalid distress delivery frame")
                    report = {key: item for key, item in report.items() if key != "delivered_frame"}
                if (isinstance(report, Mapping) and report.get("kind") == "distress"
                        and "age_frames" not in report and isinstance(report.get("observed_frame"), int)
                        and not isinstance(report.get("observed_frame"), bool)):
                    normalized_reports.append({**report, "age_frames": max(0, frame - report["observed_frame"])})
                else:
                    normalized_reports.append(report)
            principal_value = {**value, "reports": normalized_reports}
        # Commander roster is a separately declared public inventory view;
        # leave richer delivered reports to the principal-report projection.
        if "roster" in principal_value:
            expedition = dict(principal_value)
            expedition["reports"] = [report for report in principal_value.get("reports", [])
                                      if isinstance(report, Mapping) and report.get("kind") == "distress"]
            _bounded_expedition(expedition, result, scenario_table)
        _bounded_principal_body(principal_value, result, scenario_table)
    permitted = value.get("permitted_intents", [])
    if not isinstance(permitted, list) or len(permitted) > 16:
        raise ProtocolError("invalid_observation", "invalid permitted intents")
    result["permitted_intents"] = []
    seen_permissions: set[str] = set()
    for intent in permitted:
        if not isinstance(intent, Mapping):
            raise ProtocolError("invalid_observation", "invalid permitted intent")
        verbs = intent.get("verbs", [])
        if not isinstance(verbs, list):
            raise ProtocolError("invalid_observation", "invalid permitted verbs")
        if intent.get("type") == "TASK":
            public_verbs = [verb for verb in ("advance", "move_to")
                            if verb in verbs and (verb != "move_to" or principal is not None)]
            if not public_verbs:
                continue
            if "TASK" not in seen_permissions:
                result["permitted_intents"].append({"type": "TASK", "verbs": public_verbs})
                seen_permissions.add("TASK")
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
            if (any(item != table["factory"]["name"] for item in factories)
                    or any(item not in table["chassis"] for item in chassis)
                    or any(item not in table["chips"] for item in controllers)):
                raise ProtocolError("invalid_observation", "unknown PRODUCE permission identifier")
            public_factories = sorted({item for item in factories if item == table["factory"]["name"]})
            public_chassis = sorted({item for item in chassis if item in table["chassis"]})
            public_controllers = sorted({item for item in controllers if item in table["chips"]})
            if public_factories and public_chassis and public_controllers and "PRODUCE" not in seen_permissions:
                result["permitted_intents"].append({
                    "type": "PRODUCE",
                    "factories": public_factories,
                    "chassis": public_chassis,
                    "controllers": public_controllers,
                })
                seen_permissions.add("PRODUCE")
            continue
        if intent.get("type") == "MESSAGE" and principal is not None:
            if set(intent) != {"type"}:
                raise ProtocolError("invalid_observation", "invalid MESSAGE permission")
            if "MESSAGE" not in seen_permissions:
                result["permitted_intents"].append({"type": "MESSAGE"})
                seen_permissions.add("MESSAGE")
    if len(compact_json(result)) > MAX_OBSERVATION_BYTES:
        raise ProtocolError("observation_too_large", "transport observation exceeds public cap")
    return result
