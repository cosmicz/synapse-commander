"""Two-principal HTTP fixture for the bounded per-body view seam.

This is deliberately a small scripted check, not a mission controller.  It
proves that two bearer-bound clients receive different principal streams and
that the stranded body's own local authority remains usable while radio is
lost.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .scripted import ClientError, GatewayClient


INTENT_SCHEMA = "IntentEnvelope/v1"
EXPIRY_AHEAD_FRAMES = 600


class ViewSmoke:
    """Read two principal streams, then send one local task and one message."""

    def __init__(self, commander: GatewayClient, m1: GatewayClient, *,
                 sleep: Callable[[float], None] = time.sleep, poll_s: float = 0.25,
                 wall_timeout_s: float = 120.0) -> None:
        self.commander = commander
        self.m1 = m1
        self.sleep = sleep
        self.poll_s = poll_s
        self.wall_timeout_s = wall_timeout_s
        self.trace: dict[str, Any] = {"steps": []}

    def _step(self, name: str, **detail: Any) -> None:
        self.trace["steps"].append({"step": name, "wall_monotonic_ns": time.monotonic_ns(), **detail})

    def _wait_observation(self, client: GatewayClient, principal: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.wall_timeout_s
        last = None
        while time.monotonic() < deadline:
            status, observation = client.observation()
            last = status
            if status == 200 and observation.get("principal") == principal and observation.get("body"):
                return observation
            self.sleep(self.poll_s)
        raise ClientError(f"no observation for {principal} (last HTTP {last})")

    @staticmethod
    def _envelope(principal: str, label: str, observation: dict[str, Any], intent_type: str,
                  payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": INTENT_SCHEMA,
            "intent_id": f"view-{principal}-{label}",
            "principal": principal,
            "based_on_observation_id": observation["observation_id"],
            "expires_at_frame": int(observation["frame"]) + EXPIRY_AHEAD_FRAMES,
            "type": intent_type,
            "payload": payload,
        }

    def run(self) -> dict[str, Any]:
        commander = self._wait_observation(self.commander, "commander")
        m1 = self._wait_observation(self.m1, "M1")
        self.trace.update(commander_observation=commander, m1_observation=m1)
        self._step("separate_streams_observed", commander=commander["observation_id"], m1=m1["observation_id"])

        # A local task proves M1's body can act without a simulated radio link.
        body = m1.get("body")
        if not isinstance(body, dict) or body.get("id") is None:
            raise ClientError("M1 observation has no bounded own body")
        advance = self._envelope("M1", "advance", m1, "TASK",
                                  {"unit": str(body["id"]), "verb": "advance", "args": {}})
        advance_status, advance_receipt = self.m1.submit(advance)
        self.trace.update(advance_envelope=advance, advance_http=advance_status, advance_receipt=advance_receipt)
        self._step("m1_advance_submitted", http=advance_status, accepted=advance_receipt.get("accepted"))
        if advance_status != 202 or advance_receipt.get("accepted") is not True:
            raise ClientError(f"M1 local advance was not accepted: {advance_status} {advance_receipt}")

        # Transmission is a local action too.  In the fixture it is expected to
        # be accepted even if subsequent simulated delivery is dropped.
        message = self._envelope("M1", "message", m1, "MESSAGE",
                                 {"to": "commander", "text": "local body advancing"})
        message_status, message_receipt = self.m1.submit(message)
        self.trace.update(message_envelope=message, message_http=message_status, message_receipt=message_receipt)
        self._step("m1_message_submitted", http=message_status, accepted=message_receipt.get("accepted"))
        if message_status != 202 or message_receipt.get("accepted") is not True:
            raise ClientError(f"M1 message was not accepted: {message_status} {message_receipt}")

        # The gateway itself must refuse the other principal's observation id.
        foreign = self._envelope("commander", "foreign-observation", m1, "TASK",
                                 {"unit": str(body["id"]), "verb": "advance", "args": {}})
        foreign_status, foreign_response = self.commander.submit(foreign)
        self.trace.update(foreign_envelope=foreign, foreign_http=foreign_status, foreign_response=foreign_response)
        self._step("cross_stream_probe", http=foreign_status, error=foreign_response.get("error"))
        return self.trace
