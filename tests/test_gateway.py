from __future__ import annotations

import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from synapse_commander.gateway import LoopbackGateway
from synapse_commander.protocol import IntentEnvelope, IntentReceipt, ProtocolError
from synapse_commander.recording import Recorder, verify_ledger
from synapse_commander.recording import LedgerError
from synapse_commander.transport import MemoryTransport
from synapse_commander.scenarios import ScenarioError
from synapse_commander.run import build_manifest, gateway_sessions, mint_principal_tokens
from tests.test_recording import manifest
from tests.test_smoke_observation import provisioning_observation


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
        "payload": {"factory": "F0", "chassis": "scout_v1", "controller": "reasoner_large_v1", "parameters": {}},
    }


class GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = MemoryTransport(frame=100)
        self.transport.publish_observation("blue", observation())
        self.server = LoopbackGateway(self.transport, principal="agent-alpha", team="blue", scenario="rescue_smoke").make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, method: str, path: str, body: object | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        encoded = None if body is None else json.dumps(body).encode()
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        connection.request(method, path, body=encoded, headers=request_headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def test_loopback_observation_is_bounded_and_reports_omissions(self) -> None:
        self.transport.publish_observation(
            "blue",
            observation(
                entities=[{"id": str(i)} for i in reversed(range(35))],
                recent_receipts=[{"id": str(i), "frame": i} for i in range(20)],
                recent_effects=[{"id": str(i), "frame": i} for i in range(20)],
            ),
        )
        status, result = self.request("GET", "/v1/observation")
        self.assertEqual(status, 200)
        self.assertEqual(result["schema"], "synapse-smoke/v1")
        self.assertEqual(len(result["entities"]), 32)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["omitted"]["entities"], 3)
        self.assertEqual(result["omitted"]["recent_receipts"], 4)
        self.assertEqual(result["entities"][0]["id"], "0")
        self.assertEqual(len(result["recent_effects"]), 16)
        self.assertEqual(result["recent_receipts"][0]["frame"], 4)
        self.assertEqual(result["recent_effects"][-1]["frame"], 19)

    def test_principal_session_requires_bearer_token_and_binds_envelope(self) -> None:
        transport = MemoryTransport(frame=100)
        transport.publish_principal_observation("blue", "commander", observation(
            "obs-commander-100", principal="commander", body={"id": 1, "pos": [0, 0, 0], "source": "own_body"}))
        server = LoopbackGateway(transport, principal="legacy", team="blue",
                                 sessions={"private-token": "commander"}).make_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", "/v1/observation")
            self.assertEqual(connection.getresponse().status, 401)
            connection.close(); connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("GET", "/v1/observation", headers={"Authorization": "Bearer private-token"})
            self.assertEqual(connection.getresponse().status, 200)
        finally:
            connection.close(); server.shutdown(); thread.join(); server.server_close()

    def test_session_observation_allow_list_rejects_cross_stream_and_unserved_ids(self) -> None:
        transport = MemoryTransport(frame=100)
        transport.publish_principal_observation("blue", "commander", observation(
            "obs-commander-100", principal="commander", body={"id": 1, "pos": [0, 0, 0], "source": "own_body"}))
        transport.publish_principal_observation("blue", "M1", observation(
            "obs-M1-100", principal="M1", body={"id": 2, "pos": [1, 0, 0], "source": "own_body"}))
        gateway = LoopbackGateway(transport, principal="legacy", team="blue", sessions={"c": "commander", "m": "M1"})
        gateway.observation("commander")
        task = {"schema": "IntentEnvelope/v1", "intent_id": "cross", "principal": "M1", "based_on_observation_id": "obs-commander-100", "expires_at_frame": 200, "type": "TASK", "payload": {"unit": "7", "verb": "advance", "args": {}}}
        with self.assertRaisesRegex(ProtocolError, "not served"):
            gateway.submit(task, "M1")
        # IDs are deterministic, so an id that has never been served must
        # receive the identical gateway rejection.  Otherwise a client can
        # tell whether another principal fetched a frame from a 400/409 split.
        for observation_id in ("obs-commander-130", "obs-M1-130"):
            task["based_on_observation_id"] = observation_id
            with self.assertRaisesRegex(ProtocolError, "not served") as error:
                gateway.submit(task, "M1")
            self.assertEqual(error.exception.code, "unknown_observation")
        task["principal"] = "commander"
        with self.assertRaisesRegex(ProtocolError, "session principal"):
            gateway.submit(task, "M1")

    def test_session_unserved_ids_have_the_same_gateway_http_rejection(self) -> None:
        transport = MemoryTransport(frame=100)
        transport.publish_principal_observation("blue", "commander", observation(
            "obs-commander-100", principal="commander", body={"id": 1, "pos": [0, 0, 0], "source": "own_body"}))
        transport.publish_principal_observation("blue", "M1", observation(
            "obs-M1-100", principal="M1", body={"id": 2, "pos": [1, 0, 0], "source": "own_body"}))
        server = LoopbackGateway(transport, principal="legacy", team="blue",
                                 sessions={"commander-token": "commander", "m1-token": "M1"}).make_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()

        def request(method: str, path: str, *, token: str, body: object | None = None) -> tuple[int, dict[str, object]]:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                encoded = None if body is None else json.dumps(body).encode()
                connection.request(method, path, body=encoded,
                                   headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        try:
            self.assertEqual(request("GET", "/v1/observation", token="commander-token")[0], 200)
            for observation_id in ("obs-commander-100", "obs-commander-130", "obs-M1-130"):
                task = {"schema": "IntentEnvelope/v1", "intent_id": f"probe-{observation_id}", "principal": "M1",
                        "based_on_observation_id": observation_id, "expires_at_frame": 200, "type": "TASK",
                        "payload": {"unit": "7", "verb": "advance", "args": {}}}
                status, result = request("POST", "/v1/intents", token="m1-token", body=task)
                self.assertEqual((status, result["error"]), (400, "unknown_observation"))
        finally:
            server.shutdown(); thread.join(); server.server_close()

    def test_session_observation_allow_list_expires_after_engine_window(self) -> None:
        transport = MemoryTransport(frame=100)
        transport.publish_principal_observation("blue", "M1", observation(
            "obs-M1-100", principal="M1", body={"id": 2, "pos": [1, 0, 0], "source": "own_body"}))
        gateway = LoopbackGateway(transport, principal="legacy", team="blue", sessions={"m": "M1"})
        gateway.observation("M1")
        transport.frame = 1001
        task = {"schema": "IntentEnvelope/v1", "intent_id": "expired-history", "principal": "M1",
                "based_on_observation_id": "obs-M1-100", "expires_at_frame": 1100, "type": "TASK",
                "payload": {"unit": "7", "verb": "advance", "args": {}}}
        with self.assertRaisesRegex(ProtocolError, "not served") as error:
            gateway.submit(task, "M1")
        self.assertEqual(error.exception.code, "unknown_observation")

    def test_principal_session_requires_the_envelope_principal(self) -> None:
        transport = MemoryTransport(frame=100)
        transport.publish_principal_observation("blue", "commander", observation(
            "obs-commander-100", principal="commander", body={"id": 1, "pos": [0, 0, 0], "source": "own_body"}))
        gateway = LoopbackGateway(transport, principal="legacy", team="blue", sessions={"c": "commander"})
        missing = {"schema": "IntentEnvelope/v1", "intent_id": "missing-principal",
                   "based_on_observation_id": "obs-commander-100", "expires_at_frame": 200,
                   "type": "TASK", "payload": {"unit": "7", "verb": "advance", "args": {}}}
        with self.assertRaisesRegex(ProtocolError, "session principal"):
            gateway.submit(missing, "commander")

    def test_principal_observation_keeps_only_typed_body_reports_and_message_permission(self) -> None:
        transport = MemoryTransport(frame=100)
        transport.publish_principal_observation("blue", "M1", observation(
            "obs-M1-100", principal="M1", body={"id": 2, "pos": [1, 0, 0], "source": "own_body", "link": False,
                                                   "sensor_range": 900,
                                                   "private": "not forwarded"},
            reports=[{"kind": "message", "from": "commander", "text": "hold position",
                      "observed_frame": 70, "delivered_frame": 100, "age_frames": 30}],
            link_state=[{"unit_id": 7, "linked": True}, {"unit_id": 5, "linked": False}],
            permitted_intents=[{"type": "TASK", "verbs": ["advance"]}, {"type": "MESSAGE"}]))
        gateway = LoopbackGateway(transport, principal="legacy", team="blue", scenario="rescue_smoke",
                                  sessions={"m": "M1", "c": "commander"})
        result = gateway.observation("M1")
        self.assertEqual(result["body"], {"id": 2, "pos": [1, 0, 0], "source": "own_body", "link": False,
                                           "sensor_range": 900})
        self.assertEqual(result["reports"], [{"kind": "message", "from": "commander", "text": "hold position",
                                                "observed_frame": 70, "delivered_frame": 100, "age_frames": 30}])
        self.assertEqual(result["link_state"], [{"unit_id": 5, "linked": False}, {"unit_id": 7, "linked": True}])
        self.assertIn({"type": "MESSAGE"}, result["permitted_intents"])

    def test_principal_tokens_are_private_run_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            tokens = mint_principal_tokens(path, ["commander", "M1"])
            self.assertEqual(set(tokens), {"commander", "M1"})
            self.assertEqual(json.loads((path / "principals.json").read_text()), tokens)
            self.assertEqual((path / "principals.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(set(gateway_sessions(tokens).values()), {"commander", "M1"})

    def test_manifest_declares_all_principal_sources(self) -> None:
        identity = {name: {"name": name, "version": "1"} for name in ("engine", "game", "map")}
        manifest_value = build_manifest(run_id="r", rig_commit="a" * 40, app_commit="b" * 40,
                                        identity=identity, config_hashes={}, principals=["commander", "M1"],
                                        principal_sources={"commander": "scripted", "M1": "agent"})
        self.assertEqual(manifest_value["principals"], ["commander", "M1"])
        self.assertEqual(manifest_value["principal_sources"]["M1"], "agent")

    def test_server_is_loopback_only(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        with self.assertRaises(ValueError):
            LoopbackGateway(self.transport, principal="agent-alpha", team="blue").make_server(host="0.0.0.0")

    def test_selected_unavailable_scenario_fails_at_gateway_startup(self) -> None:
        with patch("synapse_commander.gateway.load_scenario", side_effect=ScenarioError("broken")):
            with self.assertRaisesRegex(ValueError, "scenario_unavailable"):
                LoopbackGateway(self.transport, principal="agent-alpha", team="blue", scenario="rescue_smoke")

    def test_cross_origin_and_non_json_ingress_are_rejected_before_transport(self) -> None:
        status, body = self.request("POST", "/v1/intents", produce(), {"Origin": "https://evil.example"})
        self.assertEqual((status, body["error"]), (403, "untrusted_origin"))
        status, body = self.request("POST", "/v1/intents", produce(), {"Content-Type": "text/plain"})
        self.assertEqual((status, body["error"]), (415, "content_type_required"))
        self.assertEqual(self.transport.accepted, [])

    def test_rebinding_host_and_oversize_observation_are_rejected(self) -> None:
        status, body = self.request("GET", "/v1/observation", headers={"Host": "evil.example:80"})
        self.assertEqual((status, body["error"]), (403, "untrusted_origin"))
        self.transport.publish_observation("blue", observation(resources={"blob": "x" * 33000}))
        status, body = self.request("GET", "/v1/observation")
        self.assertEqual((status, body["error"]), (503, "observation_unavailable"))

    def test_observation_ledger_failure_returns_stable_503(self) -> None:
        class FailingLedger:
            def record(self, *_args, **_kwargs):
                raise LedgerError("ledger_closed")
        self.server.shutdown(); self.thread.join(); self.server.server_close()
        self.server = LoopbackGateway(self.transport, principal="agent-alpha", team="blue", ledger=FailingLedger()).make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        status, body = self.request("GET", "/v1/observation")
        self.assertEqual((status, body["error"]), (503, "observation_unavailable"))

    def test_short_body_times_out_and_closes_connection(self) -> None:
        connection = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        request = f"POST /v1/intents HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\nContent-Type: application/json\r\nContent-Length: 16\r\n\r\n{{".encode()
        connection.sendall(request)
        connection.settimeout(2)
        self.assertEqual(connection.recv(1), b"")
        connection.close()

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
        raw_payload["payload"] = {"factory": "F0", "chassis": "scout_v1", "controller": "reasoner_large_v1", "parameters": {"lua": "Spring.KillUnit()"}}
        status, result = self.request("POST", "/v1/intents", raw_payload)
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "invalid_parameters")
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
        self.assertEqual(result["error"], "invalid_parameters")

    def test_selected_production_vocabulary_has_stable_reason_codes(self) -> None:
        request = produce()
        request["payload"]["chassis"] = "not_a_chassis"
        status, result = self.request("POST", "/v1/intents", request)
        self.assertEqual((status, result["error"]), (400, "unknown_chassis"))
        request = produce()
        request["payload"]["controller"] = "not_a_controller"
        status, result = self.request("POST", "/v1/intents", request)
        self.assertEqual((status, result["error"]), (400, "unknown_controller"))

    def test_only_engine_smoke_advance_task_verb_is_allowed(self) -> None:
        request = {"schema": "IntentEnvelope/v1", "intent_id": "task-1", "based_on_observation_id": "obs-1", "expires_at_frame": 200, "type": "TASK", "payload": {"unit": "H1", "verb": "move", "args": {}}}
        status, result = self.request("POST", "/v1/intents", request)
        self.assertEqual((status, result["error"]), (400, "verb_not_supported"))
        self.assertEqual(self.transport.accepted, [])

    def test_principal_move_to_has_exact_integer_coordinate_arguments(self) -> None:
        request = {"schema": "IntentEnvelope/v1", "intent_id": "move-1", "principal": "M1",
                   "based_on_observation_id": "obs-M1-100", "expires_at_frame": 200, "type": "TASK",
                   "payload": {"unit": "M1", "verb": "move_to", "args": {"x": 100, "z": 200}}}
        parsed = IntentEnvelope.from_untrusted(request, principal="M1", team="0", principals=frozenset({"M1"}))
        self.assertEqual(parsed.payload["args"], {"x": 100, "z": 200})
        with self.assertRaisesRegex(ProtocolError, "integer x and z"):
            IntentEnvelope.from_untrusted({**request, "payload": {**request["payload"], "args": {"x": 100.5, "z": 200}}},
                                         principal="M1", team="0", principals=frozenset({"M1"}))
        for args in ({"x": -1, "z": 0}, {"x": 0, "z": 65537}):
            with self.subTest(args=args), self.assertRaisesRegex(ProtocolError, "outside the map bounds"):
                IntentEnvelope.from_untrusted({**request, "payload": {**request["payload"], "args": args}},
                                              principal="M1", team="0", principals=frozenset({"M1"}))
        with self.assertRaisesRegex(ProtocolError, "not exposed"):
            IntentEnvelope.from_untrusted(request, principal="M1", team="0")

    def test_message_requires_principal_scenario_and_is_bounded(self) -> None:
        message = {"schema": "IntentEnvelope/v1", "intent_id": "message-1", "based_on_observation_id": "obs-1", "expires_at_frame": 200, "type": "MESSAGE", "payload": {"to": "M1", "text": "hello"}}
        status, result = self.request("POST", "/v1/intents", message)
        self.assertEqual((status, result["error"]), (400, "intent_not_allowed"))

    def test_non_finite_json_is_rejected_at_parse_boundary(self) -> None:
        request = produce(); request["expires_at_frame"] = float("nan")
        status, result = self.request("POST", "/v1/intents", request)
        self.assertEqual((status, result["error"]), (400, "malformed_json"))

    def test_stale_and_expired_submissions_are_distinct_rejections(self) -> None:
        self.transport.frame = 1100
        status, receipt = self.request("POST", "/v1/intents", produce())
        self.assertEqual(status, 409)
        self.assertEqual(receipt["reason"], "stale_observation")
        self.transport.publish_observation("blue", observation("obs-fresh", 1100))
        status, receipt = self.request("POST", "/v1/intents", produce("obs-fresh", 1099))
        self.assertEqual(status, 409)
        self.assertEqual(receipt["reason"], "expiry_out_of_range")

    def test_engine_immobilised_refusal_is_preserved(self) -> None:
        class ImmobilisedTransport(MemoryTransport):
            def submit_intent(self, envelope, wait_s: float = 1.0):
                return IntentReceipt(envelope.intent_id, False, self.frame, "engine-immobilised", "immobilised")

        self.server.shutdown(); self.thread.join(); self.server.server_close()
        self.transport = ImmobilisedTransport(frame=100)
        self.transport.publish_observation("blue", observation())
        self.server = LoopbackGateway(self.transport, principal="agent-alpha", team="blue").make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        status, receipt = self.request("POST", "/v1/intents", produce())
        self.assertEqual((status, receipt["accepted"], receipt["reason"]), (409, False, "immobilised"))

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
        self.assertEqual(ledger.events[2]["payload"], {"intent_id": "intent-1", "status": "accepted", "principal": "agent-alpha", "client": "agent-alpha", "frame": 100})

    def test_transport_timeout_returns_503_without_a_receipt_record(self) -> None:
        class TimeoutTransport(MemoryTransport):
            def submit_intent(self, envelope, wait_s: float = 1.0):
                raise TimeoutError("no synced receipt")
        self.server.shutdown(); self.thread.join(); self.server.server_close()
        self.transport = TimeoutTransport(frame=100)
        self.transport.publish_observation("blue", observation())
        ledger = Ledger()
        self.server = LoopbackGateway(self.transport, principal="agent-alpha", team="blue", ledger=ledger).make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        status, receipt = self.request("POST", "/v1/intents", produce())
        self.assertEqual(status, 503)
        self.assertEqual(receipt["error"], "gateway_unavailable")
        self.assertEqual([event["kind"] for event in ledger.events], ["INTENT_SUBMITTED"])

    def test_timeout_receipt_can_be_polled_without_replaying_the_intent(self) -> None:
        class DeferredReceiptTransport(MemoryTransport):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.waits: list[float] = []
                self.delayed: IntentReceipt | None = None

            def submit_intent(self, envelope, wait_s: float = 1.0):
                self.waits.append(wait_s)
                raise TimeoutError("engine is still drawing")

            def receipt(self, intent_id: str):
                return self.delayed if self.delayed and self.delayed.intent_id == intent_id else None

        self.server.shutdown(); self.thread.join(); self.server.server_close()
        self.transport = DeferredReceiptTransport(frame=100)
        self.transport.publish_observation("blue", observation())
        ledger = Ledger()
        self.server = LoopbackGateway(self.transport, principal="agent-alpha", team="blue", ledger=ledger,
                                      receipt_wait_s=4.5).make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        status, body = self.request("POST", "/v1/intents", produce())
        self.assertEqual((status, body["error"]), (503, "gateway_unavailable"))
        self.assertEqual(self.transport.waits, [4.5])
        self.assertEqual([event["kind"] for event in ledger.events], ["INTENT_SUBMITTED"])
        self.assertEqual(self.request("GET", "/v1/receipts/intent-1"), (404, {"error": "not_found"}))
        self.transport.delayed = IntentReceipt("intent-1", True, 125, "engine-intent-1")
        status, body = self.request("GET", "/v1/receipts/intent-1")
        self.assertEqual((status, body["accepted"]), (200, True))
        self.assertEqual(self.transport.waits, [4.5], "polling must not submit another attempt")
        self.assertEqual([event["kind"] for event in ledger.events], ["INTENT_SUBMITTED", "INTENT_RECEIPT"])
        self.assertEqual(ledger.events[1]["causes"], (ledger.events[0]["event_id"],))

    def test_legacy_timeout_receipt_is_also_unknown_without_a_receipt_record(self) -> None:
        class LegacyTimeout(MemoryTransport):
            def submit_intent(self, envelope, wait_s: float = 1.0):
                return IntentReceipt(envelope.intent_id, False, 9, "legacy", "receipt_timeout")
        self.server.shutdown(); self.thread.join(); self.server.server_close()
        self.transport = LegacyTimeout(frame=700); self.transport.publish_observation("blue", observation(frame=100))
        ledger = Ledger(); self.server = LoopbackGateway(self.transport, principal="agent-alpha", team="blue", ledger=ledger).make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        status, body = self.request("POST", "/v1/intents", produce(expiry=800))
        self.assertEqual(status, 503); self.assertEqual(body["error"], "gateway_unavailable")
        self.assertEqual([event["kind"] for event in ledger.events], ["INTENT_SUBMITTED"])
        self.assertEqual(ledger.events[0]["sim_frame"], 700)

    def test_accepted_ledger_records_use_ingestion_frame_not_source_frames(self) -> None:
        class OldReceipt(MemoryTransport):
            def submit_intent(self, envelope, wait_s: float = 1.0):
                return IntentReceipt(envelope.intent_id, True, 9, "old-receipt")
        self.server.shutdown(); self.thread.join(); self.server.server_close()
        self.transport = OldReceipt(frame=700)
        self.transport.publish_observation("blue", observation("source", 100))
        ledger = Ledger(); self.server = LoopbackGateway(self.transport, principal="agent-alpha", team="blue", ledger=ledger).make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.request("GET", "/v1/observation")
        self.request("POST", "/v1/intents", produce("source", 800))
        self.assertEqual([event["sim_frame"] for event in ledger.events], [700, 700, 700])
        self.assertEqual(ledger.events[2]["payload"]["frame"], 9)

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

    def test_real_recorder_preserves_duplicate_attempt_and_its_rejection(self) -> None:
        self.server.shutdown(); self.thread.join(); self.server.server_close()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"; recorder = Recorder.create(path, manifest(), fsync=False); recorder.run_start(0)
            self.server = LoopbackGateway(self.transport, principal="agent-alpha", team="blue", origin="scripted", ledger=recorder).make_server()
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
            self.request("GET", "/v1/observation")
            self.assertEqual(self.request("POST", "/v1/intents", produce())[0], 202)
            self.assertEqual(self.request("POST", "/v1/intents", produce())[0], 409)
            recorder.run_end(100, {"success": False}); result = verify_ledger(path)
        self.assertTrue(result.ok, result.errors)
        submitted = [r for r in result.records if r["kind"] == "INTENT_SUBMITTED"]
        receipts = [r for r in result.records if r["kind"] == "INTENT_RECEIPT"]
        self.assertEqual(len(submitted), 2); self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[1]["payload"]["reason_code"], "duplicate_intent_id")
        self.assertEqual(receipts[1]["causes"], [submitted[1]["event_id"]])

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

    def test_closed_controller_and_install_version_enums_reject_validly_shaped_unknowns(self) -> None:
        request = produce(); request["payload"]["controller"] = "haul_v9"
        status, result = self.request("POST", "/v1/intents", request)
        self.assertEqual((status, result["error"]), (400, "unknown_controller"))

    def test_closed_chassis_enum_rejects_validly_shaped_unknown(self) -> None:
        request = produce(); request["payload"]["chassis"] = "hauler_v9"
        status, result = self.request("POST", "/v1/intents", request)
        self.assertEqual((status, result["error"]), (400, "unknown_chassis"))
        install = {"schema": "IntentEnvelope/v1", "intent_id": "install-1", "based_on_observation_id": "obs-1", "expires_at_frame": 200, "type": "INSTALL", "payload": {"unit": "H1", "expected_version": "reasoner_large_v1", "replacement_version": "haul_v9", "parameters": {}}}
        status, result = self.request("POST", "/v1/intents", install)
        self.assertEqual((status, result["error"]), (400, "unknown_controller"))

    def test_declared_produce_shape_reaches_transport_even_when_inventory_is_empty(self) -> None:
        self.transport.publish_observation("blue", provisioning_observation())
        status, receipt = self.request("POST", "/v1/intents", produce("obs-0-15"))
        self.assertEqual((status, receipt["accepted"]), (202, True))
        envelope = self.transport.accepted[-1]
        self.assertEqual(envelope.payload["factory"], "F0")
        self.assertEqual(envelope.payload["chassis"], "scout_v1")
        self.assertEqual(envelope.payload["controller"], "reasoner_large_v1")


if __name__ == "__main__":
    unittest.main()
