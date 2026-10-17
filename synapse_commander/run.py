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
from .gateway import LoopbackGateway
from .recording import EFFECT, INTENT_RECEIPT, Recorder, verify_ledger


SCENARIO = {"id": "loop0-advance-smoke", "version": "0", "seed": 1}
ADAPTER_VERSION = "synapse-mailbox/0+gateway/v1"
PRINCIPAL = "scripted-reference"
TEAM = "0"
FPS = 30
ORDER_DONE_LIMIT_FRAMES = 600
CLASSIFICATION_LABEL = "scripted fixture: headless, scripted reference client, not autonomous, not visual"


class SmokeTransport(Protocol):
    def hello(self) -> dict[str, Any] | None: ...
    def current_frame(self) -> int: ...
    def observation_for(self, team: str) -> Any: ...
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

    # LedgerSink, used by the gateway
    def record(self, kind: str, *, sim_frame: int, origin: str, payload: dict[str, Any],
               causes: tuple[str, ...] | list[str] = ()) -> str:
        with self._lock:
            self._fetch()
            self._drain(upto=sim_frame)
            return self._write(kind, sim_frame=sim_frame, origin=origin, payload=payload, causes=causes)

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


def build_manifest(*, run_id: str, rig_commit: str | None, app_commit: str, identity: dict[str, Any],
                   config_hashes: dict[str, str]) -> dict[str, Any]:
    return {
        "schema": "synapse.manifest/v1",
        "run_id": run_id,
        "rig_commit": rig_commit,
        "app_commit": app_commit,
        "engine": identity["engine"],
        "game": identity["game"],
        "map": identity["map"],
        "scenario": dict(SCENARIO),
        "controller_images": {},
        "adapter_version": ADAPTER_VERSION,
        "principals": [PRINCIPAL],
        "config_hashes": config_hashes,
        "model_id": None,
        "external_budgets": {},
        "clock_mode": "continuous",
        "fps": FPS,
        "presentation": "headless",
        "required_decisions": [],
    }


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_smoke(config: SmokeConfig, *,
              transport_factory: Callable[[Path, str], SmokeTransport] | None = None,
              engine_factory: Callable[[launch.RunLayout], SmokeEngine] | None = None,
              identity: dict[str, Any] | None = None,
              app: tuple[str, int] | None = None,
              engine_scan: Callable[[int | None], list[int]] = launch.other_engines) -> dict[str, Any]:
    """Run one Loop 0 smoke and return its verdict; writes evidence under out_dir/run_id."""
    run_id = launch.check_run_id(config.run_id)
    if not config.rig_commit:
        raise launch.LaunchError("rig commit is required for the manifest")
    app_commit, app_dirty = app or app_identity()
    if app_dirty and not config.allow_dirty:
        raise launch.LaunchError(f"application checkout has {app_dirty} modified files; commit first")
    layout = launch.RunLayout(config.out_dir / run_id)
    artifacts = None
    if engine_factory is None:
        artifacts = launch.RuntimeArtifacts.from_dirs(engine_dir=config.engine_dir, data_dir=config.data_dir,
                                                      game_dir=config.game_dir)
        layout.create(artifacts)
        identity = identity or artifacts.identity()
    else:
        layout.write_dir.mkdir(parents=True)
    script = launch.start_script(run_id=run_id, host_port=launch.free_udp_port())
    settings = launch.engine_settings()
    layout.write_config(script, settings)
    mailbox_dir = layout.mailbox_dir
    mailbox_dir.mkdir(parents=True, exist_ok=True)

    config_hashes = {"start_script": sha256_text(script),
                     "springsettings": sha256_text(launch._settings_text(settings))}
    if artifacts is not None:
        config_hashes.update(artifacts.archive_hashes())
    package = Path(__file__).resolve().parent
    for module in ("mailbox.py", "gateway.py", "protocol.py", "recording.py", "run.py", "launch.py",
                   "agents/scripted.py"):
        path = package / module
        if path.exists():
            config_hashes[f"synapse_commander/{module}"] = launch.sha256_file(path)
    identity = identity or {"engine": {"name": "none", "version": "none"},
                            "game": {"name": "none", "version": "none"},
                            "map": {"name": "none", "version": "none"}}
    manifest = build_manifest(run_id=run_id, rig_commit=config.rig_commit, app_commit=app_commit,
                              identity=identity, config_hashes=config_hashes)
    (mailbox_dir / "run.json").write_text(json.dumps(
        {"run_id": run_id, "game": launch.APP_GAME_TYPE, "map": launch.MAP_NAME,
         "start_script_sha256": config_hashes["start_script"], "app_commit": app_commit}, indent=1) + "\n")

    lock = None
    if config.lock_dir is not None:
        lock = launch.RuntimeLock(config.lock_dir, f"plt-loop {run_id} {utc_now()}", wait_s=config.lock_wait_s)
    verdict: dict[str, Any] = {"run_id": run_id, "classification": CLASSIFICATION_LABEL,
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

        gateway = LoopbackGateway(transport, principal=PRINCIPAL, team=TEAM, origin="scripted", ledger=ordered)
        server = gateway.make_server(0)
        threading.Thread(target=server.serve_forever, name="gateway", daemon=True).start()

        def pump_loop() -> None:
            while not pump_stop.wait(0.2):
                others = engine_scan(getattr(engine, "pid", None))
                if others:
                    seen = verdict.setdefault("other_engines_during_run", [])
                    seen.extend(pid for pid in others if pid not in seen)
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

        client = GatewayClient(f"http://127.0.0.1:{server.server_port}")
        smoke = ScriptedSmoke(client, wall_timeout_s=config.client_timeout_s, poll_s=config.client_poll_s)
        try:
            trace = smoke.run()
        except Exception as exc:  # keep the partial transcript as evidence
            error = f"client: {type(exc).__name__}: {exc}"
            trace = smoke.trace
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
            checks = evaluate(trace, ordered, verdict)
            outcome = {"pre_ledger_checks": "PASS" if checks and all(c["ok"] for c in checks.values())
                       and not error else "FAIL", "failed": sorted(n for n, c in checks.items() if not c["ok"]),
                       "error": error, "classification": CLASSIFICATION_LABEL}
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
    _finish(verdict, trace, ordered, ledger_path, layout)
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
    others = sorted(set(verdict.get("other_engines_at_start", [])) | set(verdict.get("other_engines_during_run", [])))
    check("exclusive_runtime", not others, other_engine_pids=others,
          detail="no other spring-headless process ran on this host during the run")
    teardown = verdict.get("teardown") or {}
    check("teardown_bounded", teardown.get("group_alive_after") is False
          and float(teardown.get("teardown_s", 1e9)) <= 20.0, teardown=teardown)
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


def _probe_id(trace: dict[str, Any], name: str) -> str | None:
    for probe in trace.get("probes", []):
        if probe.get("name") == name:
            return probe.get("intent_id")
    return None


def _finish(verdict: dict[str, Any], trace: dict[str, Any], ordered: OrderedLedger | None,
            ledger_path: Path, layout: launch.RunLayout) -> None:
    checks = evaluate(trace, ordered, verdict)
    verification = verify_ledger(ledger_path) if ledger_path.exists() else None
    if verification is not None:
        checks["ledger_verified"] = {"ok": verification.ok, "status": verification.status,
                                     "errors": verification.errors,
                                     "classification": verification.classification,
                                     "autonomous": verification.autonomous,
                                     "evidence_limits": verification.evidence_limits,
                                     "counts": verification.counts}
        checks.update(ledger_checks(verification.records if verification.ok else _ledger_records(ledger_path), trace))
        checks["not_autonomous"] = {"ok": verification.autonomous is False
                                    and verification.classification in ("scripted_replay", "scripted_fixture"),
                                    "classification": verification.classification}
    else:
        checks["ledger_verified"] = {"ok": False, "status": "missing"}
    if ordered is not None:
        verdict["ingestion"] = {"clamped_frames": ordered.clamped, "uncited_effects": ordered.uncited_effects,
                                "bridge_errors": ordered.bridge_errors, "bye": ordered.bye,
                                "late_or_engine_receipts": ordered.engine_receipts}
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
