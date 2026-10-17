"""Rescue demo drivers: a scripted rehearsal and a human command prompt.

Two principals act through the gateway with their own session tokens: the
commander and the stranded mind M1. Nothing here touches the engine directly;
every intent is an IntentEnvelope/v1 the engine may refuse.

The scripted rehearsal plays the relay-then-assist rescue as far as the
engine's verbs allow: produce a relay with the free escort chip, send it to
the ridge waypoint until M1's link is restored, exchange messages, then assist
and extract when those verbs are offered (the extraction slice), otherwise it
stops with the outcome pending. Each phase is stamped with simulation and wall
time for the camera plan of the rendered capture.
"""

from __future__ import annotations

import shlex
import threading
import time
from typing import Any, Callable, TextIO

from .scripted import ClientError, GatewayClient, planar_distance


INTENT_SCHEMA = "IntentEnvelope/v1"
EXPIRY_AHEAD_FRAMES = 600
FPS = 30
RELAY_CHASSIS = "relay_support_v1"


def rescue_points(table: dict[str, Any]) -> dict[str, list[float]]:
    """Map-centre-relative [x, z] points the rehearsal needs, from the scenario table."""
    waypoints = table["waypoints"]
    zone = table["extraction_zones"][table["objective"]["extraction"]]
    ridge = waypoints.get("W2", waypoints["W1"])  # the ridge corner of the relay corridor
    return {"w1": [waypoints["W1"][0], waypoints["W1"][-1]], "ridge": [ridge[0], ridge[-1]],
            "extraction": list(zone["position"])}


def task_verbs(observation: dict[str, Any]) -> list[str]:
    for permitted in observation.get("permitted_intents", []):
        if isinstance(permitted, dict) and permitted.get("type") == "TASK":
            return list(permitted.get("verbs") or [])
    return []


def roster_entry(observation: dict[str, Any], name: str) -> dict[str, Any]:
    return next((e for e in observation.get("roster") or [] if e.get("name") == name), {})


def free_chip(observation: dict[str, Any], prefer: str = "escort_fixed_v1") -> str | None:
    chips = (observation.get("budgets") or {}).get("chips") or {}
    if chips.get(prefer, 0) > 0:
        return prefer
    funded = sorted(chip for chip, count in chips.items() if count > 0)
    return funded[0] if funded else None


def render_observation(observation: dict[str, Any]) -> str:
    """A compact text view of one principal's bounded observation."""
    lines = [f"[{observation.get('principal', '?')}] frame {observation.get('frame')} "
             f"(t={int(observation.get('frame', 0)) / FPS:.1f}s) {observation.get('observation_id')}"]
    body = observation.get("body") or {}
    if body:
        pos = body.get("pos") or [0, 0, 0]
        lines.append(f"  body {body.get('id')} {body.get('def')} at ({pos[0]:.0f}, {pos[2]:.0f})")
    for entity in observation.get("entities") or []:
        if entity.get("id") == body.get("id"):
            continue
        pos = entity.get("pos") or [0, 0, 0]
        lines.append(f"  sees {entity.get('id')} {entity.get('def')} [{entity.get('source')}] at ({pos[0]:.0f}, {pos[2]:.0f})")
    budgets = observation.get("budgets")
    if budgets:
        chips = ", ".join(f"{k}={v}" for k, v in sorted((budgets.get("chips") or {}).items()))
        lines.append(f"  budgets: bodies={budgets.get('bodies_remaining')} chips: {chips}")
    for entry in observation.get("roster") or []:
        lines.append(f"  roster {entry.get('name')} {entry.get('def')} {entry.get('status')}"
                     + (f" id={entry['unit_id']}" if entry.get("unit_id") is not None else ""))
    for link in observation.get("link_state") or []:
        lines.append(f"  link {link.get('unit_id')}: {'linked' if link.get('linked') else 'no link'}")
    for report in (observation.get("reports") or [])[-6:]:
        what = report.get("text") or report.get("symptom") or report.get("kind")
        lines.append(f"  report {report.get('kind')} from {report.get('from') or report.get('asset')}: {what}"
                     + (f" (age {report['age_frames']}f)" if report.get("age_frames") is not None else ""))
    waypoints = (observation.get("scenario") or {}).get("waypoints") or {}
    if waypoints:
        lines.append("  waypoints: " + ", ".join(f"{k}=({v[0]:.0f}, {v[2]:.0f})" for k, v in sorted(waypoints.items())))
    lines.append("  permitted: " + "; ".join(
        f"{p.get('type')}" + (f" {','.join(p.get('verbs') or [])}" if p.get("verbs") else "")
        for p in observation.get("permitted_intents") or []))
    return "\n".join(lines)


