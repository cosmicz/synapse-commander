"""Python-standard-library HTTP ingress for bounded external agent intents."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from typing import Any

from .protocol import (
    MAX_ENVELOPE_BYTES,
    IntentEnvelope,
    LedgerSink,
    ProtocolError,
    bounded_observation,
    compact_json,
)
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
    ) -> None:
        self.transport = transport
        self.principal = principal
        self.team = team
        self.origin = origin
        self.ledger = ledger
        self._observation_events: dict[str, str] = {}

    def _record_observation(self, observation: dict[str, Any]) -> None:
        if self.ledger is None:
            return
        event_id = self.ledger.record(
            "OBSERVATION",
            sim_frame=observation["frame"],
            origin="gateway",
            payload={"team": self.team, "observation": observation},
        )
        self._observation_events[observation["observation_id"]] = event_id

    def _record_submission(self, envelope: IntentEnvelope) -> str | None:
        if self.ledger is None:
            return None
        observation = self.transport.observation_for(self.team)
        observation_event = self._observation_events.get(envelope.based_on_observation_id)
        return self.ledger.record(
            "INTENT_SUBMITTED",
            sim_frame=int(observation["frame"]),
            origin=self.origin,
            payload={"envelope": envelope.to_wire()},
            causes=(observation_event,) if observation_event else (),
        )

    def _record_receipt(self, receipt: Any, submission_event: str | None) -> None:
        if self.ledger is None:
            return
        payload = {"intent_id": receipt.intent_id, "status": "accepted" if receipt.accepted else "rejected"}
        if receipt.reason:
            payload["reason_code"] = receipt.reason
        self.ledger.record(
            "INTENT_RECEIPT",
            sim_frame=receipt.frame,
            origin="gateway",
            payload=payload,
            causes=(submission_event,) if submission_event else (),
        )

    def make_server(self, port: int = 0, *, host: str = "127.0.0.1") -> ThreadingHTTPServer:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("LoopbackGateway may bind only to a loopback address")
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SynapseGateway/1"

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

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/v1/observation":
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                try:
                    observation = bounded_observation(gateway.transport.observation_for(gateway.team))
                    gateway._record_observation(observation)
                    self._send(HTTPStatus.OK, observation)
                except (KeyError, ProtocolError):
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "observation_unavailable"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/v1/intents":
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
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
                    value = json.loads(raw.decode("utf-8"))
                    envelope = IntentEnvelope.from_untrusted(value, principal=gateway.principal, team=gateway.team)
                    submission_event = gateway._record_submission(envelope)
                    receipt = gateway.transport.submit_intent(envelope)
                    gateway._record_receipt(receipt, submission_event)
                    self._send(HTTPStatus.ACCEPTED if receipt.accepted else HTTPStatus.CONFLICT, receipt.to_wire())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send(HTTPStatus.BAD_REQUEST, {"error": "malformed_json"})
                except ProtocolError as exc:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": exc.code})

        return ThreadingHTTPServer((host, port), Handler)
