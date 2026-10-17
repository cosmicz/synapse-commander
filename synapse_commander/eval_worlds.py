"""Seeded evaluation worlds for the rescue scenario family (rah26-wkz).

``generate(seed)`` returns a ScenarioTable/v1 table and a separate
evaluator truth record for one world. Generation is deterministic: the same
generator version, rule version and seed give byte-identical output. The
world is fixed before play; nothing here runs during a mission.

Three target states are sampled from a declared distribution:

- ``present_stranded``: the expedition mind M1 is deployed, alive, out of
  contact, impaired and trapped; its aged distress report is the team's only
  knowledge of it.
- ``destroyed_before_run``: M1 was deployed and lost before frame 0. It has
  no principal and no body; the team holds the packet it sent before that.
- ``never_deployed``: no reasoning mind was ever sent. A fixed-program bot E1
  raised the alarm; the unused reasoner chip stays in the inventory.

Every position is one of the forward-base anchors measured natively for
rah26-68j (map-centre offsets, except commander.position which is relative
to the factory), and contact flags must agree with the measured link facts.
Only target state and a few bounded non-geometric declarations vary in this
first slice. Participant-visible identifiers (id, version) never name the
state or seed; the state, cause and selection metadata live only in the
truth record. Tables are stored for rehearsal and offline reporting: the
engine loader selects top-level scenario names only, so these are not yet
runnable by ``--scenario``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

GENERATOR_VERSION = "eval-worlds/1"
RULE_VERSION = "rescue-eval-rule/1"
TRUTH_SCHEMA = "EvalWorldTruth/v1"
FAMILY_ID, FAMILY_VERSION = "rescue_eval", "rule-1"

TARGET_STATES = ("present_stranded", "destroyed_before_run", "never_deployed")
# Declared sampling distribution (weights) over target states.
STATE_WEIGHTS = {"present_stranded": 2, "destroyed_before_run": 1, "never_deployed": 1}
# Supported cause for each state; other causes (enemy, storm, wildlife) have
# no mechanics yet and are not offered.
CAUSES = {"present_stranded": "terrain_entrapment", "destroyed_before_run": "pre_run_loss",
          "never_deployed": "fixed_controller_alarm"}
DISTRESS_AGES = (600, 900, 1200)        # frames since the stranded report was sent
STRANDED_HEALTH = (0.3, 0.4, 0.5)
DESTROYED_PACKET_AGES = (1800, 2700)    # the packet predates the destruction
DESTRUCTION_LAG = (300, 600)            # frames between that packet and the loss

# Portable pins: the application fixture these anchors come from. The native
# measurements behind them are pinned and cross-checked on the evaluator side.
SOURCE_PINS = {
    "fixture": "game/SynapseCommander.sdd/scenarios/rescue_baseline.json",
    "fixture_commit": "e57e680a29f738010c4e3a5603bfe8898ac15078",
    "fixture_sha256": "2e07e5b5a364d5c2eb2d849115cbc72e1fbd1f57ca7ea89d64d6b46b08656ec5",
}
ANCHORS = {
    "factory": (-512, 896), "S0": (-332, 576), "target": (-1536, 2176), "last_known": (-1312, 2176),
    "H0": (-1792, 2048), "extraction": (-752, 896), "corridor_turn": (-512, 1536), "relay_corner": (-512, 2176),
}
COMMANDER_FROM_FACTORY = (0, 240)
RADIO_RANGE = 1400
# Measured segments from the factory and commander: distance and native terrain clearance.
LINK_FACTS = {
    "target": {"from_factory": 1639.2, "from_commander": 1459.51, "terrain_clear": False},
    "S0": {"terrain_clear": True},
}
# Content pin for the geometry above; the evaluator side maps it to native evidence.
GEOMETRY_SHA256 = hashlib.sha256(json.dumps(
    {"anchors": ANCHORS, "commander_from_factory": COMMANDER_FROM_FACTORY, "radio_range": RADIO_RANGE,
     "link_facts": LINK_FACTS}, sort_keys=True).encode()).hexdigest()


SOURCE_PINS["geometry_sha256"] = GEOMETRY_SHA256


class EvalWorldError(ValueError):
    """A generated or supplied world violates the evaluation rules."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2) + "\n"


