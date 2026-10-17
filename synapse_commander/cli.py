"""Command line: ``python -m synapse_commander run-smoke ...``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from . import launch
from .run import SmokeConfig, new_run_id, run_smoke


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
    smoke.add_argument("--engine-dir", type=Path, required=True,
                       help="directory holding the pinned spring-headless binary")
    smoke.add_argument("--data-dir", type=Path, required=True,
                       help="engine data directory holding maps/ (and games/BAR.sdd unless --game-dir)")
    smoke.add_argument("--game-dir", type=Path, help="stock BAR checkout (default DATA_DIR/games/BAR.sdd)")
    smoke.add_argument("--out", type=Path, required=True,
                       help="private evidence root; the run writes OUT/RUN_ID/ and never reuses it")
    smoke.add_argument("--lock-dir", type=Path,
                       help="project runtime lock directory (atomic mkdir, vmlock.sh compatible)")
    smoke.add_argument("--rig-commit", required=True, help="exact rig commit recorded in the manifest")
    smoke.add_argument("--run-id", default=None, help="run id [A-Za-z0-9_-]{1,64} (default: generated)")
    smoke.add_argument("--hello-timeout", type=float, default=180.0, help="seconds to wait for the engine hello")
    smoke.add_argument("--allow-dirty", action="store_true",
                       help="permit a modified app checkout (recorded; not for evidence runs)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run-smoke":
        config = SmokeConfig(engine_dir=args.engine_dir, data_dir=args.data_dir, game_dir=args.game_dir,
                             out_dir=args.out, lock_dir=args.lock_dir, rig_commit=args.rig_commit,
                             run_id=args.run_id or new_run_id(), allow_dirty=args.allow_dirty,
                             hello_timeout_s=args.hello_timeout)
        try:
            verdict = run_smoke(config)
        except launch.LaunchError as exc:
            print(f"run-smoke: {exc}", file=sys.stderr)
            return 2
        summary = {key: verdict.get(key) for key in ("run_id", "verdict", "failed_checks", "error",
                                                     "classification", "app_commit", "rig_commit")}
        summary["evidence_dir"] = verdict["run_id"]
        print(json.dumps(summary, indent=1))
        return 0 if verdict["verdict"] == "PASS" else 1
    return 2
