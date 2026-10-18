"""A fixed first production must not leave the second, reasoning body inert."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from synapse_commander.agents.commander import ModelReply
from synapse_commander import launch, run


class Client:
    def __init__(self, principal, observed):
        self.principal = principal
        self.observed = observed
        self.submissions = []

    def observation(self):
        if self.principal not in self.observed:
            return 409, {"error": "not_embodied"}
        return 200, self.observed[self.principal]

    def submit(self, envelope):
        self.submissions.append(envelope)
        return 202, {"accepted": True}


class Route:
    name, model = "fake-test", "test-model"

    def __init__(self):
        self.calls = []

    def call(self, prompt):
        value = json.loads(prompt)
        self.calls.append(value)
        obs = value["current_observation"]
        answer = {"action": "intent", "envelope": {
            "schema": "IntentEnvelope/v1", "intent_id": value["next_intent_id"],
            "principal": value["principal"], "based_on_observation_id": obs["observation_id"],
            "expires_at_frame": obs["frame"] + 900, "type": "TASK",
            "payload": {"unit": str(obs["body"]["id"]), "verb": "move_to", "args": {"x": 10, "z": 20}}}}
        text = json.dumps(answer)
        return ModelReply(text, [self.model], 1, text)


class Sink:
    def __init__(self):
        self.records = []

    def record_inference(self, observation_id, payload, envelope, label):
        self.records.append((observation_id, payload, envelope, label))
        return "inference-test"


class ManufacturedMindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.observed = {"job-2": {
            "principal": "job-2", "observation_id": "obs-job-2-900", "frame": 900,
            "body": {"id": 31945}, "permitted_intents": [{"type": "TASK", "verbs": ["move_to"]}]}}
        self.clients = {name: Client(name, self.observed)
                        for name in ("commander", "M1", *run.JOB_PRINCIPALS)}
        self.route, self.sink = Route(), Sink()
        cfg = run.SmokeConfig(engine_dir=Path(self.tmp.name), data_dir=Path(self.tmp.name),
                              out_dir=Path(self.tmp.name), rig_commit="b" * 40,
                              call_budget=2, agent_deadline_s=10, client_poll_s=0.01)
        with patch.object(run, "GatewayClient", side_effect=lambda _url, token, **kw: self.clients[token]):
            self.driver = run.make_agents(cfg, launch.RunLayout(Path(self.tmp.name)), self.sink, 1,
                                          {name: name for name in self.clients}, 1, self.route)

    def test_fixed_job_one_is_inert_but_reasoner_job_two_acts_on_own_view(self):
        for mind in self.driver.minds.values():
            mind.step()
        self.assertEqual([c["principal"] for c in self.route.calls], ["job-2"])
        submission = self.clients["job-2"].submissions[0]
        self.assertEqual(submission["principal"], "job-2")
        self.assertEqual(submission["payload"]["unit"], "31945")
        self.assertEqual(submission["based_on_observation_id"], "obs-job-2-900")
        self.assertEqual(self.sink.records[0][1]["principal"], "job-2")
        self.assertEqual(self.sink.records[0][3], "relay_move")
        self.assertFalse(self.clients["job-1"].submissions)

    def test_foreign_view_does_not_activate_an_unused_slot(self):
        self.observed["job-3"] = self.observed["job-2"]
        self.driver.minds["job-3"].step()
        self.assertEqual(self.route.calls, [])
        self.assertEqual(self.sink.records, [])

    def test_trace_does_not_claim_unused_or_fixed_slots_ran(self):
        self.driver.minds["job-2"].step()
        self.driver.minds["commander"]._latest = {"objective": {"status": "extracted"}}
        for mind in self.driver.minds.values():
            mind.start = lambda: None
        trace = self.driver.run()
        self.assertEqual(set(trace["minds"]), {"commander", "M1", "job-2"})
        self.assertEqual(trace["minds"]["job-2"]["calls"], 1)

    def test_each_job_route_gets_a_principal_neutral_child_prompt(self):
        from synapse_commander.agents.mind_prompts import CHILD_PROMPT
        self.assertNotIn('"job-1"', CHILD_PROMPT)
        self.assertEqual(set(self.driver.minds), {"commander", "M1", *run.JOB_PRINCIPALS})

    def test_run_check_requires_calls_without_requiring_a_particular_child(self):
        for calls, expected in ((0, False), (1, True)):
            with self.subTest(calls=calls):
                trace = {"agents": True, "outcome": "extracted", "minds": {
                    "commander": {"calls": calls}, "M1": {"calls": 1}, "job-2": {"calls": 1}}}
                checks = run.evaluate_rescue(trace, None, {})
                self.assertEqual(checks["live_minds_ran"]["ok"], expected)


class MindBudgetEvidenceTests(unittest.TestCase):
    def check(self, observed, traced=None, per_mind=2, total=6):
        names = ["commander", "M1", "job-2"]
        records = [{"kind": "MANIFEST", "payload": {
            "principal_sources": {name: "agent" for name in names},
            "external_budgets": {"model_calls_per_principal": per_mind, "model_calls": total}}}]
        records += [{"kind": "INFERENCE", "payload": {"principal": name, "live": True}}
                    for name, count in observed.items() for _ in range(count)]
        trace = {"agents": True, "minds": {name: {"calls": count}
                 for name, count in (observed if traced is None else traced).items()}}
        return run.rescue_ledger_checks(records, trace)["model_call_budgets"]

    def test_all_realized_minds_fit_the_declared_per_mind_and_aggregate_limits(self):
        self.assertTrue(self.check({"commander": 2, "M1": 1, "job-2": 2})["ok"])

    def test_one_mind_cannot_borrow_an_unused_minds_budget(self):
        self.assertFalse(self.check({"commander": 3, "M1": 1})["ok"])

    def test_an_incorrect_aggregate_declaration_is_refused(self):
        self.assertFalse(self.check({"commander": 1}, total=2)["ok"])

    def test_an_attempt_missing_from_the_ledger_is_not_hidden(self):
        result = self.check({"commander": 1}, traced={"commander": 2})
        self.assertFalse(result["ok"])
        self.assertFalse(result["trace_matches_ledger"])


if __name__ == "__main__":
    unittest.main()
