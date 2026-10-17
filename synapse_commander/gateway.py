"""Python-standard-library HTTP ingress for bounded external agent intents."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import socket
from threading import RLock
from typing import Any

from .protocol import (
    MAX_ENVELOPE_BYTES,
    IntentEnvelope,
    LedgerSink,
    ProtocolError,
    bounded_observation,
    compact_json,
)
from .recording import LedgerError
from .scenarios import ScenarioError, load_scenario
from .transport import EngineTransport


class LoopbackGateway:
    """Serve one fixed principal/team on loopback only.

    The caller never selects a team or principal.  A supplied identity is
    overwritten during parsing and cannot change the port envelope.
    """

    def __init__(
        self,
        transport: EngineTransport,
        *,
        principal: str,
        team: str,
        origin: str = "agent",
        ledger: LedgerSink | None = None,
        scenario: str | None = None,
    ) -> None:
        self.transport = transport
        self.principal = principal
        self.team = team
        self.origin = origin
        self.ledger = ledger
        self._scenario = None
        if scenario is not None:
            try:
                self._scenario = load_scenario(scenario)
            except ScenarioError as error:
                raise ValueError("scenario_unavailable") from error
        self._observation_events: dict[str, str] = {}
        self._ledger_lock = RLock()

    def _record_observation(self, observation: dict[str, Any]) -> None:
        if self.ledger is None:
            return
        event_id = self.ledger.record(
            "OBSERVATION",
            sim_frame=self.transport.current_frame(),
            origin="gateway",
            payload={"observation_id": observation["observation_id"], "team": self.team, "observation": observation},
        )
        self._observation_events[observation["observation_id"]] = event_id

    def _record_submission(self, envelope: IntentEnvelope) -> str | None:
        if self.ledger is None:
            return None
        observation_event = self._observation_events.get(envelope.based_on_observation_id)
        return self.ledger.record(
            "INTENT_SUBMITTED",
            sim_frame=self.transport.current_frame(),
            origin=self.origin,
            payload={"intent_id": envelope.intent_id, "type": envelope.type,
                     "based_on_observation_id": envelope.based_on_observation_id,
                     "envelope": envelope.to_wire()},
            causes=(observation_event,) if observation_event else (),
        )

    def _record_receipt(self, receipt: Any, submission_event: str | None) -> None:
        if self.ledger is None:
            return
        payload = {"intent_id": receipt.intent_id, "status": "accepted" if receipt.accepted else "rejected"}
        payload["frame"] = receipt.frame
        if receipt.reason:
            payload["reason_code"] = receipt.reason
        self.ledger.record(
            "INTENT_RECEIPT",
            sim_frame=self.transport.current_frame(),
            origin="gateway",
            payload=payload,
            causes=(submission_event,) if submission_event else (),
        )

    def observation(self) -> dict[str, Any]:
        with self._ledger_lock:
            observation = bounded_observation(self.transport.observation_for(self.team), scenario_table=self._scenario)
            self._record_observation(observation)
            return observation

    def submit(self, value: Any) -> Any:
        with self._ledger_lock:
            envelope = IntentEnvelope.from_untrusted(value, principal=self.principal, team=self.team,
                                                     chassis=None if self._scenario is None else frozenset(self._scenario["chassis"]),
                                                     controllers=None if self._scenario is None else frozenset(self._scenario["chips"]))
            submission_event = self._record_submission(envelope)
            receipt = self.transport.submit_intent(envelope)
            if receipt.reason in {"pending", "receipt_timeout"}:
                raise TimeoutError("engine receipt is not yet known")
            self._record_receipt(receipt, submission_event)
            return receipt

    def make_server(self, port: int = 0, *, host: str = "127.0.0.1") -> ThreadingHTTPServer:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("LoopbackGateway may bind only to a loopback address")
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SynapseGateway/1"

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(1.0)

            def log_message(self, _format: str, *_args: Any) -> None:
                # Do not leak request bodies or agent identifiers to stdout.
                return

            def _send(self, status: HTTPStatus, value: dict[str, Any]) -> None:
                body = compact_json(value)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _trusted_headers(self, *, body: bool) -> bool:
                host = self.headers.get("Host")
                expected = f"127.0.0.1:{self.server.server_port}"
                if host != expected or self.headers.get("Origin") is not None:
                    self._send(HTTPStatus.FORBIDDEN, {"error": "untrusted_origin"})
                    return False
                if body and self.headers.get("Content-Type") != "application/json":
                    self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "content_type_required"})
                    return False
                return True

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/v1/observation":
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                if not self._trusted_headers(body=False):
                    return
                try:
                    observation = gateway.observation()
                    self._send(HTTPStatus.OK, observation)
                except (KeyError, ProtocolError, ValueError):
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "observation_unavailable"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/v1/intents":
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                if not self._trusted_headers(body=True):
                    return
                if self.headers.get("Content-Length") is None:
                    self._send(HTTPStatus.LENGTH_REQUIRED, {"error": "length_required"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    length = -1
                if length < 0 or length > MAX_ENVELOPE_BYTES:
                    self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "envelope_too_large"})
                    return
                raw = self.rfile.read(length)
                try:
                    value = json.loads(raw.decode("utf-8"), parse_constant=lambda _x: (_ for _ in ()).throw(ValueError()))
                    receipt = gateway.submit(value)
                    self._send(HTTPStatus.ACCEPTED if receipt.accepted else HTTPStatus.CONFLICT, receipt.to_wire())
                except LedgerError:
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "gateway_unavailable"})
                except ProtocolError as exc:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": exc.code})
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    self._send(HTTPStatus.BAD_REQUEST, {"error": "malformed_json"})
                except Exception:
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "gateway_unavailable"})

            def handle_one_request(self) -> None:
                try:
                    super().handle_one_request()
                except socket.timeout:
                    self.close_connection = True

        return ThreadingHTTPServer((host, port), Handler)
