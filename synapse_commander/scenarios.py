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
        if not {"def", "build_frames", "commission_frames"} <= set(item) <= {"def", "build_frames", "commission_frames", "capabilities"} or not _identifier(item.get("def")) or not _non_negative_int(item.get("build_frames")) or not _non_negative_int(item.get("commission_frames")):
            raise ScenarioError("invalid chassis definition")
    if any(not _non_negative_int(amount) for amount in chips.values()):
        raise ScenarioError("invalid chip inventory")
    if not {"chip"} <= set(commander) <= {"chip", "position", "capabilities"} or commander.get("chip") not in chips:
        raise ScenarioError("invalid commander")
    if "position" in commander:
        _coordinates(commander["position"], "commander position", 2)
    if "W1" not in waypoints or len(waypoints) > 8:
        raise ScenarioError("missing public waypoint")
    for point in waypoints.values():
        _coordinates(point, "waypoint", 3)
    if set(objective) != {"type", "target", "extraction"} or any(not _identifier(value) for value in objective.values()):
        raise ScenarioError("invalid objective")
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