class PrincipalSession:
    """One principal's gateway client plus envelope numbering."""

    def __init__(self, principal: str, client: GatewayClient, label: str) -> None:
        self.principal = principal
        self.client = client
        self.label = label
        self.counter = 0
        self.latest: dict[str, Any] | None = None

    def observe(self) -> dict[str, Any] | None:
        status, observation = self.client.observation()
        if status == 200 and observation.get("principal") == self.principal:
            self.latest = observation
            return observation
        return None

    def envelope(self, kind: str, intent_type: str, payload: dict[str, Any],
                 observation: dict[str, Any] | None = None) -> dict[str, Any]:
        observation = observation or self.latest
        if observation is None:
            raise ClientError(f"{self.principal} has no observation yet")
        self.counter += 1
        return {"schema": INTENT_SCHEMA, "intent_id": f"{self.label}-{self.principal}-{self.counter:03d}-{kind}",
                "principal": self.principal, "based_on_observation_id": observation["observation_id"],
                "expires_at_frame": int(observation["frame"]) + EXPIRY_AHEAD_FRAMES,
                "type": intent_type, "payload": payload}

    def submit(self, envelope: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return self.client.submit(envelope)


class M1Policy:
    """Scripted local policy for the stranded mind: report status once it can be heard."""

    def __init__(self, session: PrincipalSession, *, poll_s: float = 0.25,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.session = session
        self.poll_s = poll_s
        self.sleep = sleep
        self.sent: list[dict[str, Any]] = []
        self.stop = threading.Event()

    def step(self) -> None:
        observation = self.session.observe()
        if observation is None or self.sent:
            return
        envelope = self.session.envelope("status", "MESSAGE",
                                         {"to": "commander", "text": "M1: alive, mobility degraded, holding position"})
        status, receipt = self.session.submit(envelope)
        self.sent.append({"envelope": envelope, "http": status, "receipt": receipt, "frame": observation["frame"]})

    def run_in_background(self) -> threading.Thread:
        def loop() -> None:
            while not self.stop.wait(self.poll_s):
                try:
                    self.step()
                except Exception:  # the policy is best effort; the ledger shows what happened
                    pass
        thread = threading.Thread(target=loop, name="m1-policy", daemon=True)
        thread.start()
        return thread


class RescueRehearsal:
    """Scripted relay-then-assist rescue with a timed phase plan for the camera."""

    def __init__(self, commander: GatewayClient, m1: GatewayClient, *, points: dict[str, Any],
                 poll_s: float = 0.25, wall_timeout_s: float = 240.0, link_timeout_frames: int = 2400,
                 outcome_timeout_s: float = 420.0, sleep: Callable[[float], None] = time.sleep) -> None:
        self.points = points
        self.outcome_timeout_s = outcome_timeout_s
        self.commander = PrincipalSession("commander", commander, "rescue")
        self.m1 = PrincipalSession("M1", m1, "rescue")
        self.poll_s = poll_s
        self.wall_timeout_s = wall_timeout_s
        self.link_timeout_frames = link_timeout_frames
        self.sleep = sleep
        self.began = time.monotonic()
        self.trace: dict[str, Any] = {"phases": [], "intents": []}

    def phase(self, name: str, observation: dict[str, Any] | None, camera: str, **detail: Any) -> None:
        frame = None if observation is None else int(observation["frame"])
        self.trace["phases"].append({
            "phase": name, "frame": frame, "sim_s": None if frame is None else round(frame / FPS, 2),
            "wall_s": round(time.monotonic() - self.began, 2), "camera": camera, **detail})

    def wait(self, session: PrincipalSession, predicate: Callable[[dict[str, Any]], bool], what: str,
             timeout_s: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (timeout_s or self.wall_timeout_s)
        while time.monotonic() < deadline:
            observation = session.observe()
            if observation is not None and predicate(observation):
                return observation
            self.sleep(self.poll_s)
        raise ClientError(f"timed out waiting for {what}")

    def submit(self, session: PrincipalSession, kind: str, intent_type: str, payload: dict[str, Any],
               observation: dict[str, Any]) -> dict[str, Any]:
        envelope = session.envelope(kind, intent_type, payload, observation)
        status, receipt = session.submit(envelope)
        record = {"principal": session.principal, "kind": kind, "envelope": envelope, "http": status,
                  "receipt": receipt, "frame": observation["frame"]}
        self.trace["intents"].append(record)
        return record

    def run(self) -> dict[str, Any]:
        start = self.wait(self.commander, lambda o: bool(o.get("roster")) and o.get("budgets") is not None,
                          "the commander's first view")
        self.wait(self.m1, lambda o: bool(o.get("body")), "M1's first view")
        m1_status = roster_entry(start, "M1").get("status")
        self.trace.update(start_observation=start, m1_status_at_start=m1_status)
        self.phase("situation", start, "wide on base, ridge and valley; distress report on M1", m1=m1_status)
        ridge = absolute(start, self.points["ridge"], self.points["w1"])
        extraction = absolute(start, self.points["extraction"], self.points["w1"])
        self.trace.update(ridge_point=ridge, extraction_point=extraction)

        # Relay: one relay body with the free chip, from the commander's factory.
        chip = free_chip(start)
        if chip is None:
            raise ClientError("no funded chip for a relay")
        factory = (start.get("factories") or [{}])[0].get("name", "F0")
        known = {link.get("unit_id") for link in start.get("link_state") or []}
        known |= {e.get("id") for e in start.get("entities") or []}
        produce = self.submit(self.commander, "produce-relay", "PRODUCE",
                              {"factory": factory, "chassis": RELAY_CHASSIS, "controller": chip, "parameters": {}}, start)
        if produce["http"] != 202 or not produce["receipt"].get("accepted"):
            raise ClientError(f"relay PRODUCE refused: {produce['http']} {produce['receipt']}")
        self.phase("produce_relay", start, "close on factory F0", chassis=RELAY_CHASSIS, chip=chip)

        built = self.wait(self.commander,
                          lambda o: any(link.get("unit_id") not in known for link in o.get("link_state") or []),
                          "the relay body to appear")
        relay_id = next(link["unit_id"] for link in built["link_state"] if link.get("unit_id") not in known)
        self.trace["relay_unit"] = relay_id
        self.phase("relay_built", built, "close on the new relay at the factory", unit=relay_id)

        # Send it to the ridge corner; M1's link is restored once it is there.
        verbs = task_verbs(built)
        self.trace["task_verbs"] = verbs
        if "move_to" in verbs:
            payload = {"unit": str(relay_id), "verb": "move_to", "args": {"x": int(ridge[0]), "z": int(ridge[1])}}
        else:  # before the extraction slice: the fixed-waypoint verb, labelled in the phase plan
            payload = {"unit": str(relay_id), "verb": "advance", "args": {}}
        move = self.submit(self.commander, "relay-to-ridge", "TASK", payload, built)
        if move["http"] != 202 or not move["receipt"].get("accepted"):
            raise ClientError(f"relay move refused: {move['http']} {move['receipt']}")
        self.phase("relay_moving", built, "follow the relay over the base toward the ridge corner", unit=relay_id,
                   verb=payload["verb"])
        limit = int(built["frame"]) + self.link_timeout_frames
        linked = self.wait(self.commander,
                           lambda o: roster_entry(o, "M1").get("status") == "in_contact" or int(o["frame"]) > limit,
                           "M1's link to be restored")
        restored = roster_entry(linked, "M1").get("status") == "in_contact"
        self.trace.update(link_restored=restored)
        self.phase("link_restored" if restored else "link_not_restored", linked,
                   "split: relay on the ridge corner, M1 in the valley")
        if not restored:
            raise ClientError("M1's link was not restored within the frame limit")

        # The commander tells M1 where to go; M1 answers and goes there itself.
        order = self.submit(self.commander, "extraction-point", "MESSAGE",
                            {"to": "M1", "text": f"extract at {int(extraction[0])} {int(extraction[1])}"}, linked)
        heard = self.wait(self.m1, lambda o: any(r.get("kind") == "message" and r.get("from") == "commander"
                                                  for r in o.get("reports") or []),
                          "the commander's message to reach M1", timeout_s=60)
        self.phase("message_delivered", heard, "message overlay: commander to M1, extraction point",
                   http=order["http"])
        target = parse_extraction(heard) or extraction
        m1_verbs = task_verbs(heard)
        if "move_to" not in m1_verbs:
            self.trace["outcome"] = "pending"
            self.phase("extraction_pending", heard,
                       "hold on M1; caption: move_to and extraction outcomes not yet in the engine",
                       missing_verbs=[v for v in ("move_to",) if v not in m1_verbs])
            return self.trace
        body = heard.get("body") or {}
        self.submit(self.m1, "ack", "MESSAGE", {"to": "commander", "text": "M1: heading to extraction"}, heard)
        go = self.submit(self.m1, "to-extraction", "TASK",
                         {"unit": str(body.get("id")), "verb": "move_to",
                          "args": {"x": int(target[0]), "z": int(target[1])}}, heard)
        if go["http"] != 202 or not go["receipt"].get("accepted"):
            raise ClientError(f"M1 move_to refused: {go['http']} {go['receipt']}")
        self.phase("m1_moving", heard, "follow M1 at damaged speed toward the extraction zone")
        done = self.wait(self.commander,
                         lambda o: (o.get("objective") or {}).get("status") not in (None, "pending"),
                         "the mission outcome", timeout_s=self.outcome_timeout_s)
        self.trace["outcome"] = (done.get("objective") or {}).get("status")
        self.phase("outcome", done, "wide: outcome card", outcome=self.trace["outcome"])
        return self.trace


def absolute(observation: dict[str, Any], offset: list[float], w1_offset: list[float]) -> tuple[float, float]:
    """Map-centre-relative table point to world x, z, anchored on the observed W1."""
    w1 = ((observation.get("scenario") or {}).get("waypoints") or {}).get("W1")
    if not w1:
        raise ClientError("observation has no W1 to anchor table points")
    centre_x, centre_z = w1[0] - w1_offset[0], w1[2] - w1_offset[-1]
    return centre_x + offset[0], centre_z + offset[-1]


def parse_extraction(observation: dict[str, Any]) -> tuple[float, float] | None:
    for report in reversed(observation.get("reports") or []):
        words = str(report.get("text") or "").split()
        if report.get("from") == "commander" and len(words) >= 4 and words[:2] == ["extract", "at"]:
            try:
                return float(words[2]), float(words[3])
            except ValueError:
                return None
    return None


HELP = """commands:
  (empty) | obs                 refresh and show the commander's view
  produce CHASSIS [CONTROLLER]  build at the factory (controller defaults to a funded chip)
  advance UNIT                  order a body to the ridge waypoint W1
  move_to UNIT X Z              move a body (only when the engine offers move_to)
  message TO TEXT...            send a message to another principal (e.g. M1)
  help | quit"""


class RescuePlay:
    """A person commands the commander principal at a prompt; M1 runs a scripted policy."""

    def __init__(self, commander: GatewayClient, m1: GatewayClient, *, stdin: TextIO, stdout: TextIO,
                 poll_s: float = 0.25, sleep: Callable[[float], None] = time.sleep) -> None:
        self.commander = PrincipalSession("commander", commander, "play")
        self.m1_policy = M1Policy(PrincipalSession("M1", m1, "play"), poll_s=poll_s, sleep=sleep)
        self.stdin = stdin
        self.stdout = stdout
        self.sleep = sleep
        self.trace: dict[str, Any] = {"inputs": [], "intents": []}

    def say(self, text: str) -> None:
        self.stdout.write(text + "\n")
        self.stdout.flush()

    def refresh(self) -> dict[str, Any] | None:
        for _ in range(40):
            observation = self.commander.observe()
            if observation is not None:
                return observation
            self.sleep(0.25)
        return None

    def parse(self, line: str, observation: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | str:
        """Turn one input line into (kind, intent type, payload) or an error message."""
        try:
            words = shlex.split(line)
        except ValueError as exc:
            return f"cannot parse: {exc}"
        command, args = words[0].lower(), words[1:]
        if command == "produce" and 1 <= len(args) <= 2:
            controller = args[1] if len(args) == 2 else free_chip(observation)
            factory = (observation.get("factories") or [{}])[0].get("name", "F0")
            return "produce", "PRODUCE", {"factory": factory, "chassis": args[0], "controller": controller,
                                          "parameters": {}}
        if command == "advance" and len(args) == 1:
            return "advance", "TASK", {"unit": args[0], "verb": "advance", "args": {}}
        if command == "move_to" and len(args) == 3:
            try:
                x, z = float(args[1]), float(args[2])
            except ValueError:
                return "move_to needs numeric X and Z"
            return "move_to", "TASK", {"unit": args[0], "verb": "move_to", "args": {"x": x, "z": z}}
        if command == "message" and len(args) >= 2:
            return "message", "MESSAGE", {"to": args[0], "text": " ".join(args[1:])[:256]}
        return f"unknown command: {line!r}; type help"

    def run(self) -> dict[str, Any]:
        self.m1_policy.run_in_background()
        self.say("Synapse Commander rescue: you are the commander. " + HELP.splitlines()[0])
        observation = self.refresh()
        if observation is not None:
            self.say(render_observation(observation))
        try:
            for raw in self.stdin:
                line = raw.strip()
                self.trace["inputs"].append({"line": line, "wall_monotonic_ns": time.monotonic_ns()})
                if line in ("quit", "exit"):
                    break
                if line in ("help", "?"):
                    self.say(HELP)
                    continue
                observation = self.refresh() or observation
                if line in ("", "obs"):
                    if observation is not None:
                        self.say(render_observation(observation))
                    continue
                if observation is None:
                    self.say("no observation yet; try again")
                    continue
                parsed = self.parse(line, observation)
                if isinstance(parsed, str):
                    self.say(parsed)
                    continue
                kind, intent_type, payload = parsed
                envelope = self.commander.envelope(kind, intent_type, payload, observation)
                status, receipt = self.commander.submit(envelope)
                self.trace["intents"].append({"input": line, "envelope": envelope, "http": status, "receipt": receipt})
                verdict = "accepted" if receipt.get("accepted") else f"refused: {receipt.get('reason') or receipt.get('error')}"
                self.say(f"  -> {intent_type} {verdict} (HTTP {status})")
        finally:
            self.m1_policy.stop.set()
        self.trace["m1_messages"] = self.m1_policy.sent
        return self.trace