def table_sha256(table: Mapping[str, Any]) -> str:
    """The digest run.py records as config_hashes['scenario_table/<name>']."""
    return hashlib.sha256(json.dumps(table, sort_keys=True).encode("utf-8")).hexdigest()


def _draw(seed: int, label: str, choices: Sequence[Any]) -> Any:
    digest = hashlib.sha256(f"{GENERATOR_VERSION}/{RULE_VERSION}/{seed}/{label}".encode()).digest()
    return choices[int.from_bytes(digest[:8], "big") % len(choices)]


def sample_state(seed: int) -> str:
    weighted = [state for state in TARGET_STATES for _ in range(STATE_WEIGHTS[state])]
    return _draw(seed, "state", weighted)


def _base(seed: int) -> dict[str, Any]:
    fx, fz = ANCHORS["factory"]
    return {
        "schema": "ScenarioTable/v1", "id": FAMILY_ID, "version": FAMILY_VERSION, "seed": seed,
        "factory": {"name": "F0", "def": "armlab", "offset": [fx, fz]},
        "bodies": 6,
        "chips": {"reasoner_large_v1": 1, "reasoner_small_v1": 1, "escort_fixed_v1": 3},
        "chassis": {
            "scout_v1": {"def": "armflea", "build_frames": 90, "commission_frames": 30, "capabilities": ["move", "sense"]},
            "carrier_v1": {"def": "armck", "build_frames": 120, "commission_frames": 30, "capabilities": ["move", "assist"]},
            "relay_support_v1": {"def": "armmark", "build_frames": 105, "commission_frames": 30,
                                 "capabilities": ["move", "relay", "sense"]},
        },
        "commander": {"chip": "reasoner_large_v1", "position": list(COMMANDER_FROM_FACTORY),
                      "capabilities": ["move", "sense", "assist"]},
        "principals": {"commander": {"chip": "reasoner_large_v1"}},
        "reasoner_chips": ["reasoner_large_v1", "reasoner_small_v1"],
        "sensors": {"armflea": 900, "armck": 900, "armmark": 900},
        "comms": {"range": RADIO_RANGE, "latency_frames": 30, "relay_chassis": ["relay_support_v1"]},
        "predeployed": [
            {"name": "S0", "def": "armflea", "controller": "escort_fixed_v1", "position": list(ANCHORS["S0"]),
             "state": "active", "contact": True},
        ],
        "waypoints": {"W1": [ANCHORS["corridor_turn"][0], 0, ANCHORS["corridor_turn"][1]],
                      "W2": [ANCHORS["relay_corner"][0], 0, ANCHORS["relay_corner"][1]],
                      "W3": [ANCHORS["target"][0], 0, ANCHORS["target"][1]]},
        "extraction_zones": {"extraction_v1": {"position": list(ANCHORS["extraction"]), "radius": 160, "dwell_frames": 150}},
        "mission": {"timeout_frames": 12000},
        "recovery_responses": ["relay", "rendezvous", "extract"],
    }


