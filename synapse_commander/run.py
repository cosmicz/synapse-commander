"""Loop 0 orchestration: engine, mailbox transport, gateway, recorder, client.

One run: write the manifest, start the headless engine, serve the loopback
gateway, drive it with the scripted reference client over HTTP only, ingest
the engine's outbox into the ledger, stop the engine, verify the ledger and
write a verdict.  The verdict is computed from the verified ledger and the
client's HTTP transcript, never from an acceptance alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as _dt
import hashlib
import json
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Callable, Protocol

from . import launch
from .agents.scripted import (
    ADVANCE_LIMIT_FRAMES,
    MIN_DISPLACEMENT,
    STILL_DISTANCE,
    GatewayClient,
    ScriptedSmoke,
    planar_distance,
)
from .agents.provision import ProvisionSmoke
from .agents.world import WorldObserver
from .agents.view import ViewSmoke
from .agents.commander import SYSTEM_PROMPT, AgentCommander, ClaudeCliRoute, displacement_after_receipt
from .gateway import LoopbackGateway
from .scenarios import ScenarioError, load_scenario
from .recording import EFFECT, INTENT_RECEIPT, Recorder, verify_ledger


SCENARIO = {"id": "loop0-advance-smoke", "version": "0", "seed": 1}
ADAPTER_VERSION = "synapse-mailbox/0+gateway/v1"
PRINCIPAL = "scripted-reference"
TEAM = "0"
FPS = 30
ORDER_DONE_LIMIT_FRAMES = 600
AGENT_PRINCIPAL = "agent-commander"
AGENT_LABEL = ("agent commander: headless; every intent chosen by a live model from gateway observations; "
               "autonomy only as the verified ledger classifies it; not visual")
CLASSIFICATION_LABEL = "scripted fixture: headless, scripted reference client, not autonomous, not visual"


def run_label(agent: bool, presentation: str) -> str:
    """How the run is described; a rendered client changes presentation, not decisions."""
    label = AGENT_LABEL if agent else CLASSIFICATION_LABEL
    if presentation == "rendered":
        label = label.replace("headless", "rendered client").replace(
            "not visual", "rendered frames show presentation only")
    return label


class SmokeTransport(Protocol):
    def hello(self) -> dict[str, Any] | None: ...
    def current_frame(self) -> int: ...
    def observation_for(self, team: str, principal: str | None = None) -> Any: ...
    def submit_intent(self, envelope: Any) -> Any: ...
    def events_since(self, seq: int, limit: int = 256) -> list[dict[str, Any]]: ...
    def close(self) -> None: ...


class SmokeEngine(Protocol):
    teardown: dict[str, object]
    def start(self) -> None: ...
    def poll(self) -> int | None: ...
    def stop(self) -> dict[str, object]: ...


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    return "loop0-" + _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)


def mint_principal_tokens(run_dir: Path, principals: list[str]) -> dict[str, str]:
    """Create per-run session credentials only in the private evidence tree."""
    if not principals or len(set(principals)) != len(principals):
        raise ValueError("principals must be unique and non-empty")
    if any(not isinstance(principal, str) or not principal for principal in principals):
        raise ValueError("principal names must be non-empty strings")
    tokens = {principal: secrets.token_urlsafe(32) for principal in principals}
    path = run_dir / "principals.json"
    path.write_text(json.dumps(tokens, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return tokens


def gateway_sessions(principal_tokens: dict[str, str]) -> dict[str, str]:
    """Invert a private principal-token file for the in-memory gateway only."""
    if not principal_tokens or len(set(principal_tokens.values())) != len(principal_tokens):
        raise ValueError("principal tokens must be unique and non-empty")
    if any(not isinstance(principal, str) or not principal or not isinstance(token, str) or not token
           for principal, token in principal_tokens.items()):
        raise ValueError("principal tokens must be non-empty strings")
    return {token: principal for principal, token in principal_tokens.items()}


def app_identity() -> tuple[str, int]:
    """HEAD of the repository holding this package, and its modified-file count."""
    root = Path(__file__).resolve().parent.parent
    head = launch.git_head(root)
    if head is None:
        raise launch.LaunchError("application commit unavailable; run from a git checkout")
    return head, launch.git_dirty_count(root) or 0


class OrderedLedger:
    """Serializes gateway records and engine outbox ingestion into one ledger.

    The recorder requires a non-decreasing sim_frame.  Engine events are fed in
    outbox order; before a gateway record at frame F, every buffered engine
    event at a frame <= F is written first.  An effect that names an intent is
    held until that intent's accepted receipt exists, so it can cite it.
    A gateway record whose frame is older than the ledger's last frame is
    written at the ledger's frame (the frame of ingestion); the payload keeps
    the engine's own frame.  Engine observation and receipt outbox records are
    not duplicated: the gateway records the ones it serves.
    """

    def __init__(self, recorder: Recorder, transport: SmokeTransport) -> None:
        self.recorder = recorder
        self.transport = transport
        self._lock = threading.RLock()
        self._last_seq = 0
        self._buffer: list[dict[str, Any]] = []
        self.last_frame = 0
        self.clamped: list[dict[str, Any]] = []
        self.accepted_receipts: dict[str, str] = {}
        self._unit_effect: dict[Any, str] = {}
        self._scenario: str | None = None
        self._clock: str | None = None
        self.hello: dict[str, Any] | None = None
        self.bye: dict[str, Any] | None = None
        self.bridge_errors: list[dict[str, Any]] = []
        self.uncited_effects: list[dict[str, Any]] = []
        self.engine_receipts: dict[str, dict[str, Any]] = {}
        self.observation_events: dict[str, str] = {}
        self._inference_links: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self.inference_link_mismatches: list[str] = []
        self.done_intents: set[str] = set()

    def record_inference(self, observation_id: str, payload: dict[str, Any],
                         intent: dict[str, Any] | None, label: str | None) -> str | None:
        """The agent's own INFERENCE record, citing the observation it was shown.

        When the decision is an intent, the gateway's later INTENT_SUBMITTED for
        the same intent_id cites this record and carries its decision label,
        provided the submitted envelope is the one the model produced.
        """
        with self._lock:
            observation_event = self.observation_events.get(observation_id)
            if observation_event is None:
                return None
            self._fetch()
            event_id = self._write("INFERENCE", sim_frame=max(self.transport.current_frame(), self.last_frame),
                                   origin="agent", payload=payload, causes=[observation_event])
            if intent is not None and label is not None:
                self._inference_links[str(intent["intent_id"])] = (event_id, label, intent)
            return event_id

    # LedgerSink, used by the gateway
    def record(self, kind: str, *, sim_frame: int, origin: str, payload: dict[str, Any],
               causes: tuple[str, ...] | list[str] = ()) -> str:
        with self._lock:
            self._fetch()
            self._drain(upto=sim_frame)
            if kind == "INTENT_SUBMITTED":
                payload, causes = self._link_inference(payload, list(causes))
            return self._write(kind, sim_frame=sim_frame, origin=origin, payload=payload, causes=causes)

    def _link_inference(self, payload: dict[str, Any], causes: list[str]) -> tuple[dict[str, Any], list[str]]:
        intent_id = str(payload.get("intent_id"))
        link = self._inference_links.pop(intent_id, None)
        if link is None:
            return payload, causes
        event_id, label, produced = link
        submitted = payload.get("envelope", {})
        fields = ("intent_id", "based_on_observation_id", "expires_at_frame", "type", "payload")
        if any(submitted.get(key) != produced.get(key) for key in fields):
            self.inference_link_mismatches.append(intent_id)
            return payload, causes
        return {**payload, "decision": label}, [*causes, event_id]

    def pump(self, *, final: bool = False) -> None:
        with self._lock:
            self._fetch()
            self._drain(upto=None, final=final)

    def _write(self, kind: str, *, sim_frame: int, origin: str, payload: dict[str, Any],
               causes: tuple[str, ...] | list[str]) -> str:
        frame = sim_frame
        if frame < self.last_frame:
            # Ledger time is the frame of ingestion; keep the source frame visible.
            self.clamped.append({"kind": kind, "requested": sim_frame, "written": self.last_frame})
            payload = {**payload, "source_frame": sim_frame}
            frame = self.last_frame
        event_id = self.recorder.record(kind, sim_frame=frame, origin=origin, payload=payload, causes=list(causes))
        self.last_frame = frame
        if kind == "OBSERVATION" and isinstance(payload.get("observation_id"), str):
            self.observation_events[payload["observation_id"]] = event_id
        if kind == INTENT_RECEIPT and payload.get("status") == "accepted":
            self.accepted_receipts[str(payload.get("intent_id"))] = event_id
        return event_id

    def _fetch(self) -> None:
        while True:
            events = self.transport.events_since(self._last_seq, 256)
            if not events:
                return
            for event in events:
                self._last_seq = max(self._last_seq, int(event.get("seq", 0)))
                self._buffer.append(event)
            if len(events) < 256:
                return

    def _drain(self, *, upto: int | None, final: bool = False) -> None:
        while self._buffer:
            event = self._buffer[0]
            frame = int(event.get("frame", 0))
            if upto is not None and frame > upto:
                return
            if not self._ingest(event, final=final):
                return  # an effect waits for its intent's receipt
            self._buffer.pop(0)

    def _ingest(self, event: dict[str, Any], *, final: bool) -> bool:
        kind = event.get("kind")
        body = event.get("body") if isinstance(event.get("body"), dict) else {}
        frame = int(event.get("frame", 0))
        meta = {"engine_seq": event.get("seq"), "engine_kind": kind, "engine_ms": event.get("engine_ms"),
                "engine_frame": frame}
        if kind == "hello":
            self.hello = body
            self._clock = self._write("CLOCK_MODE", sim_frame=frame, origin="engine",
                                      payload={"mode": body.get("clock_mode", "continuous"), "declared": True,
                                               "protocol": body.get("protocol"), **meta}, causes=())
        elif kind == "scenario":
            self._scenario = self._write("SCENARIO_EVENT", sim_frame=frame, origin="scenario",
                                         payload={**body, "name": str(body.get("name", "unnamed")), **meta},
                                         causes=())
        elif kind == "effect":
            intent_id = body.get("intent_id")
            unit = body.get("unit_id")
            cause = None
            if intent_id is not None:
                cause = self.accepted_receipts.get(str(intent_id))
                if cause is None and not final:
                    return False
            if cause is None:
                cause = self._unit_effect.get(unit) or self._scenario
            if cause is None:
                self.uncited_effects.append(event)
                return True
            event_id = self._write(EFFECT, sim_frame=frame, origin="engine",
                                   payload={**body, "type": str(body.get("effect")), **meta}, causes=[cause])
            self._unit_effect[unit] = event_id
            if body.get("effect") == "order_done" and intent_id is not None:
                self.done_intents.add(str(intent_id))
        elif kind == "receipt":
            self.engine_receipts[str(body.get("intent_id"))] = {"frame": frame, **body}
        elif kind == "bye":
            self.bye = body
        elif kind == "bridge_error":
            self.bridge_errors.append(event)
        # observation: served through the gateway, which records what it serves
        return True


@dataclass
class SmokeConfig:
    engine_dir: Path
    data_dir: Path
    out_dir: Path
    game_dir: Path | None = None
    lock_dir: Path | None = None
    rig_commit: str | None = None
    run_id: str = field(default_factory=new_run_id)
    allow_dirty: bool = False
    hello_timeout_s: float = 120.0
    client_timeout_s: float = 120.0
    client_poll_s: float = 0.25
    lock_wait_s: float = 1200.0
    mode: str = "scripted"            # scripted, agent, provision, world, or embodied two-client view
    engine_binary: str = launch.HEADLESS_BINARY  # or "spring", the rendered client
    scenario: str | None = None       # a ScenarioTable/v1 name in the archive; None is Loop 0
    model: str = "claude-sonnet-5"    # agent mode: model pinned for the CLI route
    call_budget: int = 20
    call_timeout_s: float = 60.0
    agent_deadline_s: float = 600.0


def build_manifest(*, run_id: str, rig_commit: str | None, app_commit: str, identity: dict[str, Any],
                   config_hashes: dict[str, str], principal: str = PRINCIPAL, model_id: str | None = None,
                   presentation: str = "headless", scenario: dict[str, Any] | None = None,
                   external_budgets: dict[str, Any] | None = None,
                   required_decisions: list[dict[str, Any]] | None = None,
                   principals: list[str] | None = None,
                   principal_sources: dict[str, str] | None = None) -> dict[str, Any]:
    declared_principals = [principal] if principals is None else list(principals)
    if not declared_principals or len(set(declared_principals)) != len(declared_principals):
        raise ValueError("manifest principals must be unique and non-empty")
    manifest = {
        "schema": "synapse.manifest/v1",
        "run_id": run_id,
        "rig_commit": rig_commit,
        "app_commit": app_commit,
        "engine": identity["engine"],
        "game": identity["game"],
        "map": identity["map"],
        "scenario": dict(scenario or SCENARIO),
        "controller_images": {},
        "adapter_version": ADAPTER_VERSION,
        "principals": declared_principals,
        "config_hashes": config_hashes,
        "model_id": model_id,
        "external_budgets": external_budgets or {},
        "clock_mode": "continuous",
        "fps": FPS,
        "presentation": presentation,
        "required_decisions": required_decisions or [],
    }
    if principal_sources is not None:
        if set(principal_sources) != set(declared_principals) or any(source not in {"scripted", "agent"} for source in principal_sources.values()):
            raise ValueError("principal_sources must name every declared principal")
        manifest["principal_sources"] = dict(principal_sources)
    return manifest


def scenario_seed(table: dict[str, Any] | None) -> int:
    """The table's declared world seed, else the Loop 0 smoke seed."""
    seed = None if table is None else table.get("seed")
    if isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0:
        return seed
    return SCENARIO["seed"]


