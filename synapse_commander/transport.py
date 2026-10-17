"""Replaceable, authority-limited transport boundary for a future runtime."""

from __future__ import annotations

from collections.abc import Mapping
from threading import Lock
from typing import Protocol

from .protocol import (
    MAX_EXPIRY_AHEAD_FRAMES,
    MAX_OBSERVATION_AGE_FRAMES,
    IntentEnvelope,
    IntentReceipt,
)


class EngineTransport(Protocol):
    """The only authority the HTTP adapter needs from an engine-side bridge.

    Implementations run outside synchronized simulation.  They may forward the
    typed envelope to a reducer, but never accept a Lua string or engine order.
    """

    def observation_for(self, team: str, principal: str | None = None) -> Mapping[str, object]: ...

    def current_frame(self) -> int: ...

    def submit_intent(self, envelope: IntentEnvelope) -> IntentReceipt: ...


class MemoryTransport:
    """Deterministic engine-dispatcher double and reference validator for tests.

    A runtime bridge replaces this object while retaining the exact two-method
    port. Its token bucket models the synchronized dispatcher's acceptance
    authority; it is not a gateway ingress limiter. Effects remain separate
    from accepted receipts.
    """

    def __init__(self, *, frame: int = 100) -> None:
        self.frame = frame
        self.observations: dict[str, dict[str, object]] = {}
        self.principal_observations: dict[tuple[str, str], dict[str, object]] = {}
        self._history: dict[str, tuple[str, dict[str, object]]] = {}
        self.observation_teams: dict[str, str] = {}
        self.accepted: list[IntentEnvelope] = []
        self._accepted_ids: set[str] = set()
        self._team_tokens: dict[str, tuple[float, int]] = {}
        self._receipt_count = 0
        self._lock = Lock()

    def publish_observation(self, team: str, observation: Mapping[str, object]) -> None:
        stored = dict(observation)
        observation_id = str(stored["observation_id"])
        self.observations[team] = stored
        self.observation_teams[observation_id] = team
        self._history[observation_id] = (team, stored)

    def observation_for(self, team: str, principal: str | None = None) -> Mapping[str, object]:
        return self.observations[team] if principal is None else self.principal_observations[(team, principal)]

    def publish_principal_observation(self, team: str, principal: str, observation: Mapping[str, object]) -> None:
        self.principal_observations[(team, principal)] = dict(observation)


    def current_frame(self) -> int:
        return self.frame

    def submit_intent(self, envelope: IntentEnvelope) -> IntentReceipt:
        with self._lock:
            self._receipt_count += 1
            receipt_id = f"receipt-{self._receipt_count}"
            observation_entry = self._history.get(envelope.based_on_observation_id)
            observation = observation_entry[1] if observation_entry and observation_entry[0] == envelope.team else None
            known_team = self.observation_teams.get(envelope.based_on_observation_id)
            if envelope.intent_id in self._accepted_ids:
                return IntentReceipt(envelope.intent_id, False, self.frame, receipt_id, "duplicate_intent_id")
            if known_team is not None and known_team != envelope.team:
                return IntentReceipt(envelope.intent_id, False, self.frame, receipt_id, "wrong_team")
            if observation is None:
                return IntentReceipt(envelope.intent_id, False, self.frame, receipt_id, "unknown_observation")
            observed_frame = observation.get("frame")
            if not isinstance(observed_frame, int) or self.frame - observed_frame > MAX_OBSERVATION_AGE_FRAMES:
                return IntentReceipt(envelope.intent_id, False, self.frame, receipt_id, "stale_observation")
            if envelope.expires_at_frame < self.frame or envelope.expires_at_frame > self.frame + MAX_EXPIRY_AHEAD_FRAMES:
                return IntentReceipt(envelope.intent_id, False, self.frame, receipt_id, "expiry_out_of_range")
            tokens, last_frame = self._team_tokens.get(envelope.team, (4.0, self.frame))
            tokens = min(4.0, tokens + max(0, self.frame - last_frame) / 15.0)
            if tokens < 1.0:
                self._team_tokens[envelope.team] = (tokens, self.frame)
                return IntentReceipt(envelope.intent_id, False, self.frame, receipt_id, "rate_limited")
            self._team_tokens[envelope.team] = (tokens - 1.0, self.frame)
            self.accepted.append(envelope)
            self._accepted_ids.add(envelope.intent_id)
            return IntentReceipt(envelope.intent_id, True, self.frame, receipt_id)
