"""Loop 0 orchestration against an in-process fake engine (no Recoil).

The fake stands in for the engine and its mailbox: it advances frames from
the wall clock, moves one unit toward a fixed waypoint, and emits outbox-shaped
hello, scenario, observation, receipt and effect records.  These tests cover
the launcher's wiring, ledger ingestion and verdict rules only; they are not
engine evidence.
"""

from __future__ import annotations

import math
from pathlib import Path
import tempfile
import threading
import time
import unittest

from synapse_commander import launch
from synapse_commander.protocol import IntentReceipt, bounded_observation
from synapse_commander.recording import verify_ledger
from synapse_commander.run import SmokeConfig, run_smoke


FPS_FAKE = 300  # simulated frames per wall second, so a stale observation arrives in ~3 s


class FakeEngine:
    def __init__(self, run_id: str, *, accept_stale: bool = False, move: bool = True,
                 gaia: bool = True, die_after_s: float | None = None) -> None:
        self.die_after_s = die_after_s
        self.run_id = run_id
        self.accept_stale = accept_stale
        self.move = move
        self.gaia = gaia
        self.lock = threading.RLock()
        self.t0: float | None = None
        self.frame = -1
        self.events: list[dict] = []
        self.observations: dict[str, int] = {}
        self.latest: dict | None = None
        self.receipts: dict[str, dict] = {}
        self.seen: set[str] = set()
        self.unit = {"id": 101, "pos": [1000.0, 10.0, 1000.0], "target": None, "intent": None}
        self.gaia_unit = {"id": 900, "pos": [1100.0, 10.0, 1050.0]}
        self.w1 = [1400.0, 10.0, 1000.0]
        self.teardown: dict[str, object] = {}
        self.stopped = False

    # engine process side
    def start(self) -> None:
        self.t0 = time.monotonic()

    def _dead(self) -> bool:
        return (self.die_after_s is not None and self.t0 is not None
                and time.monotonic() - self.t0 > self.die_after_s)

    def poll(self) -> int | None:
        if self._dead():
            return 134
        return 0 if self.stopped else None

    def stop(self) -> dict[str, object]:
        self.stopped = True
        self.teardown = {"started": True, "group_alive_after": False, "teardown_s": 0.0, "signal": "fake"}
        return self.teardown

    # simulation
    def _emit(self, kind: str, body: dict) -> None:
        self.events.append({"seq": len(self.events) + 1, "frame": max(self.frame, 0), "kind": kind, "body": body})

    def _advance(self) -> None:
        if self.t0 is None or self.stopped or self._dead():
            return
        target = int((time.monotonic() - self.t0) * FPS_FAKE)
        while self.frame < target:
            self.frame += 1
            if self.frame == 0:
                self._emit("hello", {"protocol": "synapse-mailbox/0", "run_id": self.run_id, "teams": [0],
                                     "clock_mode": "continuous"})
                self._emit("scenario", {"name": "smoke_spawn", "unit_id": self.unit["id"]})
                self._emit("effect", {"schema": "EffectEvent/v1", "effect": "unit_created", "frame": 0,
                                      "unit_id": self.unit["id"], "team": 0, "intent_id": None})
            unit = self.unit
            if unit["target"] is not None and self.move:
                dx = unit["target"][0] - unit["pos"][0]
                dz = unit["target"][2] - unit["pos"][2]
                dist = math.hypot(dx, dz)
                if dist <= 2.0:
                    unit["pos"] = list(unit["target"])
                    self._emit("effect", {"schema": "EffectEvent/v1", "effect": "order_done", "frame": self.frame,
                                          "unit_id": unit["id"], "team": 0, "intent_id": unit["intent"]})
                    unit["target"] = None
                else:
                    unit["pos"] = [unit["pos"][0] + 2.0 * dx / dist, unit["pos"][1], unit["pos"][2] + 2.0 * dz / dist]
            if self.frame % 15 == 0:
                observation_id = f"obs-0-{self.frame}"
                self.observations[observation_id] = self.frame
                entities = [{"id": unit["id"], "pos": list(unit["pos"]), "source": "own_unit",
                             "observed_frame": self.frame, "age_frames": 0}]
                if self.gaia:
                    entities.append({"id": self.gaia_unit["id"], "pos": list(self.gaia_unit["pos"]), "source": "los",
                                     "observed_frame": self.frame, "age_frames": 0})
                self.latest = {"schema": "ObservationFrame/v1", "observation_id": observation_id, "team": 0,
                               "frame": self.frame, "entities": entities,
                               "scenario": {"waypoints": {"W1": self.w1}},
                               "permitted_intents": [{"type": "TASK", "verbs": ["advance"]}],
                               "truncated": False, "omitted": 0}
                self._emit("observation", self.latest)

    # transport port
    def hello(self):
        with self.lock:
            self._advance()
            return self.events[0]["body"] if self.events else None

    def current_frame(self) -> int:
        with self.lock:
            self._advance()
            return max(self.frame, 0)

    def observation_for(self, team: str):
        with self.lock:
            self._advance()
            if self.latest is None or str(team) != "0":
                raise KeyError(team)
            return self.latest

    def events_since(self, seq: int, limit: int = 256):
        with self.lock:
            self._advance()
            return [e for e in self.events if e["seq"] > seq][:limit]

    def close(self) -> None:
        with self.lock:
            self._emit("bye", {"reason": "quit"})

    def submit_intent(self, envelope) -> IntentReceipt:
        with self.lock:
            self._advance()
            frame = self.frame
            reason = None
            payload = envelope.payload
            if str(envelope.team) != "0":
                reason = "wrong_team"
            elif envelope.intent_id in self.seen:
                reason = "duplicate_intent_id"
            elif envelope.type != "TASK":
                reason = "unsupported_type"
            elif payload.get("verb") != "advance" or payload.get("args"):
                reason = "verb_not_supported"
            elif envelope.based_on_observation_id not in self.observations:
                reason = "unknown_observation"
            elif frame - self.observations[envelope.based_on_observation_id] > 900 and not self.accept_stale:
                reason = "stale_observation"
            elif envelope.expires_at_frame < frame:
                reason = "expired"
            elif str(payload.get("unit")) != str(self.unit["id"]):
                reason = "not_owned"
            self.seen.add(envelope.intent_id)
            status = "rejected" if reason else "accepted"
            self._emit("receipt", {"schema": "IntentReceipt/v1", "intent_id": envelope.intent_id, "team": 0,
                                   "status": status, "reason": reason, "frame": frame})
            if reason is None:
                self.unit["target"] = list(self.w1)
                self.unit["intent"] = envelope.intent_id
                self._emit("effect", {"schema": "EffectEvent/v1", "effect": "order_started", "frame": frame,
                                      "unit_id": self.unit["id"], "team": 0, "intent_id": envelope.intent_id})
            return IntentReceipt(envelope.intent_id, reason is None, frame, f"fake-{envelope.intent_id}", reason)


