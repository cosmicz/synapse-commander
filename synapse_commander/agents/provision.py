"""Scripted Loop 1 client: provision one unit through the factory, then use it.

HTTP only, like the Loop 0 reference client. Every choice is read from the
public observation (declared ids, budgets, the produced unit's controller);
every intent was written before the run, so the run is a scripted fixture.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .scripted import ADVANCE_LIMIT_FRAMES, MIN_DISPLACEMENT, ClientError, GatewayClient, planar_distance


INTENT_SCHEMA = "IntentEnvelope/v1"
EXPIRY_AHEAD_FRAMES = 600


def produce_permission(observation: dict[str, Any]) -> dict[str, Any] | None:
    for permitted in observation.get("permitted_intents", []):
        if isinstance(permitted, dict) and permitted.get("type") == "PRODUCE":
            return permitted
    return None


def budgets(observation: dict[str, Any]) -> dict[str, Any] | None:
    value = observation.get("budgets")
    return value if isinstance(value, dict) else None


def produced_units(observation: dict[str, Any], existing_ids: set[Any]) -> list[dict[str, Any]]:
    """Own units carrying a controller that did not exist before the PRODUCE.

    Predeployed assets and the commander can carry the same controller kind,
    so a produced unit is recognised as new, not by its controller alone.
    """
    return [e for e in observation.get("entities", [])
            if e.get("source") == "own_unit" and e.get("controller") and e.get("id") not in existing_ids]


def known_unit_ids(observation: dict[str, Any]) -> set[Any]:
    """Every unit id the team already knows: entities, the commander and roster ids."""
    ids = {e.get("id") for e in observation.get("entities", [])}
    if observation.get("commander"):
        ids.add(observation["commander"].get("unit_id"))
    ids.update(entry.get("unit_id") for entry in observation.get("roster") or [] if entry.get("unit_id") is not None)
    return ids


class ProvisionSmoke:
    """PRODUCE one unit, see it built and budgeted, refuse an unfunded second, advance it."""

    def __init__(self, client: GatewayClient, *, sleep: Callable[[float], None] = time.sleep,
                 poll_s: float = 0.25, wall_timeout_s: float = 120.0) -> None:
        self.client = client
        self.sleep = sleep
        self.poll_s = poll_s
        self.wall_timeout_s = wall_timeout_s
        self.trace: dict[str, Any] = {"steps": []}
        self._counter = 0

    def _step(self, name: str, **detail: Any) -> None:
        self.trace["steps"].append({"step": name, "wall_monotonic_ns": time.monotonic_ns(), **detail})

    def envelope(self, label: str, observation: dict[str, Any], intent_type: str,
                 payload: dict[str, Any]) -> dict[str, Any]:
        self._counter += 1
        return {"schema": INTENT_SCHEMA, "intent_id": f"loop1-{self._counter:02d}-{label}",
                "based_on_observation_id": observation["observation_id"],
                "expires_at_frame": int(observation["frame"]) + EXPIRY_AHEAD_FRAMES,
                "type": intent_type, "payload": payload}

    def wait_observation(self, predicate: Callable[[dict[str, Any]], bool], what: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.wall_timeout_s
        last = None
        while time.monotonic() < deadline:
            status, observation = self.client.observation()
            last = status
            if status == 200 and predicate(observation):
                return observation
            self.sleep(self.poll_s)
        raise ClientError(f"no observation satisfying {what} (last HTTP {last})")

    def run(self) -> dict[str, Any]:
        # 1. The scenario is live: commander bound, budgets and PRODUCE visible.
        first = self.wait_observation(
            lambda o: bool(o.get("commander")) and budgets(o) is not None and produce_permission(o) is not None,
            "a bound commander, budgets and a PRODUCE permission")
        permission = produce_permission(first)
        before = budgets(first)
        commander_id = first["commander"]["unit_id"]
        funded = sorted(c for c in permission["controllers"] if before["chips"].get(c, 0) > 0)
        if before["bodies_remaining"] < 1 or not funded:
            raise ClientError(f"nothing to produce with: {before}")
        choice = {"factory": permission["factories"][0], "chassis": sorted(permission["chassis"])[0],
                  "controller": funded[0], "parameters": {}}
        self.trace.update(first_observation=first, budgets_before=before, commander_id=commander_id,
                          produce_choice=choice)
        self._step("scenario_observed", frame=first["frame"], budgets=before)

        existing = known_unit_ids(first)
        self.trace["units_before_produce"] = sorted(existing, key=str)

        # 2. One funded PRODUCE.
        produce = self.envelope("produce", first, "PRODUCE", choice)
        status, receipt = self.client.submit(produce)
        self.trace.update(produce_envelope=produce, produce_http=status, produce_receipt=receipt)
        self._step("produce_submitted", http=status, accepted=receipt.get("accepted"), frame=receipt.get("frame"))
        if status != 202 or not receipt.get("accepted"):
            raise ClientError(f"PRODUCE not accepted: HTTP {status} {receipt}")

        # 3. The spend shows in the budgets, then the unit appears with its controller.
        spent = self.wait_observation(
            lambda o: int(o["frame"]) > int(receipt["frame"]) and budgets(o) is not None, "budgets after the receipt")
        self.trace.update(budgets_after=budgets(spent), budgets_after_frame=spent["frame"])
        built = self.wait_observation(
            lambda o: any(u.get("controller") == choice["controller"] for u in produced_units(o, existing)),
            "the produced unit carrying its controller")
        unit = next(u for u in produced_units(built, existing) if u.get("controller") == choice["controller"])
        self.trace.update(built_observation=built, produced_unit=unit)
        self._step("unit_observed", frame=built["frame"], unit_id=unit["id"])

        # 4. A second PRODUCE of the same controller: its chip is now spent.
        unfunded = self.envelope("unfunded", built, "PRODUCE", choice)
        status, rejection = self.client.submit(unfunded)
        self.trace.update(unfunded_envelope=unfunded, unfunded_http=status, unfunded_response=rejection)
        self._step("unfunded_submitted", http=status, reason=rejection.get("reason"))
        unchanged = self.wait_observation(
            lambda o: int(o["frame"]) >= int(rejection.get("frame") or built["frame"]) + 30, "30 frames later")
        self.trace.update(budgets_after_unfunded=budgets(unchanged), jobs_after_unfunded=unchanged.get("factories"))

        # 5. The produced unit is commanded like the smoke unit.
        start = next((u["pos"] for u in unchanged.get("entities", []) if u.get("id") == unit["id"]), unit["pos"])
        advance = self.envelope("advance", unchanged, "TASK",
                                {"unit": str(unit["id"]), "verb": "advance", "args": {}})
        status, adv = self.client.submit(advance)
        self.trace.update(advance_envelope=advance, advance_http=status, advance_receipt=adv, advance_start=start)
        self._step("advance_submitted", http=status, accepted=adv.get("accepted"), frame=adv.get("frame"))
        if status != 202 or not adv.get("accepted"):
            raise ClientError(f"advance not accepted: HTTP {status} {adv}")
        moved = self.wait_observation(
            lambda o: any(e.get("id") == unit["id"] and planar_distance(e["pos"], start) > MIN_DISPLACEMENT
                          for e in o.get("entities", []))
            or int(o["frame"]) > int(adv["frame"]) + ADVANCE_LIMIT_FRAMES,
            "displacement or the frame limit")
        pos = next((e["pos"] for e in moved.get("entities", []) if e.get("id") == unit["id"]), None)
        self.trace.update(moved_observation=moved,
                          moved_distance=None if pos is None else planar_distance(pos, start))
        self._step("displacement_observed", frame=moved["frame"], distance=self.trace["moved_distance"])
        return self.trace
