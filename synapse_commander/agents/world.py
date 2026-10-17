"""Scripted initial-world observer: reads the rescue world over HTTP, sends nothing.

It records every observation the gateway serves for a bounded span of
simulation time so the verdict can check the roster, budgets and the aged
distress report against the evaluator-only truth in the ledger.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .scripted import ClientError, GatewayClient


class WorldObserver:
    def __init__(self, client: GatewayClient, *, span_frames: int = 90, min_observations: int = 3,
                 sleep: Callable[[float], None] = time.sleep, poll_s: float = 0.25,
                 wall_timeout_s: float = 120.0) -> None:
        self.client = client
        self.span_frames = span_frames
        self.min_observations = min_observations
        self.sleep = sleep
        self.poll_s = poll_s
        self.wall_timeout_s = wall_timeout_s
        self.trace: dict[str, Any] = {"observations": []}

    def run(self) -> dict[str, Any]:
        deadline = time.monotonic() + self.wall_timeout_s
        seen: dict[str, dict[str, Any]] = {}
        while time.monotonic() < deadline:
            status, observation = self.client.observation()
            if status == 200 and observation.get("roster") is not None:
                if observation["observation_id"] not in seen:
                    seen[observation["observation_id"]] = observation
                    self.trace["observations"].append(observation)
                frames = [o["frame"] for o in self.trace["observations"]]
                if len(frames) >= self.min_observations and max(frames) - min(frames) >= self.span_frames:
                    return self.trace
            self.sleep(self.poll_s)
        raise ClientError(f"saw {len(self.trace['observations'])} world observations, "
                          f"needed {self.min_observations} spanning {self.span_frames} frames")
