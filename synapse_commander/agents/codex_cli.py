"""A deliberately tool-free Codex CLI route for embodied game minds.

This adapter is intentionally independent of the Claude CLI adapter.  It
starts an ephemeral Codex process in an empty directory, passes the bounded
game prompt only on standard input, and accepts only a final agent message
from Codex's JSONL event stream.  A tool event is an error even though the
invocation also disables all known local tools: the event check is the
fail-closed backstop for a future CLI change.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Mapping

from .commander import DEFAULT_CALL_TIMEOUT_S, ModelReply


MAX_PROMPT_BYTES = 65_536
MAX_SYSTEM_PROMPT_BYTES = 16_384
MAX_OUTPUT_BYTES = 131_072
MAX_MODEL_CHARS = 256
MAX_MESSAGE_CHARS = 32_768

# These are explicit invocation settings rather than inherited user settings.
# ``web_search`` is a Codex config setting; the rest are feature gates in
# Codex 0.163.  MCP is additionally replaced with an empty table below.
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "hooks", "multi_agent",
    "browser_use", "computer_use", "view_image", "image_generation", "sleep_tool",
    "code_mode_host",
)


@dataclass(frozen=True)
class CliCompleted:
    """Bounded, redacted process result supplied to the event parser."""

    returncode: int
    stdout: bytes
    timed_out: bool = False
    output_too_large: bool = False


def _ms_since(begin: float) -> int:
    return int((time.monotonic() - begin) * 1000)


def _is_model(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= MAX_MODEL_CHARS


def _contains_tool_event(event: dict[str, Any]) -> bool:
    """Recognise every tool/command event shape emitted by the JSON protocol."""
    kind = event.get("type")
    if isinstance(kind, str) and any(word in kind.lower() for word in ("tool", "exec", "command", "mcp")):
        return True
    item = event.get("item")
    if not isinstance(item, dict):
        return False
    item_kind = item.get("type")
    if not isinstance(item_kind, str):
        return True
    # Reasoning and the terminal agent message are the only item kinds that
    # this one-turn route accepts.  Unknown items fail closed rather than
    # becoming an unreviewed new capability.
    return item_kind not in {"reasoning", "agent_message", "error"}


def _agent_message(item: dict[str, Any]) -> str | None:
    text = item.get("text")
    if isinstance(text, str):
        return text
    # Older CLI JSONL releases represented message content as a single text
    # content item.  Do not concatenate multiple parts: that would silently
    # change the one-final-message contract.
    content = item.get("content")
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
        value = content[0].get("text")
        return value if isinstance(value, str) else None
    return None


def parse_events(raw: bytes) -> str:
    """Return exactly one final message from the Codex JSONL stream.

    JSON is deliberately line-framed.  There is no recovery from malformed
    events, tool activity, or multiple final messages are route errors.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("CLI output is not UTF-8") from exc
    messages: list[str] = []
    lines = text.splitlines()
    if not lines:
        raise ValueError("CLI produced no JSON events")
    for line in lines:
        if not line or len(line.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError("CLI event is invalid")
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("CLI output is not JSONL") from exc
        if not isinstance(event, dict) or _contains_tool_event(event):
            raise ValueError("CLI emitted a tool or invalid event")
        if event.get("type") == "item.completed" and isinstance(event.get("item"), dict):
            item = event["item"]
            if item.get("type") == "agent_message":
                message = _agent_message(item)
                if not isinstance(message, str) or len(message) > MAX_MESSAGE_CHARS:
                    raise ValueError("CLI final message is invalid")
                messages.append(message)
    if len(messages) != 1:
        raise ValueError("CLI did not emit exactly one final agent message")
    return messages[0]


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    """Stop the CLI and any descendants started in its fresh process group."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    try:
        process.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _run_process(argv: list[str], prompt: bytes, *, cwd: str, environment: Mapping[str, str],
                 timeout_s: float, output_limit: int) -> CliCompleted:
    """Run one CLI process without allowing unbounded pipe accumulation."""
    with tempfile.TemporaryFile() as stdin_file:
        stdin_file.write(prompt)
        stdin_file.seek(0)
        try:
            process = subprocess.Popen(
                argv, stdin=stdin_file, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=cwd, env=dict(environment), start_new_session=True,
            )
        except OSError:
            return CliCompleted(-1, b"")
        stdout = bytearray()
        total_output = 0
        overflow = threading.Event()
        lock = threading.Lock()

        def drain(stream: Any, *, keep: bool) -> None:
            nonlocal total_output
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                with lock:
                    if total_output + len(chunk) > output_limit:
                        overflow.set()
                        if keep:
                            remaining = max(0, output_limit - len(stdout))
                            stdout.extend(chunk[:remaining])
                    else:
                        total_output += len(chunk)
                        if keep:
                            stdout.extend(chunk)

        assert process.stdout is not None and process.stderr is not None
        readers = [threading.Thread(target=drain, args=(process.stdout,), kwargs={"keep": True}, daemon=True),
                   threading.Thread(target=drain, args=(process.stderr,), kwargs={"keep": False}, daemon=True)]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + timeout_s
        timed_out = False
        while process.poll() is None and not overflow.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            overflow.wait(min(remaining, 0.02))
        if timed_out or overflow.is_set():
            _terminate_group(process)
        try:
            returncode = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _terminate_group(process)
            returncode = process.wait(timeout=2)
        for reader in readers:
            reader.join(timeout=2)
        process.stdout.close()
        process.stderr.close()
        return CliCompleted(returncode, bytes(stdout[:output_limit]), timed_out=timed_out,
                            output_too_large=overflow.is_set())


class CodexCliRoute:
    """One ephemeral, tool-free Codex CLI turn for a bounded game observation."""

    name = "codex-cli"

    def __init__(self, model: str, *, system_prompt: str, timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
                 executable: str = "codex", environment: Mapping[str, str] | None = None,
                 runner: Callable[..., CliCompleted] = _run_process) -> None:
        if not _is_model(model):
            raise ValueError("model must be a bounded non-empty string")
        if not isinstance(system_prompt, str) or not system_prompt or len(system_prompt.encode("utf-8")) > MAX_SYSTEM_PROMPT_BYTES:
            raise ValueError("system_prompt must be a bounded non-empty string")
        if (not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool)
                or not math.isfinite(timeout_s) or not 0 < timeout_s <= 600):
            raise ValueError("timeout_s must be a positive finite value no greater than 600")
        if not isinstance(executable, str) or not executable or len(executable) > 512:
            raise ValueError("executable must be a bounded non-empty string")
        self.model, self.system_prompt, self.timeout_s = model, system_prompt, float(timeout_s)
        self.executable, self._environment, self._runner = executable, (os.environ if environment is None else environment), runner

    def argv(self, instructions_path: Path) -> list[str]:
        argv = [self.executable, "--ask-for-approval", "never", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--json",
                "--skip-git-repo-check", "--sandbox", "read-only",
                "--config", 'web_search="disabled"', "--config", "mcp_servers={}",
                "--config", f"model_instructions_file={json.dumps(str(instructions_path))}",
                "--config", "project_doc_max_bytes=0"]
        for feature in DISABLED_FEATURES:
            argv.extend(("--config", f"features.{feature}=false"))
        return [*argv, "--model", self.model, "-"]

    def _environment_for(self, home: Path) -> dict[str, str]:
        inherited_home = self._environment.get("CODEX_HOME")
        if not inherited_home:
            source_home = self._environment.get("HOME")
            inherited_home = str(Path(source_home) / ".codex") if source_home else str(Path.home() / ".codex")
        auth_home = Path(inherited_home)
        if not auth_home.is_absolute():
            raise ValueError("CODEX_HOME must be absolute")
        env = {"HOME": str(home), "CODEX_HOME": str(auth_home), "TERM": "dumb"}
        for key in ("PATH", "LANG", "LC_ALL"):
            value = self._environment.get(key)
            if isinstance(value, str) and value:
                env[key] = value
        if "PATH" not in env:
            raise ValueError("PATH is required for the Codex executable")
        return env

    def _stdin_prompt(self, prompt: str) -> bytes:
        if not isinstance(prompt, str):
            raise ValueError("invalid prompt")
        encoded = prompt.encode("utf-8")
        if len(encoded) > MAX_PROMPT_BYTES:
            raise ValueError("invalid prompt")
        return encoded

    def call(self, prompt: str) -> ModelReply:
        begin = time.monotonic()
        try:
            stdin = self._stdin_prompt(prompt)
            with tempfile.TemporaryDirectory(prefix="synapse-codex-") as root:
                root_path = Path(root)
                cwd, home = root_path / "cwd", root_path / "home"
                cwd.mkdir()
                home.mkdir()
                instructions = root_path / "model_instructions.md"
                instructions.write_text(self.system_prompt, encoding="utf-8")
                instructions.chmod(0o600)
                done = self._runner(self.argv(instructions), stdin, cwd=str(cwd), environment=self._environment_for(home),
                                    timeout_s=self.timeout_s, output_limit=MAX_OUTPUT_BYTES)
        except (OSError, UnicodeError, ValueError):
            return ModelReply("", [], _ms_since(begin), "", error="route unavailable")
        raw = done.stdout.decode("utf-8", errors="replace")
        latency = _ms_since(begin)
        if done.timed_out:
            return ModelReply("", [], latency, raw, error=f"timeout after {self.timeout_s}s")
        if done.output_too_large:
            return ModelReply("", [], latency, raw, error="CLI output too large")
        if done.returncode != 0:
            return ModelReply("", [], latency, raw, error=f"exit {done.returncode}")
        try:
            text = parse_events(done.stdout)
        except ValueError as exc:
            return ModelReply("", [], latency, raw, error=str(exc))
        # The 0.163 JSONL lifecycle does not include a model field.  The route
        # supplies an explicit --model and has no fallback path, so that exact
        # configured identifier is disclosed as configured, not provider-reported.
        return ModelReply(text, [self.model], latency, raw, model_provenance="configured_no_fallback")


__all__ = ["CodexCliRoute", "CliCompleted", "DISABLED_FEATURES", "MAX_OUTPUT_BYTES", "MAX_PROMPT_BYTES", "parse_events"]
