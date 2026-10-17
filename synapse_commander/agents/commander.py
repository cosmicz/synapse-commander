"""Agent commander: a live model chooses each intent from gateway observations.

The loop is observe -> prompt -> one model call out of process -> parse and
validate exactly one decision -> submit through the loopback gateway -> observe
again.  The engine clock keeps running while the model thinks.  Nothing here
runs inside the simulation, and no credential is read or written: the model
route is an already-authenticated command line tool.

Each call's raw output goes to private evidence through ``on_decision``; the
ledger receives a compact INFERENCE record (hashes, latency, parsed decision)
so the run can be classified from the ledger alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import subprocess
import time
from typing import Any, Callable, Protocol

from ..protocol import INTENT_SCHEMA_VERSION, IntentEnvelope, ProtocolError
from .scripted import GatewayClient, planar_distance


ARRIVAL_RADIUS = 32.0
DEFAULT_CALL_BUDGET = 20
DEFAULT_CALL_TIMEOUT_S = 60.0
EXPIRY_AHEAD_FRAMES = 600
MAX_EXPIRY_AHEAD_FRAMES = 900

SYSTEM_PROMPT = """You are the commander of team 0 in a real-time strategy game.
The game keeps running while you think. You see one bounded observation of
your team's world and choose exactly one action. Reply with a single JSON
object and nothing else, in one of these forms:
  {"action": "intent", "envelope": <IntentEnvelope>}
  {"action": "wait", "reason": "<short reason>"}
  {"action": "done", "reason": "<short reason>"}
