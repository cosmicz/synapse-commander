"""run-rescue against a two-principal fake engine (no Recoil).

The fake follows rescue_baseline's public shapes closely enough for the
gateway: per-principal observations, link state, roster, budgets and
delivered message reports. Its relay restores M1's link when ordered, so the
rehearsal reaches the extraction step; without move_to it must stop with the
outcome labelled pending. Not engine evidence.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest

from synapse_commander.agents.rescue import absolute, parse_extraction, render_observation, rescue_points
from synapse_commander.protocol import IntentReceipt
from synapse_commander.run import SmokeConfig, run_smoke
from synapse_commander.scenarios import load_scenario


TABLE = load_scenario("rescue_baseline")
CENTRE = 6144.0


def world(offset):
    return [CENTRE + offset[0], 10.0, CENTRE + offset[-1]]


class RescueEngine:
    def __init__(self, run_id: str, *, move_to: bool = False) -> None:
        self.run_id = run_id
        self.move_to = move_to
        self.m1_moving_since: int | None = None
        self.extracted = False
        self.lock = threading.RLock()
        self.frame = 0
        self.events: list[dict] = []
        self.stopped = False
        self.teardown: dict[str, object] = {}
        self.budgets = {"bodies_remaining": 2, "chips": {"escort_fixed_v1": 1, "reasoner_large_v1": 0,
                                                         "reasoner_small_v1": 0}}
        self.relay: int | None = None
        self.relay_linked_m1 = False
        self.reports = {"commander": [], "M1": []}
        self.seen: set[str] = set()
        self._emit("scenario", {"name": "factory_spawn"})
        self._emit("hello", {"protocol": "synapse-mailbox/0", "run_id": run_id, "clock_mode": "continuous",
                             "scenario": {"id": TABLE["id"], "seed": TABLE["seed"]}, "principals": ["commander", "M1"]})

    def _emit(self, kind, body):
        self.events.append({"seq": len(self.events) + 1, "frame": self.frame, "kind": kind, "body": body})

    def start(self):
        return

    def poll(self):
        return 0 if self.stopped else None

    def stop(self):
        self.stopped = True
        self.teardown = {"group_alive_after": False, "teardown_s": 0.0, "signal": "fake"}
        return self.teardown

    def hello(self):
        return next(e["body"] for e in self.events if e["kind"] == "hello")

    def current_frame(self):
        return self.frame

    def _tick(self):
        self.frame += 15

    def observation_for(self, team, principal=None):
        with self.lock:
            self._tick()
            body_id = 18386 if principal == "commander" else 20823
            observation = {
                "schema": "ObservationFrame/v1", "principal": principal,
                "observation_id": f"obs-{principal}-{self.frame}", "team": 0, "frame": self.frame,
                "body": {"id": body_id, "pos": world([0, 240]) if principal == "commander" else world([-1536, 2176]),
                         "source": "own_body", "commands": 0,
                         "link": principal == "commander" or self.relay_linked_m1},
                "entities": [], "reports": list(self.reports[principal]), "resources": {},
                "scenario": {"id": TABLE["id"], "version": TABLE["version"],
                             "waypoints": {"W1": world(TABLE["waypoints"]["W1"])}},
                "permitted_intents": [{"type": "TASK", "verbs": ["advance", "move_to"] if self.move_to else ["advance"]},
                                      {"type": "MESSAGE"}],
                "truncated": False, "omitted": 0}
            if self.m1_moving_since is not None and self.frame - self.m1_moving_since > 120 and not self.extracted:
                self.extracted = True
                self._emit("effect", {"effect": "mind_extracted", "unit_id": 20823, "principal": "M1",
                                      "detail": {"target": "M1", "unit_id": 20823}})
                self._emit("effect", {"effect": "mind_extracted", "unit_id": 20823, "principal": "commander",
                                      "detail": {"target": "M1", "unit_id": 20823}})
                self._emit("scenario", {"name": "mission_outcome", "result": "extracted", "target": "M1"})
            if principal == "commander":
                linked = self.relay_linked_m1
                observation["objective"] = {"type": "rescue_return", "target": "M1",
                                            "status": "extracted" if self.extracted else "pending"}
                observation.update(
                    budgets={"bodies_remaining": self.budgets["bodies_remaining"],
                             "chips": dict(self.budgets["chips"])},
                    factories=[{"name": "F0", "unit_id": 10639}],
                    link_state=[{"unit_id": 4707, "linked": True}]
                    + ([{"unit_id": self.relay, "linked": True}] if self.relay else []),
                    roster=[{"name": "S0", "def": "armflea", "controller": "escort_fixed_v1", "status": "in_contact",
                             "unit_id": 4707},
                            {"name": "M1", "def": "armck", "controller": "reasoner_small_v1",
                             "status": "in_contact" if linked else "no_contact",
                             **({"unit_id": 20823} if linked else {})},
                            {"name": "H0", "def": "armck", "controller": "escort_fixed_v1", "status": "no_contact"}])
            return observation

    def events_since(self, seq, limit=256):
        return [e for e in self.events if e["seq"] > seq][:limit]

    def _receipt(self, envelope, reason=None):
        self._emit("receipt", {"intent_id": envelope.intent_id, "status": "rejected" if reason else "accepted",
                               "reason": reason})
        return IntentReceipt(envelope.intent_id, reason is None, self.frame, "fake-" + envelope.intent_id, reason)

    def submit_intent(self, envelope):
        with self.lock:
            if envelope.intent_id in self.seen:
                return self._receipt(envelope, "duplicate_intent_id")
            self.seen.add(envelope.intent_id)
            payload = envelope.payload
            if envelope.type == "PRODUCE":
                if self.budgets["chips"].get(payload["controller"], 0) < 1:
                    return self._receipt(envelope, "chip_unavailable")
                self.budgets["bodies_remaining"] -= 1
                self.budgets["chips"][payload["controller"]] -= 1
                receipt = self._receipt(envelope)
                self.relay = 30001
                self._emit("effect", {"effect": "unit_created", "intent_id": envelope.intent_id,
                                      "unit_id": self.relay, "principal": "commander"})
                return receipt
            if envelope.type == "MESSAGE":
                receipt = self._receipt(envelope)
                self._emit("effect", {"effect": "message_sent", "intent_id": envelope.intent_id,
                                      "principal": envelope.principal, "unit_id": None})
                if self.relay_linked_m1:
                    self.reports[payload["to"]].append(
                        {"kind": "message", "from": envelope.principal, "text": payload["text"],
                         "observed_frame": self.frame, "delivered_frame": self.frame + 30, "age_frames": 30})
                return receipt
            if payload.get("verb") not in (("advance", "move_to") if self.move_to else ("advance",)):
                return self._receipt(envelope, "verb_not_supported")
            if envelope.principal == "M1":
                receipt = self._receipt(envelope)
                self.m1_moving_since = self.frame
                self._emit("effect", {"effect": "order_started", "intent_id": envelope.intent_id,
                                      "principal": "M1", "unit_id": 20823})
                return receipt
            if envelope.principal == "commander" and str(payload.get("unit")) != str(self.relay):
                return self._receipt(envelope, "not_authorized")
            receipt = self._receipt(envelope)
            self._emit("effect", {"effect": "order_started", "intent_id": envelope.intent_id,
                                  "principal": envelope.principal, "unit_id": payload.get("unit")})
            if str(payload.get("unit")) == str(self.relay):
                self.relay_linked_m1 = True
                self._emit("effect", {"effect": "link_restored", "unit_id": 20823, "principal": "M1"})
            return receipt

    def close(self):
        self._emit("bye", {"reason": "quit"})


class RescueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def run_rescue(self, run_id, move_to=False, **config):
        engine = RescueEngine(run_id, move_to=move_to)
        cfg = SmokeConfig(engine_dir=self.out, data_dir=self.out, out_dir=self.out, run_id=run_id,
                          rig_commit="b" * 40, scenario="rescue_baseline", mode="rescue", client_timeout_s=10,
                          client_poll_s=0.01, **config)
        verdict = run_smoke(cfg, transport_factory=lambda _p, _r: engine, engine_factory=lambda _l: engine,
                            app=("a" * 40, 0), engine_scan=lambda _p: [])
        return verdict, engine

    def test_rehearsal_restores_the_link_and_labels_the_missing_extraction_verbs(self):
        verdict, engine = self.run_rescue("rescue-scripted")
        self.assertEqual(verdict["verdict"], "PASS", (verdict.get("error"), verdict.get("failed_checks")))
        self.assertEqual(verdict["rescue_outcome"], "pending")
        run_dir = self.out / "rescue-scripted"
        plan = (run_dir / "phase_plan.md").read_text()
        for phase in ("situation", "produce_relay", "relay_built", "relay_moving", "link_restored",
                      "message_delivered", "extraction_pending"):
            self.assertIn(f"| {phase} |", plan)
        self.assertEqual(engine.budgets["chips"]["escort_fixed_v1"], 0)
        self.assertIn("extract at", engine.reports["M1"][0]["text"])

    def test_rehearsal_extracts_m1_through_the_shared_policy(self):
        verdict, engine = self.run_rescue("rescue-full", move_to=True)
        self.assertEqual(verdict["verdict"], "PASS", (verdict.get("error"), verdict.get("failed_checks")))
        self.assertEqual(verdict["rescue_outcome"], "extracted")
        trace = json.loads((self.out / "rescue-full" / "trace.json").read_text())
        labels = [a["label"] for a in trace["m1_actions"]]
        self.assertIn("to-extraction", labels)  # chosen by m1_policy from the delivered message
        plan = (self.out / "rescue-full" / "phase_plan.md").read_text()
        for phase in ("link_restored", "message_delivered", "m1_moving", "outcome"):
            self.assertIn(f"| {phase} |", plan)
        self.assertNotIn("damaged", plan)

    def test_play_records_the_persons_intents_as_human_and_shows_refusals(self):
        commands = io.StringIO("help\nobs\nproduce relay_support_v1\nproduce relay_support_v1\n"
                               "message M1 hold on\nmove_to 30001 100 200\nfly away\nquit\n")
        output = io.StringIO()
        verdict, _engine = self.run_rescue("rescue-play", rescue_mode="play", play_stdin=commands,
                                           play_stdout=output)
        self.assertEqual(verdict["verdict"], "PASS", (verdict.get("error"), verdict.get("failed_checks")))
        text = output.getvalue()
        self.assertIn("[commander] frame", text)
        self.assertIn("-> PRODUCE accepted", text)
        self.assertIn("-> PRODUCE refused: chip_unavailable", text)
        self.assertIn("-> TASK refused: invalid_parameters", text)
        self.assertIn("unknown command", text)
        records = [json.loads(line) for line in (self.out / "rescue-play" / "ledger.jsonl").read_text().splitlines()]
        submitted = [r for r in records if r["kind"] == "INTENT_SUBMITTED"]
        commander = [r for r in submitted if r["payload"]["principal"] == "commander"]
        self.assertEqual(len(commander), 3)  # move_to is refused at the gateway before submission
        self.assertTrue(all(r["origin"] == "human" for r in commander))
        self.assertTrue(all(r["origin"] == "scripted" for r in submitted if r["payload"]["principal"] == "M1"))
        self.assertTrue(verdict["checks"]["human_inputs_recorded"]["ok"])

    def test_points_and_rendering(self):
        points = rescue_points(TABLE)
        observation = {"scenario": {"waypoints": {"W1": world(TABLE["waypoints"]["W1"])}}}
        self.assertEqual(absolute(observation, points["extraction"], points["w1"]),
                         (CENTRE - 752, CENTRE + 896))
        self.assertEqual(parse_extraction({"reports": [{"from": "commander", "text": "extract at 5392 7040"}]}),
                         (5392.0, 7040.0))
        self.assertIsNone(parse_extraction({"reports": [{"from": "S0", "text": "extract at 1 2"}]}))
        rendered = render_observation({"principal": "commander", "frame": 30, "observation_id": "o",
                                       "roster": [{"name": "M1", "def": "armck", "status": "no_contact"}],
                                       "permitted_intents": [{"type": "TASK", "verbs": ["advance"]}]})
        self.assertIn("roster M1 armck no_contact", rendered)


if __name__ == "__main__":
    unittest.main()
