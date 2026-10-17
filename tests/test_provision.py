"""Loop 1 provisioning orchestration against an in-process fake engine.

The fake follows the rescue_smoke table and the gadget's public shapes: a
bound commander, budgets, one factory with jobs, unit_created carrying the
controller, and budget rejections. It checks the client, ledger ingestion and
verdict rules only; it is not engine evidence.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest

from synapse_commander.protocol import IntentReceipt
from synapse_commander.recording import verify_ledger
from synapse_commander.run import SmokeConfig, run_smoke
from synapse_commander.scenarios import load_scenario


FPS_FAKE = 300


class FakeProvisionEngine:
    def __init__(self, run_id: str, *, spend: bool = True, refuse_unfunded: bool = True) -> None:
        self.run_id = run_id
        self.spend = spend
        self.refuse_unfunded = refuse_unfunded
        self.table = load_scenario("rescue_smoke")
        self.lock = threading.RLock()
        self.t0: float | None = None
        self.frame = -1
        self.events: list[dict] = []
        self.observations: dict[str, int] = {}
        self.latest: dict | None = None
        self.budgets = {"bodies_remaining": self.table["bodies"], "chips": dict(self.table["chips"])}
        self.commander = {"unit_id": 50, "chip": self.table["commander"]["chip"]}
        self.factory_unit = 70
        self.jobs: list[dict] = []
        self.units: dict[int, dict] = {50: {"pos": [3000.0, 10.0, 3000.0], "controller": self.commander["chip"]}}
        self.w1 = [1400.0, 10.0, 1000.0]
        self.seen: set[str] = set()
        self.moving: dict[int, str] = {}
        self.teardown: dict[str, object] = {}
        self.stopped = False

    def start(self) -> None:
        self.t0 = time.monotonic()

    def poll(self):
        return 0 if self.stopped else None

    def stop(self):
        self.stopped = True
        self.teardown = {"group_alive_after": False, "teardown_s": 0.0, "signal": "fake"}
        return self.teardown

    def _emit(self, kind: str, body: dict) -> None:
        self.events.append({"seq": len(self.events) + 1, "frame": max(self.frame, 0), "kind": kind, "body": body})

    def _effect(self, name: str, unit_id, intent_id, detail=None) -> None:
        self._emit("effect", {"schema": "EffectEvent/v1", "effect": name, "frame": self.frame, "unit_id": unit_id,
                              "team": 0, "intent_id": intent_id, "detail": detail or {}})

    def _advance(self) -> None:
        if self.t0 is None or self.stopped:
            return
        target = int((time.monotonic() - self.t0) * FPS_FAKE)
        while self.frame < target:
            self.frame += 1
            if self.frame == 0:
                self._emit("scenario", {"name": "rescue_smoke", "waypoints": {"W1": self.w1}})
                self._emit("hello", {"protocol": "synapse-mailbox/0", "run_id": self.run_id, "teams": [0],
                                     "clock_mode": "continuous"})
                self._effect("commander_bound", 50, None, {"chip": self.commander["chip"]})
            for job in self.jobs:
                if job["done_frame"] == self.frame:
                    unit_id = 200 + len([j for j in self.jobs if j["done_frame"] <= self.frame])
                    self.units[unit_id] = {"pos": [1000.0, 10.0, 1000.0], "controller": job["controller"]}
                    self._effect("unit_created", unit_id, job["intent_id"],
                                 {"controller": job["controller"], "job_id": job["job_id"], "chassis": job["chassis"]})
                    self._effect("factory_job_done", self.factory_unit, job["intent_id"],
                                 {"job_id": job["job_id"], "unit_id": unit_id})
            for unit_id, intent_id in list(self.moving.items()):
                pos = self.units[unit_id]["pos"]
                dx, dz = self.w1[0] - pos[0], self.w1[2] - pos[2]
                dist = math.hypot(dx, dz)
                if dist <= 2.0:
                    self.units[unit_id]["pos"] = list(self.w1)
                    self._effect("order_done", unit_id, intent_id, {"reason": "arrived"})
                    del self.moving[unit_id]
                else:
                    self.units[unit_id]["pos"] = [pos[0] + 2 * dx / dist, pos[1], pos[2] + 2 * dz / dist]
            if self.frame % 15 == 0:
                observation_id = f"obs-0-{self.frame}"
                self.observations[observation_id] = self.frame
                self.latest = {
                    "schema": "ObservationFrame/v1", "observation_id": observation_id, "team": 0, "frame": self.frame,
                    "entities": [{"id": uid, "pos": list(u["pos"]), "source": "own_unit", "team": 0,
                                  "controller": u["controller"]} for uid, u in sorted(self.units.items())],
                    "scenario": {"id": self.table["id"], "version": self.table["version"],
                                 "waypoints": {"W1": self.w1}},
                    "permitted_intents": [{"type": "TASK", "verbs": ["advance"]},
                                          {"type": "PRODUCE", "factories": [self.table["factory"]["name"]],
                                           "chassis": sorted(self.table["chassis"]),
                                           "controllers": sorted(self.table["chips"])}],
                    "commander": dict(self.commander),
                    "budgets": {"bodies_remaining": self.budgets["bodies_remaining"],
                                "chips": dict(self.budgets["chips"])},
                    "factories": [{"name": self.table["factory"]["name"], "unit_id": self.factory_unit,
                                   "jobs": [{k: j[k] for k in ("job_id", "intent_id", "chassis", "controller",
                                                               "started_frame", "done_frame")} for j in self.jobs]}],
                    "truncated": False, "omitted": 0}
                self._emit("observation", self.latest)

    def hello(self):
        with self.lock:
            self._advance()
            return next((e["body"] for e in self.events if e["kind"] == "hello"), None)

    def current_frame(self) -> int:
        with self.lock:
            self._advance()
            return max(self.frame, 0)

    def observation_for(self, team: str):
        with self.lock:
            self._advance()
            if self.latest is None:
                raise KeyError(team)
            return self.latest

    def events_since(self, seq: int, limit: int = 256):
        with self.lock:
            self._advance()
            return [e for e in self.events if e["seq"] > seq][:limit]

    def close(self) -> None:
        with self.lock:
            self._emit("bye", {"reason": "quit"})

    def _receipt(self, envelope, frame, reason) -> IntentReceipt:
        self._emit("receipt", {"schema": "IntentReceipt/v1", "intent_id": envelope.intent_id, "team": 0,
                               "status": "rejected" if reason else "accepted", "reason": reason, "frame": frame})
        return IntentReceipt(envelope.intent_id, reason is None, frame, f"fake-{envelope.intent_id}", reason)

    def submit_intent(self, envelope) -> IntentReceipt:
        with self.lock:
            self._advance()
            frame = self.frame
            payload = envelope.payload
            self.seen.add(envelope.intent_id)
            if envelope.based_on_observation_id not in self.observations:
                return self._receipt(envelope, frame, "unknown_observation")
            if envelope.type == "PRODUCE":
                funded = self.budgets["chips"].get(payload["controller"], 0) > 0
                if self.budgets["bodies_remaining"] < 1:
                    return self._receipt(envelope, frame, "budget_exhausted")
                if not funded and self.refuse_unfunded:
                    return self._receipt(envelope, frame, "chip_unavailable")
                if self.spend:
                    self.budgets["bodies_remaining"] -= 1
                    self.budgets["chips"][payload["controller"]] -= 1
                job = {"job_id": f"job-{len(self.jobs) + 1}", "intent_id": envelope.intent_id,
                       "chassis": payload["chassis"], "controller": payload["controller"],
                       "started_frame": frame, "done_frame": frame + 150}
                self.jobs.append(job)
                receipt = self._receipt(envelope, frame, None)
                self._effect("factory_job_started", self.factory_unit, envelope.intent_id,
                             {"job_id": job["job_id"], "factory": "F0", "chassis": job["chassis"],
                              "controller": job["controller"]})
                return receipt
            unit_id = int(payload["unit"])
            if unit_id not in self.units:
                return self._receipt(envelope, frame, "not_owned")
            receipt = self._receipt(envelope, frame, None)
            self.moving[unit_id] = envelope.intent_id
            self._effect("order_started", unit_id, envelope.intent_id, {"target": self.w1})
            return receipt


class ProvisionRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def run_provision(self, run_id: str, **fake) -> dict:
        engine = FakeProvisionEngine(run_id, **fake)
        config = SmokeConfig(engine_dir=self.out, data_dir=self.out, out_dir=self.out, run_id=run_id,
                             rig_commit="b" * 40, hello_timeout_s=10, client_timeout_s=20, client_poll_s=0.02,
                             mode="provision", scenario="rescue_smoke")
        return run_smoke(config, transport_factory=lambda _d, _i: engine, engine_factory=lambda _l: engine,
                         app=("a" * 40, 0), engine_scan=lambda _p: [])

    def test_produce_build_refuse_and_advance(self) -> None:
        verdict = self.run_provision("prov-ok")
        self.assertEqual(verdict["verdict"], "PASS", verdict["failed_checks"])
        checks = verdict["checks"]
        self.assertEqual(checks["unfunded_produce_rejected"]["reason"], "chip_unavailable")
        self.assertEqual(checks["ledger_verified"]["classification"], "scripted_replay")
        order = checks["ledger_causal_order"]["seqs"]
        self.assertEqual(list(order.values()), sorted(order.values()))
        run_dir = self.out / "prov-ok"
        self.assertIn("synapse_scenario=rescue_smoke;", (run_dir / "write" / "_synapse_script.txt").read_text())
        trace = json.loads((run_dir / "trace.json").read_text())
        self.assertNotEqual(trace["produced_unit"]["id"], trace["commander_id"])
        self.assertTrue(verify_ledger(run_dir / "ledger.jsonl").ok)

    def test_engine_that_does_not_spend_fails_the_budget_check(self) -> None:
        verdict = self.run_provision("prov-nospend", spend=False)
        self.assertEqual(verdict["verdict"], "FAIL")
        self.assertFalse(verdict["checks"]["budgets_decremented"]["ok"])

    def test_engine_that_builds_an_unfunded_unit_fails(self) -> None:
        verdict = self.run_provision("prov-overbuild", refuse_unfunded=False)
        self.assertEqual(verdict["verdict"], "FAIL")
        self.assertFalse(verdict["checks"]["unfunded_produce_rejected"]["ok"])
        self.assertFalse(verdict["checks"]["unfunded_produce_no_effect"]["ok"])

    def test_provision_mode_requires_a_scenario(self) -> None:
        from synapse_commander import launch
        config = SmokeConfig(engine_dir=self.out, data_dir=self.out, out_dir=self.out, run_id="noscen",
                             rig_commit="b" * 40, mode="provision")
        with self.assertRaises(launch.LaunchError):
            run_smoke(config, app=("a" * 40, 0))


if __name__ == "__main__":
    unittest.main()