def scenario_identity(table: dict[str, Any] | None) -> dict[str, Any] | None:
    """Manifest scenario for a selected table; None keeps the Loop 0 smoke identity."""
    if table is None:
        return None
    return {"id": table["id"], "version": table["version"], "seed": scenario_seed(table)}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_smoke(config: SmokeConfig, *,
              transport_factory: Callable[[Path, str], SmokeTransport] | None = None,
              engine_factory: Callable[[launch.RunLayout], SmokeEngine] | None = None,
              identity: dict[str, Any] | None = None,
              app: tuple[str, int] | None = None,
              engine_scan: Callable[[int | None], list[int]] = launch.other_engines,
              model_route: Any = None) -> dict[str, Any]:
    """Run one Loop 0 smoke and return its verdict; writes evidence under out_dir/run_id.

    mode "scripted" drives the run with the scripted reference client; mode
    "agent" with the agent commander, whose decisions come from a live model.
    """
    run_id = launch.check_run_id(config.run_id)
    if config.mode not in ("scripted", "agent", "provision", "world", "view"):
        raise launch.LaunchError(f"unknown mode {config.mode!r}")
    agent = config.mode == "agent"
    provision = config.mode == "provision"
    world = config.mode == "world"
    view = config.mode == "view"
    if (provision or world or view) and config.scenario is None:
        raise launch.LaunchError(f"{config.mode} mode needs a scenario table (--scenario)")
    if not config.rig_commit:
        raise launch.LaunchError("rig commit is required for the manifest")
    app_commit, app_dirty = app or app_identity()
    if app_dirty and not config.allow_dirty:
        raise launch.LaunchError(f"application checkout has {app_dirty} modified files; commit first")
    layout = launch.RunLayout(config.out_dir / run_id)
    artifacts = None
    if engine_factory is None:
        artifacts = launch.RuntimeArtifacts.from_dirs(engine_dir=config.engine_dir, data_dir=config.data_dir,
                                                      game_dir=config.game_dir, engine_binary=config.engine_binary)
        layout.create(artifacts)
        identity = identity or artifacts.identity()
    else:
        layout.write_dir.mkdir(parents=True)
    table = None
    if config.scenario is not None:
        try:
            table = load_scenario(config.scenario)  # fail closed before anything starts
        except ScenarioError as exc:
            raise launch.LaunchError(f"scenario {config.scenario!r}: {exc}") from exc
    view_principals: list[str] | None = None
    if view:
        raw_principals = None if table is None else table.get("principals")
        if not isinstance(raw_principals, dict) or set(raw_principals) != {"commander", "M1"}:
            raise launch.LaunchError("view mode needs a table declaring commander and M1 principals")
        view_principals = ["commander", "M1"]
    seed = scenario_seed(table)
    script = launch.start_script(run_id=run_id, host_port=launch.free_udp_port(), scenario=config.scenario,
                                 seed=seed)
    settings = launch.engine_settings()
    layout.write_config(script, settings)
    mailbox_dir = layout.mailbox_dir
    mailbox_dir.mkdir(parents=True, exist_ok=True)

    presentation = "headless" if artifacts is None else artifacts.presentation
    render_env = launch.render_environment()
    config_hashes = {"start_script": sha256_text(script),
                     "springsettings": sha256_text(launch._settings_text(settings)),
                     # The values may hold host paths, so the manifest carries only their hash;
                     # the private verdict keeps them in full.
                     "engine_render_environment": sha256_text(json.dumps(render_env, sort_keys=True))}
    if artifacts is not None:
        config_hashes.update(artifacts.archive_hashes())
    if table is not None:
        config_hashes[f"scenario_table/{config.scenario}"] = sha256_text(json.dumps(table, sort_keys=True))
    package = Path(__file__).resolve().parent
    for module in ("mailbox.py", "gateway.py", "protocol.py", "recording.py", "run.py", "launch.py",
                   "agents/scripted.py", "agents/commander.py", "agents/provision.py", "agents/world.py",
                   "agents/view.py"):
        path = package / module
        if path.exists():
            config_hashes[f"synapse_commander/{module}"] = launch.sha256_file(path)
    identity = identity or {"engine": {"name": "none", "version": "none"},
                            "game": {"name": "none", "version": "none"},
                            "map": {"name": "none", "version": "none"}}
    if agent:
        config_hashes["agent_system_prompt"] = sha256_text(SYSTEM_PROMPT)
        manifest = build_manifest(
            run_id=run_id, rig_commit=config.rig_commit, app_commit=app_commit, identity=identity,
            config_hashes=config_hashes, principal=AGENT_PRINCIPAL, model_id=config.model, presentation=presentation,
            scenario=scenario_identity(table),
            external_budgets={"model_calls": config.call_budget, "per_call_timeout_s": config.call_timeout_s,
                              "wall_deadline_s": config.agent_deadline_s,
                              "route": getattr(model_route, "name", "claude-cli")},
            required_decisions=[{"label": "advance_to_W1", "type": "TASK"}])
    elif view:
        manifest = build_manifest(
            run_id=run_id, rig_commit=config.rig_commit, app_commit=app_commit, identity=identity,
            config_hashes=config_hashes, presentation=presentation, scenario=scenario_identity(table),
            principals=view_principals, principal_sources={name: "scripted" for name in view_principals},
        )
    else:
        manifest = build_manifest(run_id=run_id, rig_commit=config.rig_commit, app_commit=app_commit,
                                  identity=identity, config_hashes=config_hashes, presentation=presentation,
                                  scenario=scenario_identity(table))
    (mailbox_dir / "run.json").write_text(json.dumps(
        {"run_id": run_id, "game": launch.APP_GAME_TYPE, "map": launch.MAP_NAME,
         "start_script_sha256": config_hashes["start_script"], "app_commit": app_commit}, indent=1) + "\n")

    lock = None
    if config.lock_dir is not None:
        lock = launch.RuntimeLock(config.lock_dir, f"{launch.lock_seat()} {run_id} {utc_now()}",
                                  wait_s=config.lock_wait_s)
    verdict: dict[str, Any] = {"run_id": run_id, "mode": config.mode,
                               **({"call_budget": config.call_budget, "model": config.model} if agent else {}),
                               "classification": run_label(agent, presentation),
                               "presentation": presentation, "engine_binary": config.engine_binary,
                               "scenario": None if table is None else {"id": table["id"], "seed": seed,
                                                                       "table_declares_seed": "seed" in table},
                               "engine_render_environment": render_env,
                               "app_commit": app_commit, "app_modified_files": app_dirty,
                               "rig_commit": config.rig_commit, "wall_start_utc": utc_now(),
                               "lock": None if lock is None else {"name": config.lock_dir.name}}
    ledger_path = layout.run_dir / "ledger.jsonl"
    engine: SmokeEngine | None = None
    transport: SmokeTransport | None = None
    server = None
    ordered: OrderedLedger | None = None
    recorder: Recorder | None = None
    trace: dict[str, Any] = {}
    error: str | None = None
    pump_stop = threading.Event()
    try:
        if lock is not None:
            lock.acquire()
            verdict["lock"].update(waited_s=lock.waited_s, owner=lock.owner_line,
                                   held_by="enclosing with-vmlock.sh" if lock.inherited else "launcher")
        recorder = Recorder.create(ledger_path, manifest)
        recorder.run_start(0)
        principal_tokens = mint_principal_tokens(layout.run_dir, view_principals) if view_principals else None
        transport = (transport_factory or _mailbox_factory)(mailbox_dir, run_id)
        ordered = OrderedLedger(recorder, transport)
        engine = (engine_factory or (lambda lay: launch.EngineProcess(artifacts, lay)))(layout)
        verdict["other_engines_at_start"] = engine_scan(None)
        engine.start()
        t0 = time.monotonic()
        while transport.hello() is None:
            if engine.poll() is not None:
                raise launch.LaunchError(f"engine exited with {engine.poll()} before hello")
            if time.monotonic() - t0 > config.hello_timeout_s:
                raise launch.LaunchError(f"no engine hello within {config.hello_timeout_s}s")
            time.sleep(0.25)
        verdict["hello_wall_s"] = round(time.monotonic() - t0, 3)
        ordered.pump()

        gateway = LoopbackGateway(transport, principal=AGENT_PRINCIPAL if agent else ("commander" if view else PRINCIPAL),
                                  team=TEAM, origin="agent" if agent else "scripted", ledger=ordered,
                                  scenario=config.scenario,
                                  sessions=None if principal_tokens is None else gateway_sessions(principal_tokens))
        server = gateway.make_server(0)
        threading.Thread(target=server.serve_forever, name="gateway", daemon=True).start()

        def pump_loop() -> None:
            while not pump_stop.wait(0.2):
                others = engine_scan(getattr(engine, "pid", None))
                if others:
                    seen = verdict.setdefault("other_engines_during_run", [])
                    seen.extend(pid for pid in others if pid not in seen)
                    if engine_scan is launch.other_engines:
                        # Private evidence only (the verdict, not the ledger): who it was.
                        verdict.setdefault("other_engine_processes", {}).update(
                            {str(pid): info for pid, info in launch.engine_processes(
                                getattr(engine, "pid", None)).items()})
                if engine.poll() is not None:
                    # Fail fast: without an engine the client could only time out.
                    verdict["engine_exited_early"] = engine.poll()
                    server.shutdown()
                    return
                try:
                    ordered.pump()
                except Exception as exc:  # recorded in the verdict, stops the pump
                    verdict.setdefault("pump_errors", []).append(repr(exc))
                    return
        threading.Thread(target=pump_loop, name="ledger-pump", daemon=True).start()

        client = GatewayClient(f"http://127.0.0.1:{server.server_port}",
                               token=None if principal_tokens is None else principal_tokens["commander"])
        if agent:
            decisions_path = layout.run_dir / "decisions.jsonl"

            def keep_decision(record: dict[str, Any]) -> None:
                # Raw prompts and model output stay in private evidence, never the ledger.
                with decisions_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")

            route = model_route or ClaudeCliRoute(config.model, timeout_s=config.call_timeout_s)
            driver: Any = AgentCommander(client, route, config.model, sink=ordered, on_decision=keep_decision,
                                         call_budget=config.call_budget, wall_deadline_s=config.agent_deadline_s,
                                         poll_s=config.client_poll_s)
        elif world:
            driver = WorldObserver(client, wall_timeout_s=config.client_timeout_s, poll_s=config.client_poll_s)
        elif view:
            driver = ViewSmoke(client, GatewayClient(f"http://127.0.0.1:{server.server_port}",
                                                      token=principal_tokens["M1"]),
                               wall_timeout_s=config.client_timeout_s, poll_s=config.client_poll_s)
        elif provision:
            driver = ProvisionSmoke(client, wall_timeout_s=config.client_timeout_s, poll_s=config.client_poll_s)
        else:
            driver = ScriptedSmoke(client, wall_timeout_s=config.client_timeout_s, poll_s=config.client_poll_s)
        try:
            trace = driver.run()
        except Exception as exc:  # keep the partial transcript as evidence
            error = f"client: {type(exc).__name__}: {exc}"
            trace = driver.trace
        final_advance = (trace.get("accepted_advance", {}).get("envelope") if agent
                         else trace.get("advance_envelope") if (provision or view) and trace.get("advance_http") == 202 else None)
        if final_advance:
            # The client stops once the unit is moving or at W1; give the
            # engine's order_done a bounded moment to arrive in the ledger.
            intent_id = str(final_advance["intent_id"])
            deadline = time.monotonic() + 20
            while intent_id not in ordered.done_intents and time.monotonic() < deadline:
                time.sleep(0.25)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        pump_stop.set()
        if server is not None:
            server.shutdown()
            server.server_close()
        if transport is not None:
            try:
                transport.close()  # asks the bridge to say bye
                time.sleep(1.0)
            except Exception as exc:
                verdict.setdefault("teardown_errors", []).append(repr(exc))
        if engine is not None:
            verdict["teardown"] = engine.stop()
        if ordered is not None and recorder is not None:
            try:
                ordered.pump(final=True)
            except Exception as exc:
                verdict.setdefault("pump_errors", []).append(repr(exc))
        if recorder is not None:
            checks = _evaluator(config.mode)(trace, ordered, verdict)
            outcome = {"pre_ledger_checks": "PASS" if checks and all(c["ok"] for c in checks.values())
                       and not error else "FAIL", "failed": sorted(n for n, c in checks.items() if not c["ok"]),
                       "error": error, "classification": run_label(agent, presentation)}
            try:
                recorder.run_end(ordered.last_frame if ordered else 0, outcome)
            except Exception as exc:
                verdict.setdefault("ledger_errors", []).append(repr(exc))
                recorder.close()
        if lock is not None and lock.held:
            lock.release()
            verdict["lock"]["released"] = not config.lock_dir.exists() or (
                (config.lock_dir / "owner").exists() and lock.owner_line not in
                (config.lock_dir / "owner").read_text())
    verdict["wall_end_utc"] = utc_now()
    verdict["error"] = error
    _finish(verdict, trace, ordered, ledger_path, layout, mode=config.mode)
    return verdict


