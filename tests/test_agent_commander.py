"""Agent commander without a live model or the engine.

A fake model route stands in for the CLI and the in-process fake engine from
the orchestration tests stands in for Recoil. These cover parsing, validation,
ledger linkage and the agent_only classification rules; they are not evidence
that any real model can play.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from synapse_commander.agents.commander import (
    ClaudeCliRoute,
    ModelReply,
    build_prompt,
    parse_decision,
)
from synapse_commander.recording import verify_ledger
from synapse_commander.run import SmokeConfig, run_smoke
from tests.test_loop_orchestration import FakeEngine


MODEL = "fake-model-1"


def _observation_from(prompt: str) -> dict:
    lines = prompt.splitlines()
    return json.loads(lines[lines.index("Current observation:") + 1])


class FakeModel:
    """Scripted stand-in for a model: one bad reply, then an advance, then waits."""

    name = "fake-route"

    def __init__(self, *, model_ids=(MODEL,), first_invalid=True, target=None, tamper=False):
        self.model_ids = list(model_ids)
        self.first_invalid = first_invalid
        self.target = target
        self.tamper = tamper
        self.calls = 0
        self.prompts: list[str] = []

    def call(self, prompt: str) -> ModelReply:
        self.calls += 1
        self.prompts.append(prompt)
        observation = _observation_from(prompt)
        if self.first_invalid and self.calls == 1:
            return ModelReply("Sure! I will advance the unit.", self.model_ids, 5, "{}")
        if "advance" in prompt and '"http": 202' in prompt:
            text = json.dumps({"action": "wait", "reason": "unit is moving"})
        else:
            own = [e for e in observation["entities"] if e.get("source") == "own_unit"]
            w1 = observation["scenario"]["waypoints"]["W1"]
            unit = self.target or min(own, key=lambda e: abs(e["pos"][0] - (w1[0] - 400)) + abs(e["pos"][2] - w1[2]))["id"]
            text = json.dumps({"action": "intent", "envelope": {
                "schema": "IntentEnvelope/v1", "intent_id": f"agent-{self.calls}",
                "based_on_observation_id": observation["observation_id"],
                "expires_at_frame": observation["frame"] + 300, "type": "TASK",
                "payload": {"unit": str(unit), "verb": "advance", "args": {}}}})
        return ModelReply(text, self.model_ids, 7, json.dumps({"result": text}))


class ParseDecisionTests(unittest.TestCase):
    observation = {"observation_id": "obs-0-30", "frame": 30,
                   "permitted_intents": [{"type": "TASK", "verbs": ["advance"]}]}

    def envelope(self, **overrides):
        value = {"schema": "IntentEnvelope/v1", "intent_id": "a1", "based_on_observation_id": "obs-0-30",
                 "expires_at_frame": 330, "type": "TASK", "payload": {"unit": "7", "verb": "advance", "args": {}}}
        value.update(overrides)
        return json.dumps({"action": "intent", "envelope": value})

    def test_valid_intent_and_identity_is_not_forwarded(self) -> None:
        text = self.envelope(principal="me", team="9")
        decision = parse_decision(text, self.observation, set())
        self.assertEqual(decision.action, "intent")
        self.assertNotIn("principal", decision.envelope)
        self.assertNotIn("team", decision.envelope)

    def test_rejections_are_explicit(self) -> None:
        cases = {
            "prose then json": "Here you go: " + self.envelope(),
            "two objects": self.envelope() + self.envelope(),
            "other observation": self.envelope(based_on_observation_id="obs-0-15"),
            "expired": self.envelope(expires_at_frame=30),
            "too far": self.envelope(expires_at_frame=931),
            "not permitted": json.dumps({"action": "intent", "envelope": json.loads(self.envelope(
                type="PRODUCE", payload={"factory": "F0", "chassis": "hauler", "controller": "haul_v1",
                                         "parameters": {}}))["envelope"]}),
            "raw key": self.envelope(payload={"unit": "7", "verb": "advance", "args": {}, "lua": "x"}),
            "unknown action": json.dumps({"action": "console", "command": "x"}),
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertEqual(parse_decision(text, self.observation, set()).action, "invalid")
        self.assertEqual(parse_decision(self.envelope(), self.observation, {"a1"}).rejected, "intent_id reused")

    def test_wait_and_done(self) -> None:
        self.assertEqual(parse_decision('{"action": "wait", "reason": "x"}', self.observation, set()).action, "wait")
        self.assertEqual(parse_decision('```json\n{"action": "done"}\n```', self.observation, set()).action, "done")

    def test_prompt_carries_observation_and_history(self) -> None:
        prompt = build_prompt({"observation_id": "o", "frame": 1, "entities": [], "recent_effects": []},
                              [{"call": 1, "waited": "x"}])
        self.assertIn('"observation_id": "o"', prompt)
        self.assertNotIn("recent_effects", prompt)
        self.assertIn('"waited": "x"', prompt)


class ClaudeCliRouteTests(unittest.TestCase):
    def test_invocation_is_bounded_and_toolless(self) -> None:
        seen = {}

        def runner(argv, **kwargs):
            seen.update(argv=argv, **kwargs)
            out = {"type": "result", "is_error": False, "result": '{"action": "wait"}',
                   "modelUsage": {"claude-sonnet-5": {"inputTokens": 1}}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")

        reply = ClaudeCliRoute("claude-sonnet-5", timeout_s=60, runner=runner).call("p")
        self.assertIsNone(reply.error)
        self.assertEqual(reply.model_ids, ["claude-sonnet-5"])
        self.assertEqual(reply.text, '{"action": "wait"}')
        argv = seen["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5")
        self.assertIs(seen["stdin"], subprocess.DEVNULL)
        self.assertEqual(seen["timeout"], 60)

    def test_failures_become_errors_not_decisions(self) -> None:
        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 60)

        def failing(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, "", "boom")

        def erroring(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, json.dumps({"is_error": True, "result": "x"}), "")

        for runner in (timeout, failing, erroring):
            with self.subTest(runner.__name__):
                self.assertIsNotNone(ClaudeCliRoute("m", runner=runner).call("p").error)


class AgentRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def run_agent(self, run_id: str, route: FakeModel, **fake) -> dict:
        engine = FakeEngine(run_id, **fake)
        config = SmokeConfig(engine_dir=self.out, data_dir=self.out, out_dir=self.out, run_id=run_id,
                             rig_commit="b" * 40, hello_timeout_s=10, client_poll_s=0.02, mode="agent",
                             model=MODEL, call_budget=8, agent_deadline_s=30)
        return run_smoke(config, transport_factory=lambda _d, _i: engine, engine_factory=lambda _l: engine,
                         app=("a" * 40, 0), engine_scan=lambda _p: [], model_route=route)

    def test_live_decisions_classify_agent_only(self) -> None:
        route = FakeModel()
        verdict = self.run_agent("agent-ok", route)
        self.assertEqual(verdict["verdict"], "PASS", verdict["failed_checks"])
        checks = verdict["checks"]
        self.assertEqual(checks["classified_agent_only"]["classification"], "agent_only")
        self.assertEqual(checks["invalid_outputs_rejected_and_counted"]["invalid"], 1)
        self.assertTrue(checks["ledger_causal_order"]["submission_cites_inference"])
        run_dir = self.out / "agent-ok"
        decisions = [json.loads(line) for line in (run_dir / "decisions.jsonl").read_text().splitlines()]
        self.assertEqual(len(decisions), route.calls)
        self.assertIn("raw_output", decisions[0])
        ledger = (run_dir / "ledger.jsonl").read_text()
        self.assertNotIn("Sure! I will advance", ledger, "raw model text stays out of the ledger")
        self.assertTrue(verify_ledger(run_dir / "ledger.jsonl").ok)

    def test_undeclared_model_is_not_agent_only(self) -> None:
        verdict = self.run_agent("agent-other-model", FakeModel(model_ids=("some-other-model",)))
        self.assertFalse(verdict["checks"]["classified_agent_only"]["ok"])
        self.assertTrue(verdict["checks"]["advance_accepted"]["ok"])
        self.assertEqual(verdict["verdict"], "FAIL")

    def test_rejected_choices_are_recorded_and_fail_honestly(self) -> None:
        # The fake engine rejects any unit but its smoke unit; the commander keeps
        # deciding until its call budget is spent, and nothing is substituted.
        route = FakeModel(target=50, first_invalid=False)
        verdict = self.run_agent("agent-rejected", route)
        self.assertEqual(verdict["verdict"], "FAIL")
        self.assertFalse(verdict["checks"]["advance_accepted"]["ok"])
        self.assertEqual(route.calls, 8)
        trace = json.loads((self.out / "agent-rejected" / "trace.json").read_text())
        self.assertEqual(trace["stop"], "call_budget_exhausted")
        self.assertTrue(all(d["http"] == 409 for d in trace["decisions"]))

if __name__ == "__main__":
    unittest.main()
