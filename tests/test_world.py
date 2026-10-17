"""Initial rescue-world verdict against an in-process fake engine.

The fake follows rescue_baseline and the gadget's public shapes (roster,
budgets, aged distress report, evaluator-only world_init). It checks the
observer and the verdict's truth comparisons, including that each one can
fail; it is not engine evidence.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
import unittest

from synapse_commander.run import SmokeConfig, run_smoke
from synapse_commander.scenarios import load_scenario


FPS_FAKE = 300
CENTRE = 6144.0
TABLE = load_scenario("rescue_baseline")


class FakeWorldEngine:
    def __init__(self, run_id: str, *, leak: str | None = None, budgets_wrong: bool = False,
                 frozen_age: bool = False) -> None:
        self.run_id = run_id
        self.leak = leak
        self.budgets_wrong = budgets_wrong
        self.frozen_age = frozen_age
        self.lock = threading.RLock()
        self.t0 = None
        self.frame = -1
        self.events: list[dict] = []
        self.latest = None
        self.teardown = {}
        self.stopped = False
        self.truth = [
            {"name": "S0", "def": "armflea", "controller": "escort_fixed_v1", "state": "active", "contact": True,
             "unit_id": 4707, "position": [CENTRE + 300, 18.0, CENTRE - 200]},
            {"name": "M1", "def": "armck", "controller": "reasoner_small_v1", "state": "active", "contact": False,
             "unit_id": 20823, "position": [CENTRE + 2600, -69.0, CENTRE + 1400]},
            {"name": "H0", "def": "armck", "controller": "escort_fixed_v1", "state": "destroyed", "contact": False,
             "unit_id": None, "position": [CENTRE + 2200, -83.0, CENTRE + 1100]},
        ]
        self.last_known = [CENTRE + 2400, -47.0, CENTRE + 1300]

    def start(self):
        self.t0 = time.monotonic()

    def poll(self):
        return 0 if self.stopped else None

    def stop(self):
        self.stopped = True
        self.teardown = {"group_alive_after": False, "teardown_s": 0.0, "signal": "fake"}
        return self.teardown

    def _emit(self, kind, body):
        self.events.append({"seq": len(self.events) + 1, "frame": max(self.frame, 0), "kind": kind, "body": body})

    def _advance(self):
        if self.t0 is None or self.stopped:
            return
        target = int((time.monotonic() - self.t0) * FPS_FAKE)
        while self.frame < target:
            self.frame += 1
            if self.frame == 0:
                self._emit("scenario", {"name": "factory_spawn", "id": "rescue_baseline", "version": TABLE["version"]})
                self._emit("effect", {"schema": "EffectEvent/v1", "effect": "commander_bound", "frame": 0,
                                      "unit_id": 18386, "team": 0, "intent_id": None,
                                      "detail": {"chip": "reasoner_large_v1"}})
                self._emit("scenario", {
                    "name": "world_init", "id": "rescue_baseline", "version": TABLE["version"], "seed": TABLE["seed"],
                    "frame": 0, "commander": {"unit_id": 18386, "chip": "reasoner_large_v1"},
                    "assets": self.truth, "inventory_consistent": True,
                    "inventory": {"bodies": {"total": 6, "deployed": 3, "lost": 1, "available": 2},
                                  "chips": {"escort_fixed_v1": {"total": 3, "deployed": 1, "lost": 1, "available": 1},
                                            "reasoner_large_v1": {"total": 1, "deployed": 1, "lost": 0, "available": 0},
                                            "reasoner_small_v1": {"total": 1, "deployed": 1, "lost": 0, "available": 0}}},
                    "distress": {"asset": "M1", "symptom": "no_heartbeat", "cause_hidden": "terrain_entrapment",
                                 "last_known": self.last_known, "observed_frame": -900}})
                self._emit("hello", {"protocol": "synapse-mailbox/0", "run_id": self.run_id, "teams": [0],
                                     "clock_mode": "continuous",
                                     "scenario": {"id": "rescue_baseline", "seed": TABLE["seed"]}})
            if self.frame % 15 == 0:
                entities = [{"id": 4707, "pos": self.truth[0]["position"], "source": "own_unit", "team": 0,
                             "controller": "escort_fixed_v1"},
                            {"id": 18386, "pos": [CENTRE + 1456, 57.0, CENTRE - 1244], "source": "own_unit", "team": 0,
                             "controller": "reasoner_large_v1"}]
                if self.leak == "hidden_unit":
                    entities.append({"id": 20823, "pos": self.truth[1]["position"], "source": "own_unit", "team": 0,
                                     "controller": "reasoner_small_v1"})
                roster = [{"name": "S0", "def": "armflea", "controller": "escort_fixed_v1", "status": "in_contact",
                           "unit_id": 4707},
                          {"name": "M1", "def": "armck", "controller": "reasoner_small_v1", "status": "no_contact"},
                          {"name": "H0", "def": "armck", "controller": "escort_fixed_v1", "status": "no_contact"}]
                if self.leak == "roster_id":
                    roster[1]["unit_id"] = 20823
                age = 900 if self.frozen_age else self.frame + 900
                report_position = self.truth[1]["position"] if self.leak == "true_position" else self.last_known
                self.latest = {
                    "schema": "ObservationFrame/v1", "observation_id": f"obs-0-{self.frame}", "team": 0,
                    "frame": self.frame, "entities": entities,
                    "scenario": {"id": "rescue_baseline", "version": TABLE["version"],
                                 "waypoints": {"W1": [CENTRE + 500, 15.0, CENTRE + 100]}},
                    "permitted_intents": [{"type": "TASK", "verbs": ["advance"]}],
                    "commander": {"unit_id": 18386, "chip": "reasoner_large_v1"},
                    "budgets": {"bodies_remaining": 3 if self.budgets_wrong else 2,
                                "chips": {"escort_fixed_v1": 1, "reasoner_large_v1": 0, "reasoner_small_v1": 0}},
                    "factories": [{"name": "F0", "unit_id": 10639, "jobs": []}],
                    "roster": roster,
                    "reports": [{"kind": "distress", "asset": "M1", "position": report_position,
                                 "observed_frame": self.frame - age, "age_frames": age, "symptom": "no_heartbeat"}],
                    "truncated": False, "omitted": 0}
                self._emit("observation", self.latest)

    def hello(self):
        with self.lock:
            self._advance()
            return next((e["body"] for e in self.events if e["kind"] == "hello"), None)

    def current_frame(self):
        with self.lock:
            self._advance()
            return max(self.frame, 0)

    def observation_for(self, team):
        with self.lock:
            self._advance()
            if self.latest is None:
                raise KeyError(team)
            return self.latest

    def events_since(self, seq, limit=256):
        with self.lock:
            self._advance()
            return [e for e in self.events if e["seq"] > seq][:limit]

    def submit_intent(self, envelope):
        raise AssertionError("the world observer sends no intents")

    def close(self):
        with self.lock:
            self._emit("bye", {"reason": "quit"})


class WorldRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def run_world(self, run_id, **fake):
        engine = FakeWorldEngine(run_id, **fake)
        config = SmokeConfig(engine_dir=self.out, data_dir=self.out, out_dir=self.out, run_id=run_id,
                             rig_commit="b" * 40, hello_timeout_s=10, client_timeout_s=20, client_poll_s=0.02,
                             mode="world", scenario="rescue_baseline")
        return run_smoke(config, transport_factory=lambda _d, _i: engine, engine_factory=lambda _l: engine,
                         app=("a" * 40, 0), engine_scan=lambda _p: [])

    def test_initial_world_matches_truth_without_disclosing_it(self):
        verdict = self.run_world("world-ok")
        self.assertEqual(verdict["verdict"], "PASS", verdict["failed_checks"])
        self.assertEqual(verdict["checks"]["no_hidden_truth_in_player_output"]["hidden_unit_ids"], [20823])
        self.assertEqual(verdict["checks"]["ledger_verified"]["classification"], "scripted_fixture")

    def test_each_truth_comparison_can_fail(self):
        cases = {
            "world-leak-entity": ({"leak": "hidden_unit"}, "no_hidden_truth_in_player_output"),
            "world-leak-report": ({"leak": "true_position"}, "no_hidden_truth_in_player_output"),
            # The gateway refuses a no-contact roster entry with a unit id, so the
            # observer never sees a world at all; either layer must fail the run.
            "world-roster-id": ({"leak": "roster_id"}, ("roster_contact_and_ids", "world_observed")),
            "world-budgets": ({"budgets_wrong": True}, "budgets_debited_for_predeployed"),
            "world-frozen-age": ({"frozen_age": True}, "distress_report_aged_and_growing"),
        }
        for run_id, (fake, check) in cases.items():
            with self.subTest(run_id):
                verdict = self.run_world(run_id, **fake)
                self.assertEqual(verdict["verdict"], "FAIL")
                expected = check if isinstance(check, tuple) else (check,)
                self.assertTrue(set(expected) & set(verdict["failed_checks"]), verdict["failed_checks"])


if __name__ == "__main__":
    unittest.main()