def _mailbox_factory(mailbox_dir: Path, run_id: str) -> SmokeTransport:
    from .mailbox import MailboxTransport
    return MailboxTransport(mailbox_dir, run_id=run_id, team=TEAM)


def _ledger_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def evaluate(trace: dict[str, Any], ordered: OrderedLedger | None, verdict: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Checks known before RUN_END; the ledger verification is added afterwards."""
    checks: dict[str, dict[str, Any]] = {}

    def check(name: str, ok: bool, **detail: Any) -> None:
        checks[name] = {"ok": bool(ok), **detail}

    check("hello_seen", ordered is not None and ordered.hello is not None,
          hello=None if ordered is None else ordered.hello)
    first = trace.get("first_observation")
    check("unit_and_waypoint_observed", first is not None and trace.get("w1") is not None,
          unit_id=trace.get("unit_id"), p0=trace.get("p0"), w1=trace.get("w1"))
    receipt = trace.get("advance_receipt") or {}
    check("advance_accepted", trace.get("advance_http") == 202 and receipt.get("accepted") is True,
          http=trace.get("advance_http"), receipt=receipt)
    moved = trace.get("moved_observation")
    distance = trace.get("moved_distance")
    receipt_frame = receipt.get("frame")
    within = (moved is not None and distance is not None and receipt_frame is not None
              and distance > MIN_DISPLACEMENT and int(moved["frame"]) - int(receipt_frame) <= ADVANCE_LIMIT_FRAMES)
    check("displacement_observed_within_300_frames", within, distance=distance,
          receipt_frame=receipt_frame, observation_frame=None if moved is None else moved["frame"])
    check("receipt_before_displacement", within and int(receipt_frame) < int(moved["frame"]))
    probes = [p for p in trace.get("probes", []) if "skipped" not in p]
    skipped = [p for p in trace.get("probes", []) if "skipped" in p]
    for probe in probes:
        response = probe.get("response") or {}
        code = response.get("reason") or response.get("error")
        check(f"probe_{probe['name']}",
              probe.get("http_status") in probe["expected_http"] and code == probe["expected_code"]
              and response.get("accepted") is not True,
              layer="gateway" if probe.get("http_status") == 400 else "engine",
              expected=[probe["expected_http"], probe["expected_code"]], got=[probe.get("http_status"), code])
    for probe in skipped:
        check(f"probe_{probe['name']}", False, skipped=probe["skipped"])
    settled = trace.get("settled_observation")
    final = trace.get("final_observation")
    unit_id = trace.get("unit_id")
    if settled is not None and final is not None:
        before, after = _pos(settled, unit_id), _pos(final, unit_id)
        own_moved = None if before is None or after is None else planar_distance(before, after)
        check("no_movement_after_probes", own_moved is not None and own_moved <= STILL_DISTANCE,
              own_moved=own_moved, frames=[settled["frame"], final["frame"]])
    else:
        check("no_movement_after_probes", False, detail="no settled/final observation")
    # Foreign probes ran before the advance; only the probed unit and the idle
    # smoke unit count, since Gaia critters wander on their own.
    pre, post = trace.get("foreign_probe_before"), trace.get("foreign_probe_after")
    if pre is not None and post is not None:
        moves = {}
        for entity_id in [unit_id, *trace.get("probe_target_ids", [])]:
            a, b = _pos(pre, entity_id), _pos(post, entity_id)
            moves[str(entity_id)] = None if a is None or b is None else planar_distance(a, b)
        check("no_movement_after_foreign_probes", all(d is not None and d <= STILL_DISTANCE for d in moves.values()),
              moved=moves, frames=[pre["frame"], post["frame"]])
    else:
        check("no_movement_after_foreign_probes", False, detail="foreign probes did not run")
    _host_checks(verdict, check)
    return checks


def _host_checks(verdict: dict[str, Any], check: Callable[..., None]) -> None:
    others = sorted(set(verdict.get("other_engines_at_start", [])) | set(verdict.get("other_engines_during_run", [])))
    check("exclusive_runtime", not others, other_engine_pids=others,
          detail="no other engine process (spring-headless or spring) ran on this host during the run")
    teardown = verdict.get("teardown") or {}
    check("teardown_bounded", teardown.get("group_alive_after") is False
          and float(teardown.get("teardown_s", 1e9)) <= 20.0, teardown=teardown)


def evaluate_agent(trace: dict[str, Any], ordered: OrderedLedger | None,
                   verdict: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Agent-commander checks known before RUN_END; ledger checks follow in _finish."""
    checks: dict[str, dict[str, Any]] = {}

    def check(name: str, ok: bool, **detail: Any) -> None:
        checks[name] = {"ok": bool(ok), **detail}

    check("hello_seen", ordered is not None and ordered.hello is not None,
          hello=None if ordered is None else ordered.hello)
    first = trace.get("first_observation") or {}
    w1 = ((first.get("scenario") or {}).get("waypoints") or {}).get("W1")
    check("unit_and_waypoint_observed", bool(first.get("entities")) and w1 is not None, w1=w1)
    accepted = trace.get("accepted_advance")
    check("advance_accepted", accepted is not None,
          intent=None if accepted is None else accepted["envelope"],
          receipt=None if accepted is None else accepted["receipt"])
    moved = displacement_after_receipt(trace, ADVANCE_LIMIT_FRAMES, MIN_DISPLACEMENT)
    check("displacement_observed_within_300_frames", moved["ok"], **{k: v for k, v in moved.items() if k != "ok"})
    check("receipt_before_displacement", moved.get("observation_frame") is not None
          and int(moved["receipt_frame"]) < int(moved["observation_frame"]))
    decisions = trace.get("decisions", [])
    check("model_calls_within_budget", 0 < int(trace.get("calls", 0)) <= int(verdict.get("call_budget", 20)),
          calls=trace.get("calls"), budget=verdict.get("call_budget"), stop=trace.get("stop"))
    # Counted, not failed: rejecting an invalid output client-side is the point.
    check("invalid_outputs_rejected_and_counted",
          int(trace.get("invalid_outputs", 0)) == sum(1 for d in decisions if d.get("action") == "invalid"),
          invalid=trace.get("invalid_outputs"),
          reasons=[d.get("rejected") for d in decisions if d.get("action") == "invalid"])
    latencies = [d["latency_ms"] for d in decisions if isinstance(d.get("latency_ms"), int)]
    verdict["decision_latency_ms"] = {"count": len(latencies), "min": min(latencies, default=None),
                                      "max": max(latencies, default=None),
                                      "mean": round(sum(latencies) / len(latencies)) if latencies else None}
    verdict["model_ids_reported"] = sorted({m for d in decisions for m in d.get("model_ids", [])})
    if ordered is not None and ordered.inference_link_mismatches:
        check("submitted_equals_model_output", False, mismatched=ordered.inference_link_mismatches)
    _host_checks(verdict, check)
    return checks


def agent_ledger_checks(records: list[dict[str, Any]], trace: dict[str, Any],
                        verification: Any) -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    accepted = trace.get("accepted_advance")
    intent_id = None if accepted is None else str(accepted["envelope"]["intent_id"])
    by_id = {r["event_id"]: r for r in records}

    def first(kind: str, **match: Any) -> dict[str, Any] | None:
        for record in records:
            if record["kind"] == kind and all(record["payload"].get(k) == v for k, v in match.items()):
                return record
        return None

    submitted = first("INTENT_SUBMITTED", intent_id=intent_id)
    inference = None
    if submitted is not None:
        inference = next((by_id[c] for c in submitted["causes"] if by_id[c]["kind"] == "INFERENCE"), None)
    receipt = first("INTENT_RECEIPT", intent_id=intent_id, status="accepted")
    started = first("EFFECT", intent_id=intent_id, type="order_started")
    moved_id = displacement_after_receipt(trace).get("observation_id")
    moved = first("OBSERVATION", observation_id=moved_id) if moved_id else None
    chain = [inference, submitted, receipt, started, moved]
    seqs = [None if r is None else r["seq"] for r in chain]
    cites = bool(started and receipt and receipt["event_id"] in started["causes"])
    checks["ledger_causal_order"] = {
        "ok": None not in seqs and seqs == sorted(seqs) and cites,
        "seqs": dict(zip(["inference", "submitted", "receipt", "order_started", "moved_observation"], seqs)),
        "effect_cites_receipt": cites, "submission_cites_inference": inference is not None}
    done = first("EFFECT", intent_id=intent_id, type="order_done")
    receipt_frame = None if accepted is None else accepted["receipt"].get("frame")
    done_frame = None if done is None else done["payload"].get("engine_frame")
    checks["order_done_effect"] = {
        "ok": done is not None and receipt_frame is not None and done_frame is not None
        and 0 <= int(done_frame) - int(receipt_frame) <= ORDER_DONE_LIMIT_FRAMES,
        "engine_frame": done_frame, "receipt_frame": receipt_frame, "limit_frames": ORDER_DONE_LIMIT_FRAMES}
    rejected = {r["payload"].get("intent_id") for r in records
                if r["kind"] == "INTENT_RECEIPT" and r["payload"].get("status") == "rejected"}
    leaked = [r["seq"] for r in records if r["kind"] == "EFFECT" and r["payload"].get("intent_id") in rejected]
    checks["rejected_intents_no_effect"] = {"ok": not leaked, "effect_seqs": leaked,
                                            "rejected_intents": sorted(i for i in rejected if i)}
    checks["classified_agent_only"] = {
        "ok": verification.ok and verification.classification == "agent_only" and verification.autonomous is True,
        "classification": verification.classification, "autonomous": verification.autonomous,
        "evidence_limits": verification.evidence_limits}
    return checks


def _pos(observation: dict[str, Any], entity_id: Any) -> list[float] | None:
    for entity in observation.get("entities", []):
        if entity.get("id") == entity_id:
            return entity.get("pos")
    return None


def ledger_checks(records: list[dict[str, Any]], trace: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Causal-order and no-effect checks over the verified ledger."""
    checks: dict[str, dict[str, Any]] = {}
    advance = (trace.get("advance_envelope") or {}).get("intent_id")
    unit_id = trace.get("unit_id")
    by_kind_intent: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        intent = record.get("payload", {}).get("intent_id")
        if intent is not None:
            by_kind_intent.setdefault((record["kind"], str(intent)), []).append(record)
    submitted = by_kind_intent.get(("INTENT_SUBMITTED", str(advance)), [])
    receipts = by_kind_intent.get(("INTENT_RECEIPT", str(advance)), [])
    started = [r for r in by_kind_intent.get(("EFFECT", str(advance)), []) if r["payload"].get("type") == "order_started"]
    done = [r for r in records if r["kind"] == "EFFECT" and r["payload"].get("type") == "order_done"
            and r["payload"].get("unit_id") == unit_id]
    moved_id = (trace.get("moved_observation") or {}).get("observation_id")
    moved_obs = [r for r in records if r["kind"] == "OBSERVATION" and r["payload"].get("observation_id") == moved_id]
    chain = [submitted[:1], receipts[:1], started[:1], moved_obs[:1]]
    seqs = [c[0]["seq"] if c else None for c in chain]
    ordered_ok = all(s is not None for s in seqs) and seqs == sorted(seqs)
    cites = bool(started and receipts and receipts[0]["event_id"] in started[0]["causes"])
    checks["ledger_causal_order"] = {"ok": ordered_ok and cites,
                                     "seqs": dict(zip(["submitted", "receipt", "order_started", "moved_observation"], seqs)),
                                     "effect_cites_receipt": cites}
    receipt_frame = (trace.get("advance_receipt") or {}).get("frame")
    done_frame = done[0]["payload"].get("engine_frame") if done else None
    checks["order_done_effect"] = {
        "ok": bool(done) and receipt_frame is not None and done_frame is not None
        and 0 <= int(done_frame) - int(receipt_frame) <= ORDER_DONE_LIMIT_FRAMES,
        "count": len(done), "engine_frame": done_frame, "receipt_frame": receipt_frame,
        "limit_frames": ORDER_DONE_LIMIT_FRAMES}
    probe_ids = [p.get("intent_id") for p in trace.get("probes", []) if p.get("intent_id")]
    leaked = {pid: [r["seq"] for r in by_kind_intent.get(("EFFECT", str(pid)), [])] for pid in probe_ids}
    accepted = {pid: [r["seq"] for r in by_kind_intent.get(("INTENT_RECEIPT", str(pid)), [])
                      if r["payload"].get("status") == "accepted"] for pid in probe_ids}
    checks["probes_no_effect_records"] = {
        "ok": not any(leaked.values()) and not any(accepted.values()),
        "effects": {k: v for k, v in leaked.items() if v}, "accepted": {k: v for k, v in accepted.items() if v}}
    identity = [r for r in by_kind_intent.get(("INTENT_SUBMITTED", str(_probe_id(trace, "identity_override"))), [])]
    if identity:
        envelope = identity[0]["payload"].get("envelope", {})
        checks["identity_injected_by_gateway"] = {
            "ok": envelope.get("principal") == PRINCIPAL and str(envelope.get("team")) == TEAM,
            "recorded": {"principal": envelope.get("principal"), "team": envelope.get("team")}}
    return checks


def evaluate_provision(trace: dict[str, Any], ordered: OrderedLedger | None,
                       verdict: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Loop 1 checks from the HTTP transcript; ledger checks follow in _finish."""
    checks: dict[str, dict[str, Any]] = {}

    def check(name: str, ok: bool, **detail: Any) -> None:
        checks[name] = {"ok": bool(ok), **detail}

    check("hello_seen", ordered is not None and ordered.hello is not None,
          hello=None if ordered is None else ordered.hello)
    first = trace.get("first_observation") or {}
    check("scenario_observed", bool(first.get("commander")) and trace.get("budgets_before") is not None,
          commander=first.get("commander"), budgets=trace.get("budgets_before"),
          scenario=(first.get("scenario") or {}).get("id"))
    receipt = trace.get("produce_receipt") or {}
    check("produce_accepted", trace.get("produce_http") == 202 and receipt.get("accepted") is True,
          choice=trace.get("produce_choice"), receipt=receipt)
    before, after = trace.get("budgets_before") or {}, trace.get("budgets_after") or {}
    chip = (trace.get("produce_choice") or {}).get("controller")
    check("budgets_decremented",
          bool(before and after) and after.get("bodies_remaining") == before.get("bodies_remaining", 0) - 1
          and (after.get("chips") or {}).get(chip) == (before.get("chips") or {}).get(chip, 0) - 1,
          before=before, after=after, chip=chip)
    unit = trace.get("produced_unit") or {}
    check("produced_unit_observed_with_controller", unit.get("controller") == chip and unit.get("id") is not None,
          unit=unit)
    response = trace.get("unfunded_response") or {}
    reason = response.get("reason") or response.get("error")
    check("unfunded_produce_rejected",
          trace.get("unfunded_http") == 409 and reason in ("chip_unavailable", "budget_exhausted")
          and response.get("accepted") is not True,
          http=trace.get("unfunded_http"), reason=reason)
    check("budgets_unchanged_after_rejection", trace.get("budgets_after_unfunded") == after,
          after=after, after_rejection=trace.get("budgets_after_unfunded"))
    adv = trace.get("advance_receipt") or {}
    check("produced_unit_advance_accepted", trace.get("advance_http") == 202 and adv.get("accepted") is True,
          receipt=adv)
    moved = trace.get("moved_observation")
    distance = trace.get("moved_distance")
    within = (moved is not None and distance is not None and adv.get("frame") is not None
              and distance > MIN_DISPLACEMENT and int(moved["frame"]) - int(adv["frame"]) <= ADVANCE_LIMIT_FRAMES)
    check("displacement_observed_within_300_frames", within, distance=distance, receipt_frame=adv.get("frame"),
          observation_frame=None if moved is None else moved["frame"])
    check("receipt_before_displacement", within and int(adv["frame"]) < int(moved["frame"]))
    _host_checks(verdict, check)
    return checks


def provision_ledger_checks(records: list[dict[str, Any]], trace: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Factory job, budget and advance effects in causal ledger order."""
    checks: dict[str, dict[str, Any]] = {}
    produce_id = (trace.get("produce_envelope") or {}).get("intent_id")
    unfunded_id = (trace.get("unfunded_envelope") or {}).get("intent_id")
    advance_id = (trace.get("advance_envelope") or {}).get("intent_id")
    unit_id = (trace.get("produced_unit") or {}).get("id")
    chip = (trace.get("produce_choice") or {}).get("controller")

    def first(kind: str, **match: Any) -> dict[str, Any] | None:
        for record in records:
            if record["kind"] == kind and all(record["payload"].get(k) == v for k, v in match.items()):
                return record
        return None

    def effect(name: str, intent_id: Any) -> dict[str, Any] | None:
        return first("EFFECT", type=name, intent_id=intent_id)

    bound = first("EFFECT", type="commander_bound")
    checks["commander_bound_effect"] = {"ok": bound is not None,
                                        "unit_id": None if bound is None else bound["payload"].get("unit_id")}
    created = effect("unit_created", produce_id)
    detail = {} if created is None else created["payload"].get("detail") or {}
    done = effect("factory_job_done", produce_id)
    checks["unit_created_carries_controller"] = {
        "ok": created is not None and detail.get("controller") == chip and created["payload"].get("unit_id") == unit_id
        and done is not None and (done["payload"].get("detail") or {}).get("unit_id") == unit_id
        and (done["payload"].get("detail") or {}).get("job_id") == detail.get("job_id"),
        "detail": detail, "unit_id": unit_id}
    chain = [first("INTENT_SUBMITTED", intent_id=produce_id), first("INTENT_RECEIPT", intent_id=produce_id),
             effect("factory_job_started", produce_id), created, done,
             first("INTENT_SUBMITTED", intent_id=advance_id), first("INTENT_RECEIPT", intent_id=advance_id),
             effect("order_started", advance_id)]
    seqs = [None if r is None else r["seq"] for r in chain]
    names = ["produce_submitted", "produce_receipt", "factory_job_started", "unit_created", "factory_job_done",
             "advance_submitted", "advance_receipt", "order_started"]
    receipt = chain[1]
    cites = bool(receipt and chain[2] and receipt["event_id"] in chain[2]["causes"])
    checks["ledger_causal_order"] = {"ok": None not in seqs and seqs == sorted(seqs) and cites,
                                     "seqs": dict(zip(names, seqs)), "job_started_cites_receipt": cites}
    leaked = [r["seq"] for r in records if r["kind"] == "EFFECT" and r["payload"].get("intent_id") == unfunded_id]
    checks["unfunded_produce_no_effect"] = {"ok": unfunded_id is not None and not leaked, "effect_seqs": leaked}
    order_done = effect("order_done", advance_id)
    receipt_frame = (trace.get("advance_receipt") or {}).get("frame")
    done_frame = None if order_done is None else order_done["payload"].get("engine_frame")
    checks["order_done_effect"] = {
        "ok": order_done is not None and receipt_frame is not None and done_frame is not None
        and 0 <= int(done_frame) - int(receipt_frame) <= ORDER_DONE_LIMIT_FRAMES,
        "engine_frame": done_frame, "receipt_frame": receipt_frame}
    return checks


def evaluate_world(trace: dict[str, Any], ordered: OrderedLedger | None,
                   verdict: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Initial-world checks known before RUN_END; truth comparisons follow in _finish."""
    checks: dict[str, dict[str, Any]] = {}

    def check(name: str, ok: bool, **detail: Any) -> None:
        checks[name] = {"ok": bool(ok), **detail}

    check("hello_seen", ordered is not None and ordered.hello is not None,
          hello=None if ordered is None else ordered.hello)
    observations = trace.get("observations", [])
    check("world_observed", len(observations) >= 2, count=len(observations),
          frames=[o["frame"] for o in observations])
    _host_checks(verdict, check)
    return checks


def evaluate_view(trace: dict[str, Any], ordered: OrderedLedger | None,
                  verdict: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """HTTP seam checks for the two embodied scripted principal clients."""
    checks: dict[str, dict[str, Any]] = {}

    def check(name: str, ok: bool, **detail: Any) -> None:
        checks[name] = {"ok": bool(ok), **detail}

    commander, m1 = trace.get("commander_observation") or {}, trace.get("m1_observation") or {}
    check("hello_seen", ordered is not None and ordered.hello is not None,
          hello=None if ordered is None else ordered.hello)
    check("separate_principal_streams",
          commander.get("principal") == "commander" and m1.get("principal") == "M1"
          and commander.get("observation_id") != m1.get("observation_id"),
          commander=commander.get("observation_id"), m1=m1.get("observation_id"))
    advance = trace.get("advance_receipt") or {}
    message = trace.get("message_receipt") or {}
    check("m1_local_advance_accepted", trace.get("advance_http") == 202 and advance.get("accepted") is True,
          receipt=advance)
    check("m1_message_accepted", trace.get("message_http") == 202 and message.get("accepted") is True,
          receipt=message)
    foreign = trace.get("foreign_response") or {}
    check("cross_principal_observation_refused",
          trace.get("foreign_http") == 400 and foreign.get("error") == "unknown_observation",
          http=trace.get("foreign_http"), response=foreign)
    _host_checks(verdict, check)
    return checks


def view_ledger_checks(records: list[dict[str, Any]], trace: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The ledger must retain separate observation, authority and effect streams."""
    checks: dict[str, dict[str, Any]] = {}
    observations = [record for record in records if record["kind"] == "OBSERVATION"]
    streams = {record["payload"].get("principal") for record in observations}
    checks["ledger_principal_observations"] = {"ok": {"commander", "M1"} <= streams,
                                                  "principals": sorted(p for p in streams if isinstance(p, str))}
    by_intent = {record["payload"].get("intent_id"): record for record in records
                 if record["kind"] == "INTENT_RECEIPT"}
    expected = {"advance": trace.get("advance_envelope"), "message": trace.get("message_envelope")}
    for label, envelope in expected.items():
        intent_id = None if not isinstance(envelope, dict) else envelope.get("intent_id")
        receipt = by_intent.get(intent_id)
        checks[f"ledger_{label}_receipt"] = {
            "ok": receipt is not None and receipt["payload"].get("principal") == "M1"
            and receipt["payload"].get("status") == "accepted",
            "intent_id": intent_id, "receipt": None if receipt is None else receipt["payload"],
        }
    advance_id = (trace.get("advance_envelope") or {}).get("intent_id")
    message_id = (trace.get("message_envelope") or {}).get("intent_id")
    m1_effects = [record["payload"] for record in records if record["kind"] == EFFECT
                  and record["payload"].get("principal") == "M1"]
    effect_pairs = {(effect.get("type"), effect.get("intent_id")) for effect in m1_effects}
    checks["m1_effect_stream"] = {"ok": ("order_started", advance_id) in effect_pairs
                                    and ("message_sent", message_id) in effect_pairs,
                                   "effects": sorted((str(kind), str(intent)) for kind, intent in effect_pairs)}
    return checks


# Keys a player observation must never carry: evaluator-only fields.
_HIDDEN_KEYS = frozenset({"seed", "cause_hidden", "state", "contact", "assets", "inventory", "health_truth"})


def _walk(value: Any):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key, nested
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def world_ledger_checks(records: list[dict[str, Any]], trace: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Roster, accounting, distress ageing and non-disclosure against world_init."""
    checks: dict[str, dict[str, Any]] = {}
    init = next((r for r in records if r["kind"] == "SCENARIO_EVENT" and r["payload"].get("name") == "world_init"),
                None)
    served = trace.get("observations", [])
    ledger_observations = [r for r in records if r["kind"] == "OBSERVATION"]
    first_observation = ledger_observations[0] if ledger_observations else None
    checks["world_init_recorded_before_observations"] = {
        "ok": init is not None and first_observation is not None and init["seq"] < first_observation["seq"]
        and init["origin"] == "scenario",
        "world_init_seq": None if init is None else init["seq"],
        "first_observation_seq": None if first_observation is None else first_observation["seq"]}
    if init is None or not served:
        checks["world_truth_available"] = {"ok": False}
        return checks
    truth = init["payload"]
    assets = {a["name"]: a for a in truth.get("assets", [])}
    hidden_ids = {a["unit_id"] for a in assets.values() if not a.get("contact") and a.get("unit_id") is not None}
    hidden_positions = [a["position"] for a in assets.values() if not a.get("contact") and a.get("position")]

    # Roster: every asset known by name; only units in contact carry a unit id.
    roster_ok, roster_seen = True, {}
    for observation in served:
        roster = {entry["name"]: entry for entry in observation.get("roster") or []}
        roster_seen = {name: {k: v for k, v in entry.items()} for name, entry in roster.items()}
        if set(roster) != set(assets):
            roster_ok = False
        for name, asset in assets.items():
            entry = roster.get(name, {})
            if asset.get("contact") and asset.get("state") == "active":
                roster_ok &= entry.get("status") == "in_contact" and entry.get("unit_id") == asset.get("unit_id")
            else:
                roster_ok &= entry.get("status") != "in_contact" and "unit_id" not in entry
    checks["roster_contact_and_ids"] = {"ok": roster_ok, "last_roster": roster_seen}

    # Accounting: observed budgets equal the evaluator's available counts, and
    # total = deployed + lost + available for bodies and every chip.
    inventory = truth.get("inventory", {})
    expected = {"bodies_remaining": inventory.get("bodies", {}).get("available"),
                "chips": {chip: v.get("available") for chip, v in (inventory.get("chips") or {}).items()}}
    observed = served[0].get("budgets")
    balanced = bool(truth.get("inventory_consistent")) and all(
        v["total"] == v["deployed"] + v["lost"] + v["available"]
        for v in [inventory.get("bodies", {}), *(inventory.get("chips") or {}).values()] if v)
    checks["budgets_debited_for_predeployed"] = {
        "ok": balanced and all(o.get("budgets") == expected for o in served),
        "observed": observed, "expected_available": expected, "inventory": inventory}

    # Distress: one pre-start report on the declared asset whose age grows with time.
    distress = truth.get("distress") or {}
    ages, report_ok = [], True
    for observation in served:
        reports = [r for r in observation.get("reports") or [] if r.get("kind") == "distress"]
        if len(reports) != 1:
            report_ok = False
            continue
        report = reports[0]
        report_ok &= (report.get("asset") == distress.get("asset") and report.get("symptom") == distress.get("symptom")
                      and report.get("observed_frame") == distress.get("observed_frame")
                      and report.get("age_frames") == int(observation["frame"]) - int(report["observed_frame"]))
        ages.append((observation["frame"], report.get("age_frames")))
    growing = len(ages) >= 2 and all(b[1] > a[1] for a, b in zip(ages, ages[1:]))
    pre_start = bool(ages) and distress.get("observed_frame") is not None and int(distress["observed_frame"]) < 0
    checks["distress_report_aged_and_growing"] = {"ok": report_ok and growing and pre_start, "ages": ages,
                                                  "observed_frame": distress.get("observed_frame")}

    # Non-disclosure, over the HTTP responses and the ledger's observation records alike.
    leaks: list[str] = []
    cause = distress.get("cause_hidden")
    for where, observation in [*(("http", o) for o in served),
                               *(("ledger", r["payload"].get("observation", {})) for r in ledger_observations)]:
        for key, value in _walk(observation):
            if key in _HIDDEN_KEYS:
                leaks.append(f"{where}:{observation.get('observation_id')}:key {key}")
            if cause is not None and value == cause:
                leaks.append(f"{where}:{observation.get('observation_id')}:cause")
        for report in observation.get("reports") or []:
            for position in hidden_positions:
                pos = report.get("position") or []
                if len(pos) == 3 and abs(pos[0] - position[0]) < 1 and abs(pos[2] - position[-1]) < 1:
                    leaks.append(f"{where}:{observation.get('observation_id')}:true position in report")
        for entity in observation.get("entities", []):
            if entity.get("id") in hidden_ids:
                leaks.append(f"{where}:{observation.get('observation_id')}:hidden unit {entity.get('id')}")
            for position in hidden_positions:
                pos = entity.get("pos") or []
                if len(pos) == 3 and abs(pos[0] - position[0]) < 1 and abs(pos[2] - position[-1]) < 1:
                    leaks.append(f"{where}:{observation.get('observation_id')}:true position")
    checks["no_hidden_truth_in_player_output"] = {"ok": not leaks, "leaks": leaks[:20],
                                                  "hidden_unit_ids": sorted(hidden_ids)}
    return checks


def _probe_id(trace: dict[str, Any], name: str) -> str | None:
    for probe in trace.get("probes", []):
        if probe.get("name") == name:
            return probe.get("intent_id")
    return None


def _evaluator(mode: str) -> Callable[..., dict[str, dict[str, Any]]]:
    return {"agent": evaluate_agent, "provision": evaluate_provision, "world": evaluate_world,
            "view": evaluate_view}.get(mode, evaluate)


def _finish(verdict: dict[str, Any], trace: dict[str, Any], ordered: OrderedLedger | None,
            ledger_path: Path, layout: launch.RunLayout, *, mode: str = "scripted") -> None:
    agent, provision, world, view = mode == "agent", mode == "provision", mode == "world", mode == "view"
    checks = _evaluator(mode)(trace, ordered, verdict)
    verification = verify_ledger(ledger_path) if ledger_path.exists() else None
    if verification is not None:
        checks["ledger_verified"] = {"ok": verification.ok, "status": verification.status,
                                     "errors": verification.errors,
                                     "classification": verification.classification,
                                     "autonomous": verification.autonomous,
                                     "evidence_limits": verification.evidence_limits,
                                     "counts": verification.counts}
        records = verification.records if verification.ok else _ledger_records(ledger_path)
        if agent:
            checks.update(agent_ledger_checks(records, trace, verification))
        elif provision or world:
            checks.update((provision_ledger_checks if provision else world_ledger_checks)(records, trace))
            checks["not_autonomous"] = {"ok": verification.autonomous is False
                                        and verification.classification in ("scripted_replay", "scripted_fixture"),
                                        "classification": verification.classification}
        elif view:
            checks.update(view_ledger_checks(records, trace))
            checks["not_autonomous"] = {"ok": verification.autonomous is False
                                        and verification.classification in ("scripted_replay", "scripted_fixture"),
                                        "classification": verification.classification}
        else:
            checks.update(ledger_checks(records, trace))
            checks["not_autonomous"] = {"ok": verification.autonomous is False
                                        and verification.classification in ("scripted_replay", "scripted_fixture"),
                                        "classification": verification.classification}
    else:
        checks["ledger_verified"] = {"ok": False, "status": "missing"}
    if ordered is not None:
        verdict["ingestion"] = {"clamped_frames": ordered.clamped, "uncited_effects": ordered.uncited_effects,
                                "bridge_errors": ordered.bridge_errors, "bye": ordered.bye,
                                "late_or_engine_receipts": ordered.engine_receipts,
                                "inference_link_mismatches": ordered.inference_link_mismatches}
    selected = verdict.get("scenario")
    if selected and selected.get("table_declares_seed"):
        echoed = ((ordered.hello or {}).get("scenario") or {}).get("seed") if ordered is not None else None
        checks["scenario_seed_consistent"] = {"ok": echoed == selected["seed"], "manifest_and_start_script": selected["seed"],
                                              "engine_hello": echoed}
    verdict["checks"] = checks
    verdict["failed_checks"] = sorted(name for name, c in checks.items() if not c["ok"])
    verdict["verdict"] = "PASS" if not verdict["failed_checks"] and not verdict.get("error") else "FAIL"
    verdict["evidence"] = {
        "ledger": ledger_path.name,
        "ledger_sha256": launch.sha256_file(ledger_path) if ledger_path.exists() else None,
        "engine_stdout_sha256": launch.sha256_file(layout.stdout_path) if layout.stdout_path.exists() else None,
    }
    (layout.run_dir / "trace.json").write_text(json.dumps(trace, indent=1, sort_keys=True, default=str) + "\n")
    (layout.run_dir / "verdict.json").write_text(json.dumps(verdict, indent=1, sort_keys=True, default=str) + "\n")
    if layout.infolog_path.exists():
        # infolog is flushed at engine exit; keep a copy next to the verdict.
        (layout.run_dir / "infolog.txt").write_bytes(layout.infolog_path.read_bytes())