An IntentEnvelope is {"schema": "IntentEnvelope/v1", "intent_id": "<new unique id>",
"based_on_observation_id": "<the observation_id you were shown>",
"expires_at_frame": <an integer frame after the current frame, at most 900 later>,
"type": <one of the permitted intent types>, "payload": <that type's payload>}.
A TASK payload is {"unit": "<id of a unit you own, as a string>", "verb": "<permitted verb>", "args": {}}.
Only intents listed in permitted_intents can succeed. The game decides whether
an intent is legal and reports receipts; an accepted intent is not yet an
effect, so check later observations for what actually happened."""


class ModelError(RuntimeError):
    pass


@dataclass
class ModelReply:
    text: str
    model_ids: list[str]
    latency_ms: int
    raw: str
    error: str | None = None


class ModelRoute(Protocol):
    name: str
    def call(self, prompt: str) -> ModelReply: ...


class ClaudeCliRoute:
    """``claude -p`` with tools disabled, no session persistence, bounded time."""

    name = "claude-cli"

    def __init__(self, model: str, *, timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
                 executable: str = "claude", runner: Callable[..., Any] = subprocess.run) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self.executable = executable
        self.runner = runner

    def argv(self, prompt: str) -> list[str]:
        return [self.executable, "-p", prompt, "--output-format", "json", "--model", self.model,
                "--system-prompt", SYSTEM_PROMPT, "--tools", "", "--no-session-persistence"]

    def call(self, prompt: str) -> ModelReply:
        begin = time.monotonic()
        try:
            done = self.runner(self.argv(prompt), stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            return ModelReply("", [], _ms_since(begin), "", error=f"timeout after {self.timeout_s}s")
        except OSError as exc:
            return ModelReply("", [], _ms_since(begin), "", error=f"route unavailable: {exc.strerror}")
        latency = _ms_since(begin)
        raw = done.stdout or ""
        if done.returncode != 0:
            return ModelReply("", [], latency, raw, error=f"exit {done.returncode}")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            return ModelReply("", [], latency, raw, error="cli output is not JSON")
        if not isinstance(result, dict) or result.get("is_error"):
            return ModelReply("", [], latency, raw, error="cli reported an error")
        usage = result.get("modelUsage")
        model_ids = sorted(usage) if isinstance(usage, dict) else []
        text = result.get("result")
        return ModelReply(text if isinstance(text, str) else "", model_ids, latency, raw)


def _ms_since(begin: float) -> int:
    return int((time.monotonic() - begin) * 1000)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compact_observation(observation: dict[str, Any]) -> dict[str, Any]:
    """The prompt's view: the bounded public observation, minus empty noise."""
    keep = ("observation_id", "frame", "resources", "entities", "scenario", "permitted_intents",
            "recent_receipts", "recent_effects", "truncated", "omitted")
    return {key: observation[key] for key in keep if key in observation and observation[key] not in ([], {})}


def build_prompt(observation: dict[str, Any], history: list[dict[str, Any]]) -> str:
    """Observation plus a short record of this commander's own past decisions and receipts."""
    lines = ["Mission: get one of your units to waypoint W1.",
             "Current observation:", json.dumps(compact_observation(observation), sort_keys=True)]
    if history:
        lines += ["Your previous decisions and their receipts (oldest first):",
                  json.dumps(history[-6:], sort_keys=True)]
    lines.append("Reply with exactly one JSON object.")
    return "\n".join(lines)


_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


@dataclass
class Decision:
    action: str
    envelope: dict[str, Any] | None = None
    reason: str | None = None
    rejected: str | None = None


def parse_decision(text: str, observation: dict[str, Any], used_ids: set[str]) -> Decision:
    """Exactly one decision; any violation is a client-side rejection, never a guess."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").removeprefix("json").strip()
    match = _JSON_OBJECT.search(stripped)
    if match is None or match.group(0) != stripped:
        return Decision("invalid", rejected="not exactly one JSON object")
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return Decision("invalid", rejected="malformed JSON")
    if not isinstance(value, dict) or value.get("action") not in ("intent", "wait", "done"):
        return Decision("invalid", rejected="unknown action")
    if value["action"] != "intent":
        reason = value.get("reason")
        return Decision(value["action"], reason=reason if isinstance(reason, str) else None)
    envelope = value.get("envelope")
    if not isinstance(envelope, dict):
        return Decision("invalid", rejected="intent without envelope")
    try:
        parsed = IntentEnvelope.from_untrusted(envelope, principal="client-check", team="client-check")
    except ProtocolError as exc:
        return Decision("invalid", rejected=f"protocol: {exc.code}")
    if envelope.get("schema") != INTENT_SCHEMA_VERSION:
        return Decision("invalid", rejected="schema")
    if parsed.based_on_observation_id != observation["observation_id"]:
        return Decision("invalid", rejected="based_on_observation_id is not the observation shown")
    frame = int(observation["frame"])
    if not frame < parsed.expires_at_frame <= frame + MAX_EXPIRY_AHEAD_FRAMES:
        return Decision("invalid", rejected="expires_at_frame out of range")
    if parsed.intent_id in used_ids:
        return Decision("invalid", rejected="intent_id reused")
    permitted = {p.get("type") for p in observation.get("permitted_intents", []) if isinstance(p, dict)}
    if parsed.type not in permitted:
        return Decision("invalid", rejected="intent type not permitted in this observation")
    # Identity is the gateway's to inject; the model's copy is not forwarded.
    clean = {key: envelope[key] for key in ("schema", "intent_id", "based_on_observation_id",
                                            "expires_at_frame", "type", "payload")}
    return Decision("intent", envelope=clean)


def decision_label(envelope: dict[str, Any]) -> str:
    payload = envelope.get("payload", {})
    if envelope.get("type") == "TASK" and payload.get("verb") == "advance":
        return "advance_to_W1"
    return "other"


class InferenceSink(Protocol):
    def record_inference(self, observation_id: str, payload: dict[str, Any],
                         intent: dict[str, Any] | None, label: str | None) -> str | None: ...


def _own_at_w1(observation: dict[str, Any]) -> dict[str, Any] | None:
    w1 = ((observation.get("scenario") or {}).get("waypoints") or {}).get("W1")
    if w1 is None:
        return None
    for entity in observation.get("entities", []):
        if entity.get("source") == "own_unit" and planar_distance(entity["pos"], w1) <= ARRIVAL_RADIUS:
            return entity
    return None


@dataclass
class AgentCommander:
    client: GatewayClient
    route: ModelRoute
    model_id: str
    sink: InferenceSink | None = None
    on_decision: Callable[[dict[str, Any]], None] | None = None
    call_budget: int = DEFAULT_CALL_BUDGET
    wall_deadline_s: float = 600.0
    min_frames_between_calls: int = 30
    poll_s: float = 0.25
    sleep: Callable[[float], None] = time.sleep
    trace: dict[str, Any] = field(default_factory=lambda: {"decisions": [], "observations": []})

    def _observe(self, after_frame: int = -1) -> dict[str, Any]:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status, observation = self.client.observation()
            if status == 200 and int(observation.get("frame", -1)) > after_frame:
                self.trace["observations"].append(
                    {"frame": observation["frame"], "observation_id": observation["observation_id"],
                     "own": {str(e["id"]): e["pos"] for e in observation.get("entities", [])
                             if e.get("source") == "own_unit"}})
                return observation
            self.sleep(self.poll_s)
        raise ModelError("no fresh observation from the gateway within 60 s")

    def run(self) -> dict[str, Any]:
        begin = time.monotonic()
        history: list[dict[str, Any]] = []
        used_ids: set[str] = set()
        calls = invalid = 0
        observation = self._observe()
        self.trace["first_observation"] = observation
        stop = None
        while stop is None:
            arrived = _own_at_w1(observation)
            if arrived is not None and self.trace.get("accepted_advance"):
                stop = "unit_at_W1"
                break
            if calls >= self.call_budget:
                stop = "call_budget_exhausted"
                break
            if time.monotonic() - begin > self.wall_deadline_s:
                stop = "wall_deadline"
                break
            prompt = build_prompt(observation, history)
            reply = self.route.call(prompt)
            calls += 1
            decision = (parse_decision(reply.text, observation, used_ids) if reply.error is None
                        else Decision("invalid", rejected=f"route: {reply.error}"))
            record: dict[str, Any] = {
                "call": calls, "route": self.route.name, "observation_id": observation["observation_id"],
                "observation_frame": observation["frame"], "latency_ms": reply.latency_ms,
                "model_ids": reply.model_ids, "prompt_sha256": sha256_text(prompt),
                "output_sha256": sha256_text(reply.raw), "action": decision.action,
                "rejected": decision.rejected, "reason": decision.reason,
            }
            live = reply.error is None and self.model_id in reply.model_ids
            inference_payload = {"inference_id": f"inference-{calls:03d}", "live": live,
                                 "model_id": self.model_id if live else ",".join(reply.model_ids) or None,
                                 **{k: record[k] for k in ("call", "route", "latency_ms", "prompt_sha256",
                                                           "output_sha256", "action", "rejected")}}
            label = None
            if decision.action == "intent":
                envelope = decision.envelope
                label = decision_label(envelope)
                inference_payload["intent_id"] = envelope["intent_id"]
                inference_payload["decision"] = label
            if self.sink is not None:
                record["inference_event"] = self.sink.record_inference(
                    observation["observation_id"], inference_payload,
                    decision.envelope if decision.action == "intent" else None, label)
            if self.on_decision is not None:
                self.on_decision({**record, "prompt": prompt, "raw_output": reply.raw,
                                  "parsed": decision.envelope})
            if decision.action == "invalid":
                invalid += 1
                history.append({"call": calls, "rejected_by_client": decision.rejected})
            elif decision.action == "done":
                stop = "model_done"
            elif decision.action == "intent":
                used_ids.add(decision.envelope["intent_id"])
                status, receipt = self.client.submit(decision.envelope)
                record.update(http=status, receipt=receipt, intent=decision.envelope)
                history.append({"call": calls, "intent": decision.envelope, "http": status, "receipt": receipt})
                if status == 202 and receipt.get("accepted") and label == "advance_to_W1":
                    self.trace.setdefault("accepted_advance", {"envelope": decision.envelope,
                                                               "receipt": receipt, "observation": observation})
            else:
                history.append({"call": calls, "waited": decision.reason})
            self.trace["decisions"].append(record)
            if stop is None:
                observation = self._observe(after_frame=int(observation["frame"]) + self.min_frames_between_calls - 1)
        self.trace.update(stop=stop, calls=calls, invalid_outputs=invalid,
                          wall_s=round(time.monotonic() - begin, 3), final_observation=observation)
        return self.trace


def displacement_after_receipt(trace: dict[str, Any], limit_frames: int = 300,
                               min_distance: float = 50.0) -> dict[str, Any]:
    """First observation after the accepted advance's receipt in which its unit moved."""
    accepted = trace.get("accepted_advance")
    if not accepted:
        return {"ok": False, "detail": "no accepted advance"}
    unit = str(accepted["envelope"]["payload"]["unit"])
    start = next((e["pos"] for e in accepted["observation"].get("entities", []) if str(e.get("id")) == unit), None)
    receipt_frame = int(accepted["receipt"]["frame"])
    for seen in trace["observations"]:
        pos = seen["own"].get(unit)
        if start is None or pos is None or seen["frame"] <= receipt_frame:
            continue
        distance = planar_distance(pos, start)
        if distance > min_distance:
            return {"ok": seen["frame"] - receipt_frame <= limit_frames, "unit": unit, "distance": distance,
                    "receipt_frame": receipt_frame, "observation_frame": seen["frame"],
                    "observation_id": seen["observation_id"]}
    return {"ok": False, "unit": unit, "receipt_frame": receipt_frame, "detail": "no displacement observed"}


__all__ = ["AgentCommander", "ClaudeCliRoute", "ModelReply", "parse_decision", "build_prompt",
           "displacement_after_receipt", "SYSTEM_PROMPT"]
