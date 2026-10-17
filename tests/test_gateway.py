from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from synapse_commander.gateway import LoopbackGateway
from synapse_commander.recording import Recorder, verify_ledger
from synapse_commander.transport import MemoryTransport
from tests.test_recording import manifest


class Ledger:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def record(self, kind: str, **kwargs: object) -> str:
        event_id = f"event-{len(self.events) + 1}"
        self.events.append({"event_id": event_id, "kind": kind, **kwargs})
        return event_id


def observation(observation_id: str = "obs-1", frame: int = 100, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "observation_id": observation_id,
        "frame": frame,
        "resources": {"material": 120},
        "entities": [],
        "recent_receipts": [],
        "recent_effects": [],
    }
    value.update(overrides)
    return value


def produce(observation_id: str = "obs-1", expiry: int = 200) -> dict[str, object]:
    return {
        "schema": "IntentEnvelope/v1",
        "intent_id": "intent-1",
        "based_on_observation_id": observation_id,
        "expires_at_frame": expiry,
        "type": "PRODUCE",
        "payload": {"factory": "F0", "chassis": "hauler", "controller": "haul_v1", "parameters": {}},
    }


class GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = MemoryTransport(frame=100)
        self.transport.publish_observation("blue", observation())
        self.server = LoopbackGateway(self.transport, principal="agent-alpha", team="blue").make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, method: str, path: str, body: object | None = None) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        encoded = None if body is None else json.dumps(body).encode()
        connection.request(method, path, body=encoded, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def test_loopback_observation_is_bounded_and_reports_omissions(self) -> None:
        self.transport.publish_observation(
            "blue",
            observation(
                entities=[{"id": str(i)} for i in reversed(range(35))],
                recent_receipts=[{"id": str(i)} for i in range(18)],
                recent_effects=[{"id": str(i)} for i in range(18)],
            ),
        )
        status, result = self.request("GET", "/v1/observation")
        self.assertEqual(status, 200)
        self.assertEqual(result["schema"], "synapse-smoke/v1")
        self.assertEqual(len(result["entities"]), 32)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["omitted"]["entities"], 3)
        self.assertEqual(result["omitted"]["recent_receipts"], 2)
        self.assertEqual(result["entities"][0]["id"], "0")
        self.assertEqual(len(result["recent_effects"]), 16)

    def test_server_is_loopback_only(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        with self.assertRaises(ValueError):
            LoopbackGateway(self.transport, principal="agent-alpha", team="blue").make_server(host="0.0.0.0")

    def test_gateway_injects_identity_and_acceptance_is_not_an_effect(self) -> None:
        request = produce()
        request["principal"] = "forged"
        request["team"] = "red"
        status, receipt = self.request("POST", "/v1/intents", request)
        self.assertEqual(status, 202)
        self.assertTrue(receipt["accepted"])
        self.assertEqual(self.transport.accepted[0].principal, "agent-alpha")
        self.assertEqual(self.transport.accepted[0].team, "blue")
        _, frame = self.request("GET", "/v1/observation")
        self.assertEqual(frame["recent_effects"], [])

    def test_malformed_and_raw_engine_commands_are_rejected(self) -> None:
        status, result = self.request("POST", "/v1/intents", {"lua": "Spring.GiveOrderToUnit(...)"})
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "unknown_field")
        status, result = self.request("POST", "/v1/intents", {"schema": "IntentEnvelope/v1", "type": "ENGINE_COMMAND", "payload": {}})
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "intent_not_allowed")
        raw_payload = produce()
        raw_payload["payload"] = {"factory": "F0", "chassis": "hauler", "controller": "haul_v1", "parameters": {"lua": "Spring.KillUnit()"}}
        status, result = self.request("POST", "/v1/intents", raw_payload)
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "invalid_payload")
        self.assertEqual(self.transport.accepted, [])

    def test_unsupported_intent_schema_is_rejected(self) -> None:
        request = produce()
        request["schema"] = "IntentEnvelope/v0"
        status, result = self.request("POST", "/v1/intents", request)
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "unsupported_version")

    def test_schema_rejects_non_string_type_and_unknown_controller_arguments(self) -> None:
        request = produce()
        request["type"] = []
        status, result = self.request("POST", "/v1/intents", request)
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "invalid_intent_type")
        request = produce()
        request["payload"]["parameters"] = {"unrecognized": True}
        status, result = self.request("POST", "/v1/intents", request)
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "invalid_payload")

    def test_stale_and_expired_submissions_are_distinct_rejections(self) -> None:
        self.transport.frame = 1100
        status, receipt = self.request("POST", "/v1/intents", produce())
        self.assertEqual(status, 409)
        self.assertEqual(receipt["reason"], "stale_observation")
        self.transport.publish_observation("blue", observation("obs-fresh", 1100))
        status, receipt = self.request("POST", "/v1/intents", produce("obs-fresh", 1099))
        self.assertEqual(status, 409)
        self.assertEqual(receipt["reason"], "expiry_out_of_range")

    def test_wrong_team_observation_cannot_be_used_by_fixed_team(self) -> None:
        self.transport.publish_observation("red", observation("red-obs"))
        status, receipt = self.request("POST", "/v1/intents", produce("red-obs"))
        self.assertEqual(status, 409)
        self.assertEqual(receipt["reason"], "wrong_team")

    def test_oversize_request_is_rejected_before_parsing(self) -> None:
        status, result = self.request("POST", "/v1/intents", {"padding": "x" * 5000})
        self.assertEqual(status, 413)
        self.assertEqual(result["error"], "envelope_too_large")

    def test_duplicate_and_simulation_rate_limit_do_not_reach_accepted_intents(self) -> None:
        status, receipt = self.request("POST", "/v1/intents", produce())
        self.assertEqual(status, 202)
        status, receipt = self.request("POST", "/v1/intents", produce())
        self.assertEqual(status, 409)
        self.assertEqual(receipt["reason"], "duplicate_intent_id")
        for index in range(2, 5):
            request = produce()
            request["intent_id"] = f"intent-{index}"
            status, receipt = self.request("POST", "/v1/intents", request)
            self.assertEqual(status, 202)
        request = produce()
        request["intent_id"] = "intent-5"
        status, receipt = self.request("POST", "/v1/intents", request)
        self.assertEqual(status, 409)
        self.assertEqual(receipt["reason"], "rate_limited")
        self.assertEqual(len(self.transport.accepted), 4)

    def test_ledger_links_submission_to_receipt_without_claiming_an_effect(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        ledger = Ledger()
        self.server = LoopbackGateway(self.transport, principal="agent-alpha", team="blue", ledger=ledger).make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.request("GET", "/v1/observation")
        self.request("POST", "/v1/intents", produce())
        self.assertEqual([event["kind"] for event in ledger.events], ["OBSERVATION", "INTENT_SUBMITTED", "INTENT_RECEIPT"])
        self.assertEqual(ledger.events[1]["causes"], ("event-1",))
        self.assertEqual(ledger.events[2]["causes"], ("event-2",))
        self.assertEqual(ledger.events[0]["payload"]["observation"]["observation_id"], "obs-1")
        self.assertEqual(ledger.events[1]["payload"]["envelope"]["payload"]["factory"], "F0")
        self.assertEqual(ledger.events[2]["payload"], {"intent_id": "intent-1", "status": "accepted"})

    def test_real_recorder_captures_flat_metadata_and_full_evidence(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            recorder = Recorder.create(path, manifest(), fsync=False)
            recorder.run_start(0)
            self.server = LoopbackGateway(self.transport, principal="agent-alpha", team="blue", origin="scripted", ledger=recorder).make_server()
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.request("GET", "/v1/observation")
            self.request("POST", "/v1/intents", produce())
            recorder.run_end(100, {"success": False})
            result = verify_ledger(path)
        self.assertTrue(result.ok, result.errors)
        observation_record = next(record for record in result.records if record["kind"] == "OBSERVATION")
        submitted = next(record for record in result.records if record["kind"] == "INTENT_SUBMITTED")
        self.assertEqual(observation_record["payload"]["observation_id"], "obs-1")
        self.assertEqual(submitted["payload"]["intent_id"], "intent-1")
        self.assertEqual(submitted["payload"]["envelope"]["payload"]["factory"], "F0")

    def test_history_allows_an_observation_until_its_simulation_age_expires(self) -> None:
        self.transport.publish_observation("blue", observation("old", 100))
        self.transport.publish_observation("blue", observation("new", 115))
        self.transport.frame = 500
        status, receipt = self.request("POST", "/v1/intents", produce("old", 800))
        self.assertEqual(status, 202)
        self.assertTrue(receipt["accepted"])

    def test_code_like_identifiers_are_not_forwarded(self) -> None:
        request = produce()
        request["payload"]["controller"] = "loadstring('os.exit()')()"
        status, result = self.request("POST", "/v1/intents", request)
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "invalid_payload")
        self.assertEqual(self.transport.accepted, [])


if __name__ == "__main__":
    unittest.main()
