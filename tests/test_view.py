"""Two-principal run-view orchestration against a mailbox-shaped fake engine."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from synapse_commander.protocol import IntentReceipt
from synapse_commander.run import SmokeConfig, run_smoke


VIEW_TABLE = {
    "id": "view_smoke", "version": "1", "seed": 1,
    "chips": {"reasoner_large_v1": 1, "reasoner_small_v1": 1},
    "chassis": {},
    "principals": {"commander": {"chip": "reasoner_large_v1"}, "M1": {"chip": "reasoner_small_v1"}},
}


class ViewEngine:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.events: list[dict] = []
        self.frame = 15
        self.stopped = False
        self.teardown: dict[str, object] = {}
        self._emit("hello", {"protocol": "synapse-mailbox/0", "run_id": run_id, "clock_mode": "continuous",
                             "scenario": {"seed": 1}, "principals": ["commander", "M1"]}, 0)
        self._emit("scenario", {"name": "view_spawn"}, 0)

    def _emit(self, kind: str, body: dict, frame: int | None = None) -> None:
        self.events.append({"seq": len(self.events) + 1, "frame": self.frame if frame is None else frame,
                            "kind": kind, "body": body})

    def start(self) -> None:
        return

    def poll(self) -> int | None:
        return 0 if self.stopped else None

    def stop(self) -> dict[str, object]:
        self.stopped = True
        self.teardown = {"group_alive_after": False, "teardown_s": 0.0, "signal": "fake"}
        return self.teardown

    def hello(self):
        return self.events[0]["body"]

    def current_frame(self) -> int:
        return self.frame

    def observation_for(self, team: str, principal: str | None = None) -> dict:
        if str(team) != "0" or principal not in {"commander", "M1"}:
            raise KeyError(principal)
        body_id = 11 if principal == "commander" else 22
        return {"schema": "ObservationFrame/v1", "principal": principal,
                "observation_id": f"obs-{principal}-{self.frame}", "team": 0, "frame": self.frame,
                "body": {"id": body_id, "pos": [body_id, 0, 0], "source": "own_body", "link": principal == "commander"},
                "entities": [], "reports": [], "resources": {},
                "scenario": {"waypoints": {"W1": [100, 0, 0]}},
                "permitted_intents": [{"type": "TASK", "verbs": ["advance"]}, {"type": "MESSAGE"}],
                "truncated": False, "omitted": 0}

    def submit_intent(self, envelope) -> IntentReceipt:
        self.frame += 1
        effect = "order_started" if envelope.type == "TASK" else "message_sent"
        self._emit("effect", {"effect": effect, "intent_id": envelope.intent_id,
                              "principal": envelope.principal, "unit_id": envelope.payload.get("unit")})
        if envelope.type == "TASK":
            self._emit("effect", {"effect": "order_done", "intent_id": envelope.intent_id,
                                  "principal": envelope.principal, "unit_id": envelope.payload.get("unit")})
        return IntentReceipt(envelope.intent_id, True, self.frame, "view-" + envelope.intent_id)

    def events_since(self, seq: int, limit: int = 256) -> list[dict]:
        return [event for event in self.events if event["seq"] > seq][:limit]

    def close(self) -> None:
        self._emit("bye", {"reason": "quit"})


class ViewRunTests(unittest.TestCase):
    def test_two_bearer_clients_produce_a_verified_separate_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            engine = ViewEngine("view-ok")
            config = SmokeConfig(engine_dir=out, data_dir=out, out_dir=out, run_id="view-ok", rig_commit="b" * 40,
                                 scenario="view_smoke", mode="view", client_timeout_s=5, client_poll_s=0.01)
            with mock.patch("synapse_commander.run.load_scenario", return_value=VIEW_TABLE), \
                    mock.patch("synapse_commander.gateway.load_scenario", return_value=VIEW_TABLE):
                verdict = run_smoke(config, transport_factory=lambda _path, _run: engine,
                                    engine_factory=lambda _layout: engine, app=("a" * 40, 0),
                                    engine_scan=lambda _pid: [])
            self.assertEqual(verdict["verdict"], "PASS", {"error": verdict.get("error"), "trace": verdict.get("trace"),
                                                             "checks": verdict.get("checks")})
            self.assertTrue(verdict["checks"]["ledger_verified"]["ok"])
            self.assertEqual(set(verdict["checks"]["ledger_verified"]["principal_classifications"]),
                             {"commander", "M1"})
            self.assertTrue((out / "view-ok" / "principals.json").exists())


if __name__ == "__main__":
    unittest.main()