def gateway_passes_scenario() -> bool:
    sample = {"observation_id": "o", "frame": 0, "entities": [], "scenario": {"waypoints": {"W1": [1, 2, 3]}}}
    return "scenario" in bounded_observation(sample)


class LoopOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def run_fake(self, run_id: str, **fake_options) -> tuple[dict, FakeEngine]:
        engine = FakeEngine(run_id, **fake_options)
        config = SmokeConfig(engine_dir=self.out, data_dir=self.out, out_dir=self.out, run_id=run_id,
                             rig_commit="b" * 40, lock_dir=self.out / "locks" / "fake", hello_timeout_s=10,
                             client_timeout_s=20, client_poll_s=0.02)
        verdict = run_smoke(config, transport_factory=lambda _dir, _id: engine,
                            engine_factory=lambda _layout: engine, app=("a" * 40, 0))
        return verdict, engine

    def test_full_loop_with_fake_engine(self) -> None:
        verdict, _engine = self.run_fake("fake-ok")
        checks = verdict["checks"]
        for name in ("hello_seen", "advance_accepted", "displacement_observed_within_300_frames",
                     "receipt_before_displacement", "order_done_effect", "ledger_causal_order",
                     "ledger_verified", "probes_no_effect_records", "no_movement_after_probes",
                     "probe_foreign_unit", "probe_stale_observation", "probe_expired", "probe_produce_unsupported",
                     "probe_raw_engine_payload", "probe_unsupported_verb", "probe_unknown_observation",
                     "identity_injected_by_gateway", "teardown_bounded", "not_autonomous"):
            self.assertTrue(checks[name]["ok"], (name, checks[name]))
        self.assertEqual(checks["ledger_verified"]["classification"], "scripted_replay")
        self.assertEqual(checks["unit_and_waypoint_observed"]["ok"], gateway_passes_scenario())
        self.assertEqual(verdict["verdict"], "PASS" if gateway_passes_scenario() else "FAIL")
        run_dir = self.out / "fake-ok"
        self.assertTrue(verify_ledger(run_dir / "ledger.jsonl").ok)
        self.assertTrue((run_dir / "verdict.json").exists())
        self.assertFalse((self.out / "locks" / "fake").exists(), "lock released")

    def test_accepted_stale_intent_fails_the_verdict(self) -> None:
        verdict, _engine = self.run_fake("fake-stale", accept_stale=True)
        self.assertEqual(verdict["verdict"], "FAIL")
        self.assertFalse(verdict["checks"]["probe_stale_observation"]["ok"])
        self.assertFalse(verdict["checks"]["probes_no_effect_records"]["ok"])

    def test_acceptance_without_movement_fails(self) -> None:
        verdict, _engine = self.run_fake("fake-still", move=False)
        self.assertEqual(verdict["verdict"], "FAIL")
        self.assertFalse(verdict["checks"]["displacement_observed_within_300_frames"]["ok"])
        self.assertFalse(verdict["checks"]["order_done_effect"]["ok"])
        self.assertTrue(verdict["checks"]["advance_accepted"]["ok"])

    def test_missing_foreign_unit_fails_rather_than_skips_silently(self) -> None:
        verdict, _engine = self.run_fake("fake-nogaia", gaia=False)
        self.assertFalse(verdict["checks"]["probe_foreign_unit"]["ok"])
        self.assertEqual(verdict["verdict"], "FAIL")

    def test_engine_death_fails_fast_with_partial_trace(self) -> None:
        begin = time.monotonic()
        verdict, _engine = self.run_fake("fake-dies", die_after_s=1.0)
        self.assertEqual(verdict["verdict"], "FAIL")
        self.assertEqual(verdict["engine_exited_early"], 134)
        self.assertIsNotNone(verdict["error"])
        self.assertLess(time.monotonic() - begin, 15)
        self.assertTrue(verdict["checks"]["advance_accepted"]["ok"])  # it got that far

    def test_rig_commit_required(self) -> None:
        config = SmokeConfig(engine_dir=self.out, data_dir=self.out, out_dir=self.out, run_id="x", rig_commit=None)
        with self.assertRaises(launch.LaunchError):
            run_smoke(config, app=("a" * 40, 0))


if __name__ == "__main__":
    unittest.main()
