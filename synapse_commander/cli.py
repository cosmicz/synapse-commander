"""Command line: ``python -m synapse_commander run-smoke ...``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from . import launch
from .launch import ENGINE_BINARIES, HEADLESS_BINARY
from .run import SmokeConfig, new_run_id, run_smoke


def add_run_arguments(command: argparse.ArgumentParser) -> None:
    """Launch arguments shared by run-smoke and run-agent."""
    command.add_argument("--engine-dir", type=Path, required=True,
                         help="directory holding the pinned spring-headless binary")
    command.add_argument("--engine-binary", choices=ENGINE_BINARIES, default=HEADLESS_BINARY,
                         help="engine program in --engine-dir: spring-headless (default) or the rendered "
                              "client spring, which inherits DISPLAY and GL settings from this environment")
    command.add_argument("--data-dir", type=Path, required=True,
                         help="engine data directory holding maps/ (and games/BAR.sdd unless --game-dir)")
    command.add_argument("--game-dir", type=Path, help="stock BAR checkout (default DATA_DIR/games/BAR.sdd)")
    command.add_argument("--out", type=Path, required=True,
                         help="private evidence root; the run writes OUT/RUN_ID/ and never reuses it")
    command.add_argument("--lock-dir", type=Path,
                         help="project runtime lock directory (atomic mkdir, vmlock.sh compatible)")
    command.add_argument("--rig-commit", required=True, help="exact rig commit recorded in the manifest")
    command.add_argument("--scenario", default=None,
                         help="ScenarioTable/v1 name in the app archive (e.g. rescue_smoke), passed to the "
                              "engine as synapse_scenario; unsupported by run-smoke")
    command.add_argument("--run-id", default=None, help="run id [A-Za-z0-9_-]{1,64} (default: generated)")
    command.add_argument("--hello-timeout", type=float, default=180.0, help="seconds to wait for the engine hello")
    command.add_argument("--allow-dirty", action="store_true",
                         help="permit a modified app checkout (recorded; not for evidence runs)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m synapse_commander")
    commands = parser.add_subparsers(dest="command", required=True)
    smoke = commands.add_parser(
        "run-smoke",
        help="run the Loop 0 headless smoke: scripted client -> gateway -> engine -> observation",
        description="Starts the native headless engine with the app archive, drives one scripted "
                    "advance intent and negative probes through the loopback gateway, records a "
                    "manifest and ledger, and writes verdict.json. Exit 0 only on PASS. The run is "
                    "a scripted fixture: not autonomous and not a visual demo.")
    add_run_arguments(smoke)
    agent = commands.add_parser(
        "run-agent",
        help="run the same smoke with a live model as the only commander",
        description="Same launch, gateway, recorder and lock as run-smoke, but every intent is chosen by "
                    "a live model (the authenticated Claude CLI, out of process) from gateway observations. "
                    "No probes are sent. Raw prompts and outputs go to OUT/RUN_ID/decisions.jsonl. "
                    "Exit 0 only on PASS, which requires the ledger to classify the run agent_only.")
    add_run_arguments(agent)
    provision = commands.add_parser(
        "run-provision",
        help="Loop 1 scripted smoke: PRODUCE one unit through the factory, refuse an unfunded one, advance it",
        description="Runs a scenario table (default rescue_smoke) with the scripted provisioning client: one "
                    "funded PRODUCE, the factory job and its unit observed with the installed controller, a "
                    "second PRODUCE of the spent chip refused with no effect, then a TASK advance of the produced "
                    "unit. Exit 0 only on PASS. A scripted fixture: not autonomous.")
    add_run_arguments(provision)
    world = commands.add_parser(
        "run-world",
        help="observe a seeded rescue world over HTTP (default rescue_baseline) and check it against the ledger",
        description="Sends no intents. Reads the initial world through the gateway for a bounded span and "
                    "checks the roster, budget accounting and the aged distress report against the "
                    "evaluator-only world_init record, and that no player observation carries hidden truth. "
                    "Exit 0 only on PASS. A scripted fixture.")
    add_run_arguments(world)
    view = commands.add_parser(
        "run-view",
        help="two-principal scripted view seam: M1 acts locally and sends a bounded message",
        description="Runs the view_smoke fixture with two private Bearer sessions, commander and M1. "
                    "Each client reads only its own principal observation stream; M1 advances its own "
                    "body and sends a bounded message through the gateway.  The ledger verifier checks "
                    "the separate streams.  Scripted fixture, not autonomous gameplay.")
    add_run_arguments(view)
    agent.add_argument("--model", default="claude-sonnet-5", help="model pinned for the CLI route and the manifest")
    agent.add_argument("--call-budget", type=int, default=20, help="maximum model calls")
    agent.add_argument("--call-timeout", type=float, default=60.0, help="seconds per model call")
    agent.add_argument("--deadline", type=float, default=600.0, help="wall seconds for the commander loop")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run-smoke" and args.scenario is not None:
        parser.error("run-smoke does not support --scenario; use run-provision or run-world for a scenario")
    if args.command in ("run-smoke", "run-agent", "run-provision", "run-world", "run-view"):
        extra = {}
        if args.command == "run-world":
            extra = dict(mode="world")
            args.scenario = args.scenario or "rescue_baseline"
        if args.command == "run-provision":
            extra = dict(mode="provision")
            args.scenario = args.scenario or "rescue_smoke"
        if args.command == "run-view":
            extra = dict(mode="view")
            args.scenario = args.scenario or "view_smoke"
        if args.command == "run-agent":
            extra = dict(mode="agent", model=args.model, call_budget=args.call_budget,
                         call_timeout_s=args.call_timeout, agent_deadline_s=args.deadline)
        config = SmokeConfig(engine_dir=args.engine_dir, data_dir=args.data_dir, game_dir=args.game_dir,
                             out_dir=args.out, lock_dir=args.lock_dir, rig_commit=args.rig_commit,
                             run_id=args.run_id or new_run_id(), allow_dirty=args.allow_dirty,
                             hello_timeout_s=args.hello_timeout, engine_binary=args.engine_binary,
                             scenario=args.scenario, **extra)
        try:
            verdict = run_smoke(config)
        except launch.LaunchError as exc:
            print(f"run-smoke: {exc}", file=sys.stderr)
            return 2
        summary = {key: verdict.get(key) for key in ("run_id", "mode", "presentation", "verdict", "failed_checks", "error",
                                                     "classification", "app_commit", "rig_commit")}
        summary["evidence_dir"] = verdict["run_id"]
        print(json.dumps(summary, indent=1))
        return 0 if verdict["verdict"] == "PASS" else 1
    return 2
