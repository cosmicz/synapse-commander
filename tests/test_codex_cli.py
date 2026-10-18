"""Offline boundary tests for the tool-free Codex CLI route."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from synapse_commander.agents.codex_cli import (
    CliCompleted,
    CodexCliRoute,
    DISABLED_FEATURES,
    MAX_PROMPT_BYTES,
    _run_process,
    parse_events,
)


def events(*values: dict) -> bytes:
    return ("\n".join(json.dumps(value) for value in values) + "\n").encode()


def successful_events(*, model: str = "gpt-actual", text: str = '{"action":"wait"}') -> bytes:
    return events({"type": "turn.started", "model": model},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                  {"type": "turn.completed", "model": model})


class CodexCliRouteTests(unittest.TestCase):
    def route(self, runner, **kwargs):
        return CodexCliRoute("gpt-requested", system_prompt="Game system prompt", runner=runner,
                             environment={"PATH": "/bin", "HOME": "/host-home", "CODEX_HOME": "/auth-home",
                                          "UNRELATED_SECRET": "must-not-pass"}, **kwargs)

    def test_argv_environment_and_stdin_are_explicitly_isolated(self):
        seen = {}

        def runner(argv, prompt, **kwargs):
            seen.update(argv=argv, prompt=prompt, **kwargs)
            self.assertEqual(list(Path(kwargs["cwd"]).iterdir()), [])
            self.assertEqual(list(Path(kwargs["environment"]["HOME"]).iterdir()), [])
            return CliCompleted(0, successful_events())

        reply = self.route(runner).call("local observation")
        self.assertIsNone(reply.error)
        self.assertEqual(reply.text, '{"action":"wait"}')
        self.assertEqual(reply.model_ids, ["gpt-requested"], "the route has an explicit no-fallback model")
        argv = seen["argv"]
        self.assertEqual(argv[:9], ["codex", "--ask-for-approval", "never", "exec", "--ignore-user-config", "--ignore-rules",
                                    "--ephemeral", "--json", "--skip-git-repo-check"])
        self.assertIn("-", argv)
        self.assertNotIn("local observation", argv)
        self.assertIn(b"local observation", seen["prompt"])
        self.assertIn(b"Game system prompt", seen["prompt"])
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn('web_search="disabled"', argv)
        self.assertIn("mcp_servers={}", argv)
        for feature in DISABLED_FEATURES:
            self.assertIn(f"features.{feature}=false", argv)
        self.assertEqual(seen["environment"], {"HOME": seen["environment"]["HOME"], "CODEX_HOME": "/auth-home",
                                                 "TERM": "dumb", "PATH": "/bin"})
        self.assertEqual(seen["output_limit"], 131_072)

    def test_tool_or_unknown_item_event_fails_closed(self):
        for stream in (
            events({"type": "turn.started", "model": "gpt-actual"},
                   {"type": "item.completed", "item": {"type": "command_execution", "command": "id"}}),
            events({"type": "turn.started", "model": "gpt-actual"},
                   {"type": "item.completed", "item": {"type": "mystery"}}),
        ):
            with self.subTest(stream=stream):
                with self.assertRaisesRegex(ValueError, "tool or invalid"):
                    parse_events(stream)

    def test_requires_one_final_message(self):
        cases = (
            (events({"type": "turn.started"}), "exactly"),
            (events({"type": "turn.started", "model": "gpt-a"},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}}), "exactly"),
        )
        for stream, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    parse_events(stream)

    def test_timeout_and_oversize_prompt_never_become_decisions(self):
        calls = []

        def timeout(*_args, **_kwargs):
            calls.append(1)
            return CliCompleted(-15, b"", timed_out=True)

        reply = self.route(timeout, timeout_s=3).call("p")
        self.assertEqual(reply.error, "timeout after 3.0s")
        self.assertEqual(calls, [1])
        reply = self.route(timeout).call("x" * MAX_PROMPT_BYTES)
        self.assertEqual(reply.error, "route unavailable")
        self.assertEqual(calls, [1], "invalid input must not launch a child")

    def test_timeout_terminates_the_fresh_process_group(self):
        # The helper is tested with Python, never a Codex call.  Its child is
        # in the same fresh session and must die with the parent timeout.
        script = (
            "import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            "print(child.pid, flush=True); time.sleep(30)"
        )
        with tempfile.TemporaryDirectory() as root:
            done = _run_process([sys.executable, "-c", script], b"", cwd=root,
                                environment={"PATH": os.environ["PATH"]}, timeout_s=.1, output_limit=4096)
        self.assertTrue(done.timed_out)
        self.assertLess(len(done.stdout), 4096)

    def test_output_cap_stops_a_noisy_child(self):
        with tempfile.TemporaryDirectory() as root:
            done = _run_process([sys.executable, "-c", "import sys; sys.stdout.write('x' * 100000)"], b"", cwd=root,
                                environment={"PATH": os.environ["PATH"]}, timeout_s=5, output_limit=4096)
        self.assertTrue(done.output_too_large)
        self.assertLessEqual(len(done.stdout), 4096)

