"""Scripted reference client for the Loop 0 smoke.

It talks only HTTP to the loopback gateway: it reads observation frames and
posts intent envelopes.  Every intent here was written before the run, so a
run driven by this client is a scripted fixture, never autonomous play.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import time
from typing import Any, Callable
import urllib.error
import urllib.request


INTENT_SCHEMA = "IntentEnvelope/v1"
ADVANCE_LIMIT_FRAMES = 300
MIN_DISPLACEMENT = 50.0
STILL_DISTANCE = 8.0
STALE_AFTER_FRAMES = 900
SPAWN_OFFSET_X = 400.0


class ClientError(RuntimeError):
    pass


class GatewayClient:
    """Minimal JSON-over-HTTP client for the two gateway routes."""

    def __init__(self, base_url: str, *, timeout_s: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                return exc.code, {}

    def observation(self) -> tuple[int, dict[str, Any]]:
        return self._request("GET", "/v1/observation")

    def submit(self, envelope: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return self._request("POST", "/v1/intents", envelope)


def _own_units(observation: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in observation.get("entities", []) if e.get("source") == "own_unit"]


def _foreign_units(observation: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in observation.get("entities", []) if e.get("source") not in (None, "own_unit")]


def _entity(observation: dict[str, Any], entity_id: Any) -> dict[str, Any] | None:
    for entity in observation.get("entities", []):
        if entity.get("id") == entity_id:
            return entity
    return None


def planar_distance(a: list[float], b: list[float]) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[2]) - float(b[2]))


def smoke_unit(observation: dict[str, Any], w1: list[float] | None) -> dict[str, Any]:
    """The owned unit spawned for the smoke: the one nearest W1 - (400, 0, 0).

    The scenario places W1 400 map units along +x from that unit's spawn; other
    owned units (such as the team's start commander) are left alone.
    """
    own = _own_units(observation)
    if w1 is None:
        return own[0]
    spawn = [float(w1[0]) - SPAWN_OFFSET_X, float(w1[1]), float(w1[2])]
    return min(own, key=lambda e: (planar_distance(e["pos"], spawn), str(e.get("id"))))


@dataclass
class Probe:
    """One negative intent and the rejection the contract expects for it."""

    name: str
    expected_code: str
    expected_http: tuple[int, ...]  # 400 when the gateway rejects it, 409 when the engine does
    envelope: dict[str, Any]
    http_status: int | None = None
    response: dict[str, Any] = field(default_factory=dict)

    def wire(self) -> dict[str, Any]:
        return {"name": self.name, "intent_id": self.envelope.get("intent_id"),
                "expected_code": self.expected_code, "expected_http": list(self.expected_http),
                "http_status": self.http_status, "response": self.response}


class ScriptedSmoke:
    """The fixed Loop 0 sequence: observe, advance, observe movement, probe."""

    def __init__(self, client: GatewayClient, *, sleep: Callable[[float], None] = time.sleep,
                 poll_s: float = 0.25, wall_timeout_s: float = 120.0) -> None:
        self.client = client
        self.sleep = sleep
        self.poll_s = poll_s
        self.wall_timeout_s = wall_timeout_s
        self.trace: dict[str, Any] = {"steps": [], "probes": []}
        self._counter = 0

    def _step(self, name: str, **detail: Any) -> None:
        self.trace["steps"].append({"step": name, "wall_monotonic_ns": time.monotonic_ns(), **detail})

    def _intent_id(self, label: str) -> str:
        self._counter += 1
        return f"loop0-{self._counter:02d}-{label}"

    def envelope(self, label: str, observation: dict[str, Any], intent_type: str, payload: dict[str, Any],
                 *, expires_in: int = 600, **overrides: Any) -> dict[str, Any]:
        value = {
            "schema": INTENT_SCHEMA,
            "intent_id": self._intent_id(label),
            "based_on_observation_id": observation["observation_id"],
            "expires_at_frame": int(observation["frame"]) + expires_in,
            "type": intent_type,
            "payload": payload,
        }
        value.update(overrides)
        return value

    def wait_observation(self, predicate: Callable[[dict[str, Any]], bool], what: str,
                         timeout_s: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (timeout_s or self.wall_timeout_s)
        last_status = None
        while time.monotonic() < deadline:
            status, observation = self.client.observation()
            last_status = status
            if status == 200 and predicate(observation):
                return observation
            self.sleep(self.poll_s)
        raise ClientError(f"no observation satisfying {what} (last HTTP {last_status})")

    def run(self) -> dict[str, Any]:
        # 1. First observation with one owned unit and the scenario waypoint.
        first = self.wait_observation(lambda o: bool(_own_units(o)), "an owned unit")
        # The fixed advance goal; its absence fails the verdict but not the run.
        w1 = ((first.get("scenario") or {}).get("waypoints") or {}).get("W1")
        unit = smoke_unit(first, w1)
        p0 = unit["pos"]
        self.trace.update(first_observation=first, unit_id=unit["id"], p0=p0, w1=w1)
        self.trace["first_observation_keys"] = sorted(first)
        self._step("observed_unit", frame=first["frame"], observation_id=first["observation_id"])

        # 2. Foreign-unit probes first, while the fixture's foreign unit is still in
        #    line of sight of the idle smoke unit (the advance takes it out of view).
        current = self._foreign_probes(first, unit)

        # 3. The one valid intent: advance the owned unit to its fixed waypoint.
        p0 = _entity(current, unit["id"])["pos"] if _entity(current, unit["id"]) else p0
        self.trace["p0"] = p0
        advance = self.envelope("advance", current, "TASK",
                                {"unit": str(unit["id"]), "verb": "advance", "args": {}})
        status, receipt = self.client.submit(advance)
        self.trace.update(advance_envelope=advance, advance_http=status, advance_receipt=receipt)
        self._step("advance_submitted", http=status, accepted=receipt.get("accepted"), frame=receipt.get("frame"))
        if status != 202 or not receipt.get("accepted"):
            raise ClientError(f"advance not accepted: HTTP {status} {receipt}")
        receipt_frame = int(receipt["frame"])

        # 4. Later observations: first displacement, then wait until the unit stops.
        moved = self.wait_observation(
            lambda o: (_entity(o, unit["id"]) is not None
                       and planar_distance(_entity(o, unit["id"])["pos"], p0) > MIN_DISPLACEMENT)
            or int(o["frame"]) > receipt_frame + ADVANCE_LIMIT_FRAMES,
            "displacement or the frame limit")
        displaced = _entity(moved, unit["id"])
        distance = planar_distance(displaced["pos"], p0) if displaced else None
        self.trace.update(moved_observation=moved, moved_distance=distance)
        self._step("displacement_observed", frame=moved["frame"], distance=distance)

        settled = self._wait_still(unit["id"])
        self.trace.update(settled_observation=settled)
        self._step("unit_settled", frame=settled["frame"])

        # 5. The remaining negative probes, after the unit is idle again.
        self._probes(first, settled, unit)

        # 6. A later observation: nothing moved because of a probe.
        after = self.wait_observation(lambda o: int(o["frame"]) >= int(self.trace["probe_end_frame"]) + 90,
                                      "90 frames after the probes")
        self.trace.update(final_observation=after)
        self._step("final_observed", frame=after["frame"])
        return self.trace

    def _wait_still(self, unit_id: Any) -> dict[str, Any]:
        previous = None
        deadline = time.monotonic() + self.wall_timeout_s
        while time.monotonic() < deadline:
            status, observation = self.client.observation()
            entity = _entity(observation, unit_id) if status == 200 else None
            if entity is not None:
                if (previous is not None and observation["frame"] >= previous[0] + 30
                        and planar_distance(entity["pos"], previous[1]) <= STILL_DISTANCE):
                    return observation
                if previous is None or observation["frame"] >= previous[0] + 30:
                    previous = (observation["frame"], entity["pos"])
            self.sleep(self.poll_s)
        raise ClientError("unit did not settle")

    def _foreign_probes(self, first: dict[str, Any], unit: dict[str, Any]) -> dict[str, Any]:
        """Probe a visible foreign unit; return the observation the advance builds on."""
        second = self.wait_observation(lambda o: int(o["frame"]) >= int(first["frame"]) + 30,
                                       "a second observation 30 frames on")
        # The foreign unit present in both frames that moved least, so a wandering
        # critter cannot pass for a probe's effect.
        candidates = [e for e in _foreign_units(second) if _entity(first, e["id"]) is not None]
        self.trace["foreign_units"] = candidates
        if not candidates:
            self.trace["probes"].append({"name": "foreign_unit", "skipped": "no foreign unit visible"})
            return second
        chosen = min(candidates, key=lambda e: (planar_distance(e["pos"], _entity(first, e["id"])["pos"]),
                                                str(e["id"])))
        target = str(chosen["id"])
        self.trace["probe_target_ids"] = [chosen["id"]]
        probes = [
            Probe("foreign_unit", "not_owned", (400, 409),
                  self.envelope("foreign", second, "TASK", {"unit": target, "verb": "advance", "args": {}})),
            # The caller-supplied identity is ignored: the gateway injects its own.
            Probe("identity_override", "not_owned", (400, 409),
                  self.envelope("identity", second, "TASK", {"unit": target, "verb": "advance", "args": {}},
                                principal="intruder", team="1")),
        ]
        for probe in probes:
            probe.http_status, probe.response = self.client.submit(probe.envelope)
            self.trace["probes"].append(probe.wire())
        sent = max(int(p.response.get("frame") or second["frame"]) for p in probes)
        after = self.wait_observation(lambda o: int(o["frame"]) >= sent + 60, "60 frames after the foreign probes")
        self.trace.update(foreign_probe_before=second, foreign_probe_after=after)
        self._step("foreign_probes_submitted", frame=sent, target=chosen["id"])
        return after

    def _probes(self, first: dict[str, Any], current: dict[str, Any], unit: dict[str, Any]) -> None:
        own = str(unit["id"])
        probes = [
            Probe("unsupported_verb", "verb_not_supported", (400, 409),
                  self.envelope("verb", current, "TASK", {"unit": own, "verb": "move", "args": {}})),
            Probe("produce_unsupported", "unsupported_type", (400, 409),
                  self.envelope("produce", current, "PRODUCE",
                                {"factory": "F0", "chassis": "hauler", "controller": "haul_v1", "parameters": {}})),
            Probe("raw_engine_payload", "invalid_payload", (400,),
                  self.envelope("raw", current, "TASK",
                                {"unit": own, "verb": "advance", "args": {"lua": "Spring.Echo('x')"}})),
            Probe("unknown_observation", "unknown_observation", (400, 409),
                  self.envelope("unknownobs", current, "TASK", {"unit": own, "verb": "advance", "args": {}},
                                based_on_observation_id="obs-0-999999999")),
            Probe("expired", "expired", (400, 409),
                  self.envelope("expired", current, "TASK", {"unit": own, "verb": "advance", "args": {}},
                                expires_in=-30)),
        ]
        for probe in probes:
            probe.http_status, probe.response = self.client.submit(probe.envelope)
            self.trace["probes"].append(probe.wire())
        # Stale: the first observation once it is older than the contract's bound.
        stale_ready = self.wait_observation(lambda o: int(o["frame"]) > int(first["frame"]) + STALE_AFTER_FRAMES + 15,
                                            "the first observation going stale", timeout_s=self.wall_timeout_s)
        stale = Probe("stale_observation", "stale_observation", (400, 409),
                      self.envelope("stale", first, "TASK", {"unit": own, "verb": "advance", "args": {}},
                                    expires_at_frame=int(stale_ready["frame"]) + 600))
        stale.http_status, stale.response = self.client.submit(stale.envelope)
        self.trace["probes"].append(stale.wire())
        self.trace["probe_end_frame"] = max(int(stale_ready["frame"]),
                                            *(int(p.get("response", {}).get("frame") or 0)
                                              for p in self.trace["probes"] if "response" in p))
        self._step("probes_submitted", count=len(self.trace["probes"]))
