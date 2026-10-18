"""Load the public, versioned scenario table shared with the Lua gadget."""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


SCENARIO_SCHEMA = "ScenarioTable/v1"
DEFAULT_SCENARIO = "rescue_smoke"
_SCENARIO_DIRECTORY = Path(__file__).resolve().parents[1] / "game" / "SynapseCommander.sdd" / "scenarios"


class ScenarioError(ValueError):
    """The checked-in scenario declaration is not a safe public vocabulary."""


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and value and len(value) <= 64 and all(
        char.isascii() and (char.isalnum() or char in "_.-") for char in value
    )


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ScenarioError(f"{field} must be an object")
    return value


def _coordinates(value: Any, field: str, length: int) -> None:
    if not isinstance(value, list) or len(value) != length or any(type(number) not in (int, float) or not math.isfinite(number) for number in value):
        raise ScenarioError(f"{field} must contain finite numeric coordinates")


def _speed_factor(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0.05 <= value <= 1.0


ENTRAPMENT_UNTIL = ("parts_delivered",)  # "towed" is reserved for mission v2.1 and refused until implemented


def _validate_beacon_fix(value: Any) -> bool:
    """Validate the public, deliberately imprecise search clue for §10."""
    if not isinstance(value, Mapping):
        return False
    kind = value.get("kind")
    if kind == "bearing":
        error = value.get("error_deg")
        return (set(value) == {"kind", "from", "error_deg"}
                and value.get("from") == "commander"
                and type(error) in (int, float) and math.isfinite(error)
                and 0 <= error <= 180)
    if kind == "quadrant":
        cell = value.get("cell_elmos")
        return (set(value) == {"kind", "cell_elmos"}
                and _non_negative_int(cell) and cell > 0)
    return False


def _validate_mission_v2(root: Mapping[str, Any], chassis: Mapping[str, Any]) -> None:
    """Mission v2 (kbk Design 9): speed factors, parts stock and parts-delivery entrapment, all optional."""
    for asset in root.get("predeployed") or []:
        if isinstance(asset, Mapping) and "speed_factor" in asset and not _speed_factor(asset["speed_factor"]):
            raise ScenarioError("invalid predeployed speed factor")
    if "resources" in root:
        resources = _mapping(root["resources"], "resources")
        if not set(resources) <= {"parts"} or not _non_negative_int(resources.get("parts", 0)):
            raise ScenarioError("invalid resources")
    if "entrapment" not in root:
        return
    entrapment = _mapping(root["entrapment"], "entrapment")
    names = {a.get("name"): a for a in root.get("predeployed") or [] if isinstance(a, Mapping)}
    carriers = entrapment.get("parts_carrier_chassis")
    radius, frames = entrapment.get("assist_range"), entrapment.get("assist_frames")
    required = {"asset", "until", "assist_capability", "assist_range", "assist_frames", "parts_carrier_chassis"}
    beacon_only = entrapment.get("beacon_only", False)
    if beacon_only:
        required |= {"beacon_only", "beacon_fix"}
    if set(entrapment) != required \
            or entrapment["until"] not in ENTRAPMENT_UNTIL \
            or entrapment["asset"] not in names or names[entrapment["asset"]].get("state") != "active" \
            or not _identifier(entrapment["assist_capability"]) \
            or type(radius) not in (int, float) or not math.isfinite(radius) or radius <= 0 \
            or not _non_negative_int(frames) or frames == 0 \
            or not isinstance(carriers, list) or not carriers or any(c not in chassis or chassis[c].get("carries_parts") is not True for c in carriers) or len(set(carriers)) != len(carriers) \
            or ("beacon_only" in entrapment and beacon_only is not True) \
            or (beacon_only and not _validate_beacon_fix(entrapment.get("beacon_fix"))):
        raise ScenarioError("invalid entrapment")
    if "exposure" in root:
        exposure = _mapping(root["exposure"], "exposure")
        decay, floor = exposure.get("decay_per_minute"), exposure.get("floor")
        if set(exposure) != {"asset", "decay_per_minute", "floor"} \
                or exposure.get("asset") not in names or names[exposure["asset"]].get("state") != "active" \
                or type(decay) not in (int, float) or not math.isfinite(decay) or not 0 < decay <= 1 \
                or type(floor) not in (int, float) or not math.isfinite(floor) or not 0 <= floor <= 1:
            raise ScenarioError("invalid exposure")


def _validate(table: Any, scenario_name: str) -> dict[str, Any]:
    root = _mapping(table, "scenario")
    required = {"schema", "id", "version", "factory", "bodies", "chips", "commander", "chassis", "waypoints", "objective"}
    if not required <= set(root) or root.get("schema") != SCENARIO_SCHEMA or root.get("id") != scenario_name or not _identifier(root.get("version")):
        raise ScenarioError("unsupported scenario table")
    factory = _mapping(root["factory"], "factory")
    chassis = _mapping(root["chassis"], "chassis")
    chips = _mapping(root["chips"], "chips")
    commander = _mapping(root["commander"], "commander")
    waypoints = _mapping(root["waypoints"], "waypoints")
    objective = _mapping(root["objective"], "objective")
    if set(factory) != {"name", "def", "offset"} or not _identifier(factory.get("name")) or not _identifier(factory.get("def")):
        raise ScenarioError("invalid factory definition")
    _coordinates(factory["offset"], "factory offset", 2)
    if not _non_negative_int(root.get("bodies")) or not (1 <= len(chassis) <= 8 and 1 <= len(chips) <= 8):
        raise ScenarioError("scenario table exceeds public limits")
    if any(not _identifier(name) for name in (*chassis, *chips)):
        raise ScenarioError("scenario table has invalid identifier")
    for name, item in chassis.items():
        item = _mapping(item, f"chassis.{name}")
        if not {"def", "build_frames", "commission_frames"} <= set(item) <= {"def", "build_frames", "commission_frames", "capabilities", "speed_factor", "carries_parts"} or not _identifier(item.get("def")) or not _non_negative_int(item.get("build_frames")) or not _non_negative_int(item.get("commission_frames")):
            raise ScenarioError("invalid chassis definition")
        if ("speed_factor" in item and not _speed_factor(item["speed_factor"])) or ("carries_parts" in item and not isinstance(item["carries_parts"], bool)):
            raise ScenarioError("invalid chassis definition")
    if any(not _non_negative_int(amount) for amount in chips.values()):
        raise ScenarioError("invalid chip inventory")
    if not {"chip"} <= set(commander) <= {"chip", "position", "capabilities", "speed_factor"} or commander.get("chip") not in chips \
            or ("speed_factor" in commander and not _speed_factor(commander["speed_factor"])):
        raise ScenarioError("invalid commander")
    if "position" in commander:
        _coordinates(commander["position"], "commander position", 2)
    if "W1" not in waypoints or len(waypoints) > 8:
        raise ScenarioError("missing public waypoint")
    for point in waypoints.values():
        _coordinates(point, "waypoint", 3)
    if set(objective) != {"type", "target", "extraction"} or any(not _identifier(value) for value in objective.values()):
        raise ScenarioError("invalid objective")
    if "extraction_zones" in root:
        zones = _mapping(root["extraction_zones"], "extraction_zones")
        if not 1 <= len(zones) <= 8 or any(not _identifier(name) for name in zones):
            raise ScenarioError("invalid extraction zones")
        for zone in zones.values():
            zone = _mapping(zone, "extraction zone")
            _coordinates(zone.get("position"), "extraction zone position", 2)
            radius, dwell = zone.get("radius"), zone.get("dwell_frames")
            if set(zone) != {"position", "radius", "dwell_frames"} or type(radius) not in (int, float) or not math.isfinite(radius) or radius <= 0 or not _non_negative_int(dwell) or dwell == 0:
                raise ScenarioError("invalid extraction zone")
        # A physical extraction objective names a declared zone and a bound principal.
        if objective["extraction"] not in zones or objective["target"] not in _mapping(root.get("principals"), "principals"):
            raise ScenarioError("objective names an undeclared extraction zone or principal")
    if "mission" in root:
        mission = _mapping(root["mission"], "mission")
        if set(mission) != {"timeout_frames"} or not _non_negative_int(mission["timeout_frames"]) or mission["timeout_frames"] == 0:
            raise ScenarioError("invalid mission")
    _validate_mission_v2(root, chassis)
    if "briefing" in root:
        briefing = _mapping(root["briefing"], "briefing")
        if set(briefing) != {"title", "text"} or any(
            not isinstance(briefing.get(key), str) or not briefing[key].isprintable()
            or not 1 <= len(briefing[key]) <= limit
            for key, limit in (("title", 80), ("text", 600))
        ):
            raise ScenarioError("invalid public briefing")
    return root


@lru_cache(maxsize=8)
def load_scenario(name: str = DEFAULT_SCENARIO) -> dict[str, Any]:
    """Return one checked-in scenario table, never a caller-selected path."""
    if not _identifier(name):
        raise ScenarioError("invalid scenario name")
    path = _SCENARIO_DIRECTORY / f"{name}.json"
    try:
        raw = path.read_bytes()
        table = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScenarioError("scenario table unavailable") from error
    if len(raw) > 16 * 1024:
        raise ScenarioError("scenario table too large")
    return _validate(table, name)


def supported_chassis() -> frozenset[str]:
    """Finite chassis vocabulary from the shared Loop 1 scenario table."""
    return frozenset(load_scenario()["chassis"])


def supported_controllers() -> frozenset[str]:
    """Controller IDs are exactly the declared inventory chip IDs."""
    return frozenset(load_scenario()["chips"])