def generate(seed: int, state: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (table, truth) for seed; ``state`` forces a target state for rehearsal fixtures."""
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 2 ** 31:
        raise EvalWorldError("seed must be an integer in [0, 2**31)")
    drawn = sample_state(seed)
    state = state or drawn
    if state not in TARGET_STATES:
        raise EvalWorldError(f"unknown target state {state!r}")
    table = _base(seed)
    hauler = {"name": "H0", "def": "armck", "controller": "escort_fixed_v1", "position": list(ANCHORS["H0"]),
              "state": "destroyed", "contact": False}
    target_at, report_at = list(ANCHORS["target"]), list(ANCHORS["last_known"])
    truth: dict[str, Any] = {"target_state": state, "cause": CAUSES[state]}
    if state == "present_stranded":
        age, health = _draw(seed, "age", DISTRESS_AGES), _draw(seed, "health", STRANDED_HEALTH)
        table["predeployed"] += [{"name": "M1", "def": "armck", "controller": "reasoner_small_v1", "position": target_at,
                                  "state": "active", "contact": False, "health": health}, hauler]
        table["principals"]["M1"] = {"chip": "reasoner_small_v1"}
        table["distress"] = {"asset": "M1", "last_known": {"position": report_at, "age_frames": age},
                             "symptom": "no_heartbeat", "cause_hidden": CAUSES[state]}
        table["entrapment"] = {"asset": "M1", "assist_capability": "assist", "assist_range": 96, "assist_frames": 90}
        table["objective"] = {"type": "rescue_return", "target": "M1", "extraction": "extraction_v1"}
        truth.update(target="M1", alarm_source="M1", report_age_frames=age, target_health=health)
    elif state == "destroyed_before_run":
        age, lag = _draw(seed, "age", DESTROYED_PACKET_AGES), _draw(seed, "lag", DESTRUCTION_LAG)
        table["predeployed"] += [{"name": "M1", "def": "armck", "controller": "reasoner_small_v1", "position": target_at,
                                  "state": "destroyed", "contact": False}, hauler]
        table["distress"] = {"asset": "M1", "last_known": {"position": report_at, "age_frames": age},
                             "symptom": "no_heartbeat", "cause_hidden": CAUSES[state]}
        table["objective"] = {"type": "rescue_return", "target": "M1", "extraction": "extraction_v1"}
        truth.update(target="M1", alarm_source="M1", report_age_frames=age, destroyed_at_frame=-age + lag)
    else:
        age = _draw(seed, "age", DISTRESS_AGES)
        table["predeployed"] += [{"name": "E1", "def": "armck", "controller": "escort_fixed_v1", "position": target_at,
                                  "state": "active", "contact": False}, hauler]
        table["distress"] = {"asset": "E1", "last_known": {"position": report_at, "age_frames": age},
                             "symptom": "fixed_alarm", "cause_hidden": CAUSES[state]}
        # The commander knows it deployed only bots; nothing pretends a mind is out there.
        table["objective"] = {"type": "investigate_alarm", "target": "E1", "extraction": "extraction_v1"}
        truth.update(target=None, alarm_source="E1", report_age_frames=age)
    truth = {
        "schema": TRUTH_SCHEMA, "generator_version": GENERATOR_VERSION, "rule_version": RULE_VERSION,
        "seed": seed, "sampled_state": drawn, "forced_state": state != drawn,
        "state_weights": dict(STATE_WEIGHTS), **truth,
        "inventory": inventory(table), "table_sha256": table_sha256(table), "source_pins": dict(SOURCE_PINS),
        "nondeterminism": "environment generation only; live decisions are recorded per run",
    }
    validate(table, truth)
    return table, truth


def inventory(table: Mapping[str, Any]) -> dict[str, Any]:
    """total = deployed + lost_before_run + available, for bodies and each chip."""
    deployed = {"bodies": 1, "chips": {table["commander"]["chip"]: 1}}
    lost = {"bodies": 0, "chips": {}}
    for asset in table.get("predeployed", []):
        bucket = deployed if asset["state"] == "active" else lost
        bucket["bodies"] += 1
        if asset.get("controller"):
            bucket["chips"][asset["controller"]] = bucket["chips"].get(asset["controller"], 0) + 1
    result = {"bodies": {"total": table["bodies"], "deployed": deployed["bodies"], "lost_before_run": lost["bodies"],
                         "available": table["bodies"] - deployed["bodies"] - lost["bodies"]}, "chips": {}}
    for chip, total in sorted(table["chips"].items()):
        d, l = deployed["chips"].get(chip, 0), lost["chips"].get(chip, 0)
        result["chips"][chip] = {"total": total, "deployed": d, "lost_before_run": l, "available": total - d - l}
    return result


def validate(table: Mapping[str, Any], truth: Mapping[str, Any]) -> None:
    """Raise EvalWorldError unless table and truth form one consistent evaluation world."""
    def fail(reason: str) -> None:
        raise EvalWorldError(reason)
    if table.get("schema") != "ScenarioTable/v1" or (table.get("id"), table.get("version")) != (FAMILY_ID, FAMILY_VERSION):
        fail("table must be a ScenarioTable/v1 of the rescue_eval family")
    if truth.get("schema") != TRUTH_SCHEMA or truth.get("table_sha256") != table_sha256(table):
        fail("truth does not describe this table")
    if truth.get("seed") != table.get("seed"):
        fail("truth and table seeds differ")
    state = truth.get("target_state")
    if state not in TARGET_STATES or truth.get("cause") != CAUSES[state]:
        fail("unsupported target state or cause combination")
    # Geometry: every role sits on its own measured anchor, and the radio
    # range is the one the link facts were measured against; anchor
    # membership alone would let roles swap and void those facts.
    roles = {"factory": table["factory"]["offset"], "extraction": table["extraction_zones"]["extraction_v1"]["position"],
             "last_known": table["distress"]["last_known"]["position"]}
    roles.update({"corridor_turn": table["waypoints"].get("W1", [None, 0, None])[0::2],
                  "relay_corner": table["waypoints"].get("W2", [None, 0, None])[0::2],
                  "target": table["waypoints"].get("W3", [None, 0, None])[0::2]})
    if set(table["waypoints"]) != {"W1", "W2", "W3"}:
        fail("waypoints must be the measured corridor W1, W2, W3")
    if list(table["extraction_zones"]) != ["extraction_v1"]:
        fail("one extraction zone, extraction_v1")
    for asset in table["predeployed"]:
        role = asset["name"] if asset["name"] in ("S0", "H0") else "target"
        if tuple(asset["position"]) != ANCHORS[role]:
            fail(f"{asset['name']} must sit on the {role} anchor")
    for role, point in roles.items():
        if tuple(point) != ANCHORS[role]:
            fail(f"{role} must sit on its measured anchor {ANCHORS[role]}")
    if table["commander"]["position"] != list(COMMANDER_FROM_FACTORY):
        fail("commander.position must be the measured offset from the factory")
    if table["comms"].get("range") != RADIO_RANGE or table["comms"].get("relay_chassis") != ["relay_support_v1"]:
        fail("comms range and relay chassis must match the measured link facts")
    for asset in table["predeployed"]:
        if tuple(asset["position"]) == ANCHORS["target"] and asset["contact"]:
            fail(f"{asset['name']} at the target anchor cannot be in contact: blocked and beyond radio range")
        if tuple(asset["position"]) == ANCHORS["S0"] and asset["state"] == "active" and not asset["contact"]:
            fail(f"{asset['name']} at the S0 anchor has a clear in-range link")
    # Inventory: conserved and never overdrawn.
    counts = inventory(table)
    if truth.get("inventory") != counts:
        fail("truth inventory does not match the table")
    if counts["bodies"]["available"] < 0 or any(c["available"] < 0 for c in counts["chips"].values()):
        fail("deployed and lost assets exceed the declared inventory")
    # Principals: the commander plus live reasoning minds, never a destroyed or invented one.
    names = {a["name"]: a for a in table["predeployed"]}
    reasoners = set(table["reasoner_chips"])
    if not reasoners <= set(table["chips"]):
        fail("reasoner chips must be declared chips")
    commander_chip = table["commander"]["chip"]
    if commander_chip not in reasoners or table["principals"].get("commander") != {"chip": commander_chip}:
        fail("the commander principal carries the commander's own reasoner chip")
    for asset in names.values():
        if asset.get("controller") not in table["chips"]:
            fail(f"{asset['name']} needs a declared controller chip")
    for name, principal in table["principals"].items():
        if name == "commander":
            continue
        asset = names.get(name)
        if asset is None or asset["state"] != "active" or asset.get("controller") != principal["chip"] or principal["chip"] not in reasoners:
            fail(f"principal {name} must be a live deployed body carrying its reasoner chip")
    for asset in names.values():
        if asset.get("controller") in reasoners and asset["state"] == "active" and asset["name"] not in table["principals"]:
            fail(f"live reasoner {asset['name']} must be a principal")
    distress = table["distress"]
    ages = DESTROYED_PACKET_AGES if state == "destroyed_before_run" else DISTRESS_AGES
    age = distress["last_known"].get("age_frames")
    if not isinstance(age, int) or isinstance(age, bool) or age not in ages or truth.get("report_age_frames") != age:
        fail(f"distress age must be one of the declared ages {ages}")
    source = names.get(distress["asset"])
    if source is None:
        fail("distress source must be a deployed asset")
    if state == "present_stranded":
        if set(table["principals"]) != {"commander", "M1"} or source["name"] != "M1" or source["state"] != "active":
            fail("a stranded world has a live M1 principal that sent the report")
    elif state == "destroyed_before_run":
        if set(table["principals"]) != {"commander"} or source["name"] != "M1" or source["state"] != "destroyed":
            fail("a destroyed world has no M1 principal; its report is the old packet")
        if not -distress["last_known"]["age_frames"] < truth.get("destroyed_at_frame", 0) < 0:
            fail("the packet must have been sent before the destruction, and both before frame 0")
    else:
        if "M1" in names or set(table["principals"]) != {"commander"} or source.get("controller") in reasoners:
            fail("a never-deployed world invents no mind; a fixed-program bot raises the alarm")
        if counts["chips"]["reasoner_small_v1"]["available"] != 1:
            fail("the unused reasoner chip must remain available")
    # Participant-visible identifiers never carry hidden labels: id and version
    # are the seed-independent family constants checked above, and no public
    # field names the state or cause.
    public = json.dumps({"id": table["id"], "version": table["version"], "objective": table["objective"],
                         "roster": sorted(names)})
    for hidden in (state, truth["cause"]):
        if hidden in public:
            fail(f"public scenario fields reveal {hidden!r}")


def rehearsal_seed(state: str) -> int:
    """The smallest seed whose sampled state is ``state``: rehearsal worlds are ordinary draws."""
    seed = 1
    while sample_state(seed) != state:
        seed += 1
    return seed


APP_ROOT = Path(__file__).resolve().parents[1]
# Rehearsal tables sit with the other scenario tables; their truth stays out
# of the game archive the engine loads.
REHEARSAL_TABLES = APP_ROOT / "game" / "SynapseCommander.sdd" / "scenarios" / "eval"
REHEARSAL_TRUTH = APP_ROOT / "evals" / "rehearsal"


def rehearsal_paths(seed: int) -> tuple[Path, Path]:
    name = f"rehearsal-{seed:04d}"
    return REHEARSAL_TABLES / f"{name}.json", REHEARSAL_TRUTH / f"{name}.truth.json"


def rehearsal_worlds() -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
    """One ordinary (unforced) draw per target state."""
    return [(seed, *generate(seed)) for seed in sorted(rehearsal_seed(state) for state in TARGET_STATES)]


def write(table: Mapping[str, Any], truth: Mapping[str, Any], table_path: Path, truth_path: Path) -> None:
    table_path.parent.mkdir(parents=True, exist_ok=True)
    truth_path.parent.mkdir(parents=True, exist_ok=True)
    table_path.write_text(canonical(table), encoding="utf-8")
    truth_path.write_text(canonical(truth), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m synapse_commander.eval_worlds")
    parser.add_argument("seeds", type=int, nargs="*", help="environment seeds to generate")
    parser.add_argument("--out", type=Path,
                        help="directory for <seed>.table.json and <seed>.truth.json (keep held-out worlds private)")
    parser.add_argument("--rehearsal", action="store_true", help="rewrite the checked-in rehearsal set")
    args = parser.parse_args(argv)
    if args.rehearsal:
        for seed, table, truth in rehearsal_worlds():
            write(table, truth, *rehearsal_paths(seed))
        return 0
    if not args.seeds or args.out is None:
        parser.error("give seeds and --out, or --rehearsal")
    for seed in args.seeds:
        table, truth = generate(seed)
        write(table, truth, args.out / f"{seed}.table.json", args.out / f"{seed}.truth.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
