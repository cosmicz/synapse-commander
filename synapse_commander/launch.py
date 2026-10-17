"""Native Recoil launch: run directory, start script, process lifetime, lock.

Everything machine-specific (the private runtime directory, the evidence
location) is supplied by the caller at launch; nothing here names a path on
any particular host.  The engine runs as a child in its own process group, so
teardown signals only that group and never another project's processes.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import time
from typing import Mapping, Sequence


ENGINE_VERSION = "2026.07.04"
GAME_DIR_NAME = "BAR.sdd"
MAP_FILE = "supreme_isthmus_v2.1.sd7"
MAP_NAME = "Supreme Isthmus v2.1"
STOCK_GAME_TYPE = "Beyond All Reason $VERSION"
APP_ARCHIVE_DIR = "SynapseCommander.sdd"
APP_GAME_TYPE = "Synapse Commander 0"
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# The app-owned dependent archive ships in this repository, next to the package.
APP_ARCHIVE = Path(__file__).resolve().parent.parent / "game" / APP_ARCHIVE_DIR


class LaunchError(RuntimeError):
    """A launch precondition or lifetime check failed."""


def engine_arch(machine: str | None = None) -> str:
    machine = (machine or platform.machine()).lower()
    if machine in {"x86_64", "amd64"}:
        return "amd64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    raise LaunchError(f"unsupported architecture: {machine}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git_head(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def git_dirty_count(path: Path) -> int | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
            check=True, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return len([line for line in out.stdout.splitlines() if line.strip()])


@dataclass(frozen=True)
class RuntimeArtifacts:
    """The staged engine, stock game checkout, map and the app's game archive."""

    engine_dir: Path
    game_dir: Path
    map_path: Path
    app_archive: Path

    @classmethod
    def from_dirs(cls, *, engine_dir: Path, data_dir: Path, game_dir: Path | None = None) -> "RuntimeArtifacts":
        data_dir = data_dir.resolve()
        artifacts = cls(
            engine_dir=engine_dir.resolve(),
            game_dir=(game_dir or data_dir / "games" / GAME_DIR_NAME).resolve(),
            map_path=data_dir / "maps" / MAP_FILE,
            app_archive=APP_ARCHIVE,
        )
        artifacts.check()
        return artifacts

    @property
    def engine(self) -> Path:
        return self.engine_dir / "spring-headless"

    def check(self) -> None:
        missing = [
            str(path.name)
            for path in (self.engine, self.game_dir / "modinfo.lua", self.map_path,
                         self.app_archive / "modinfo.lua")
            if not path.exists()
        ]
        if missing:
            raise LaunchError("staged runtime artifacts missing: " + ", ".join(missing))

    def identity(self) -> dict[str, object]:
        """Exact identities for the manifest; hashes, commits, never host paths."""
        return {
            "engine": {"name": "Recoil", "version": ENGINE_VERSION, "binary": self.engine.name,
                       "sha256": sha256_file(self.engine)},
            "game": {"name": "Beyond All Reason", "version": "$VERSION", "commit": git_head(self.game_dir),
                     "modified_files": git_dirty_count(self.game_dir)},
            "map": {"name": MAP_NAME, "version": "2.1", "file": MAP_FILE,
                    "sha256": sha256_file(self.map_path)},
        }

    def archive_hashes(self) -> dict[str, str]:
        """SHA-256 of every file in the app archive, keyed by archive-relative path."""
        return {
            f"{APP_ARCHIVE_DIR}/{path.relative_to(self.app_archive).as_posix()}": sha256_file(path)
            for path in sorted(self.app_archive.rglob("*")) if path.is_file()
        }


def _settings_text(settings: Mapping[str, object]) -> str:
    lines = []
    for key in sorted(settings):
        if not key.isidentifier():
            raise LaunchError(f"invalid engine setting name: {key!r}")
        value = str(settings[key])
        if "\n" in value or "\r" in value:
            raise LaunchError(f"engine setting {key} spans lines")
        lines.append(f"{key} = {value}")
    return "".join(line + "\n" for line in lines)


def start_script(
    *,
    run_id: str,
    game_type: str = APP_GAME_TYPE,
    map_name: str = MAP_NAME,
    seed: int = 1,
    player_name: str = "SynapseCommander",
) -> str:
    """One player on team 0, no cheats or debug commands, no test runner.

    Foreign units for ownership probes are Gaia units the scenario spawns. The
    game never ends by itself; the launcher stops it.
    """
    check_run_id(run_id)
    for value in (game_type, map_name, player_name):
        if any(ch in value for ch in ";{}\n\r"):
            raise LaunchError("start script value contains a structural character")
    return f"""[GAME]
{{
\tMapName={map_name};
\tGameType={game_type};
\tGameStartDelay=0;
\tStartPosType=0;
\tRecordDemo=0;
\tMyPlayerName={player_name};
\tIsHost=1;
\tFixedRNGSeed={int(seed)};
\t[MODOPTIONS]
\t{{
\t\tdeathmode=neverend;
\t\tsynapse_run_id={run_id};
\t}}
\t[ALLYTEAM0]
\t{{
\t}}
\t[TEAM0]
\t{{
\t\tTeamLeader=0;
\t\tAllyTeam=0;
\t}}
\t[PLAYER0]
\t{{
\t\tName={player_name};
\t\tTeam=0;
\t\tSpectator=0;
\t}}
}}
"""


@dataclass(frozen=True)
class RunLayout:
    """Per-run private directories; the write dir is never shared between runs."""

    run_dir: Path

    @property
    def write_dir(self) -> Path:
        return self.run_dir / "write"

    @property
    def script_path(self) -> Path:
        return self.write_dir / "_synapse_script.txt"

    @property
    def settings_path(self) -> Path:
        return self.write_dir / "springsettings.cfg"

    def mailbox_dir(self, run_id: str) -> Path:
        return self.write_dir / "synapse" / check_run_id(run_id)

    @property
    def stdout_path(self) -> Path:
        return self.run_dir / "engine-stdout.log"

    @property
    def infolog_path(self) -> Path:
        return self.write_dir / "infolog.txt"

    def create(self, artifacts: RuntimeArtifacts) -> None:
        if self.run_dir.exists():
            raise LaunchError(f"run directory already exists: {self.run_dir.name}")
        (self.write_dir / "games").mkdir(parents=True)
        (self.write_dir / "maps").mkdir()
        # Links into the staged, read-only artifacts; the run writes only here.
        (self.write_dir / "games" / GAME_DIR_NAME).symlink_to(artifacts.game_dir, target_is_directory=True)
        (self.write_dir / "maps" / MAP_FILE).symlink_to(artifacts.map_path)
        (self.write_dir / "games" / APP_ARCHIVE_DIR).symlink_to(artifacts.app_archive, target_is_directory=True)

    def write_config(self, script: str, settings: Mapping[str, object]) -> None:
        self.script_path.write_text(script, encoding="ascii")
        self.settings_path.write_text(_settings_text(settings), encoding="ascii")


def engine_settings() -> dict[str, object]:
    """A per-run springsettings file, so no user or shared configuration is read."""
    return {}


def check_run_id(run_id: str) -> str:
    if not RUN_ID.fullmatch(run_id):
        raise LaunchError("run id must be 1-64 characters of [A-Za-z0-9_-]")
    return run_id


class EngineProcess:
    """spring-headless as a child process group with bounded teardown."""

    def __init__(self, artifacts: RuntimeArtifacts, layout: RunLayout) -> None:
        self.artifacts = artifacts
        self.layout = layout
        self.proc: subprocess.Popen[bytes] | None = None
        self.started_monotonic_ns: int | None = None
        self.teardown: dict[str, object] = {}

    def command(self) -> list[str]:
        return [
            str(self.artifacts.engine), "--isolation",
            "--write-dir", str(self.layout.write_dir),
            "--config", str(self.layout.settings_path),
            str(self.layout.script_path),
        ]

    def start(self) -> None:
        if self.proc is not None:
            raise LaunchError("engine already started")
        out = self.layout.stdout_path.open("wb")
        try:
            self.proc = subprocess.Popen(
                self.command(), cwd=self.layout.write_dir, stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
            )
        finally:
            out.close()
        self.started_monotonic_ns = time.monotonic_ns()

    @property
    def pid(self) -> int | None:
        return None if self.proc is None else self.proc.pid

    def poll(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    def stop(self, *, term_timeout_s: float = 10.0, kill_timeout_s: float = 5.0) -> dict[str, object]:
        """SIGTERM the engine's own process group, then SIGKILL; never wider."""
        if self.proc is None:
            return {"started": False}
        pgid = self.proc.pid  # start_new_session: the child leads its own group
        record: dict[str, object] = {"pgid": pgid, "already_exited": self.proc.poll() is not None}
        begin = time.monotonic()
        if self.proc.poll() is None:
            _signal_group(pgid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=term_timeout_s)
                record["signal"] = "SIGTERM"
            except subprocess.TimeoutExpired:
                _signal_group(pgid, signal.SIGKILL)
                self.proc.wait(timeout=kill_timeout_s)
                record["signal"] = "SIGKILL"
        # Reap any straggler left in the group after the leader exited.
        if _group_alive(pgid):
            _signal_group(pgid, signal.SIGKILL)
            record["group_sigkill"] = True
        record["exit"] = self.proc.returncode
        record["group_alive_after"] = _group_alive(pgid)
        record["teardown_s"] = round(time.monotonic() - begin, 3)
        self.teardown = record
        return record


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_log(path: Path, needles: Sequence[str], *, timeout_s: float,
                 engine: EngineProcess | None = None, poll_s: float = 0.25) -> str:
    """Wait until the engine log contains one of NEEDLES; fail on exit or timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in needles:
                if needle in text:
                    return needle
        if engine is not None and engine.poll() is not None:
            raise LaunchError(f"engine exited with {engine.poll()} before {needles[0]!r}")
        time.sleep(poll_s)
    raise LaunchError(f"timed out after {timeout_s}s waiting for {needles[0]!r}")


class RuntimeLock:
    """The project's runtime lock, compatible with the rig's vmlock.sh.

    Acquisition is an atomic mkdir with an owner file carrying ``pid=N``.  A
    waiter breaks the lock only when that pid is gone and no project container
    runs, serialized by a ``breaking`` marker; otherwise it waits up to the
    given bound and fails.
    """

    def __init__(self, lock_dir: Path, owner_text: str, *, wait_s: float = 1200.0, poll_s: float = 1.0) -> None:
        self.lock_dir = lock_dir
        self.owner_text = owner_text
        self.wait_s = wait_s
        self.poll_s = poll_s
        self.held = False
        self.waited_s = 0.0

    def acquire(self) -> None:
        self.lock_dir.parent.mkdir(parents=True, exist_ok=True)
        begin = time.monotonic()
        while True:
            try:
                self.lock_dir.mkdir()
            except FileExistsError:
                if not self._try_break() and time.monotonic() - begin >= self.wait_s:
                    raise LaunchError(f"runtime lock busy after {self.wait_s}s: {self._owner() or 'acquiring'}")
                time.sleep(self.poll_s)
                continue
            (self.lock_dir / "owner").write_text(f"{self.owner_text} pid={os.getpid()}\n", encoding="utf-8")
            self.held = True
            self.waited_s = round(time.monotonic() - begin, 3)
            return

    def release(self) -> None:
        if not self.held:
            return
        owner = self._owner()
        if owner is not None and f"pid={os.getpid()}" not in owner.split():
            raise LaunchError("runtime lock is owned by someone else; not releasing")
        (self.lock_dir / "owner").unlink(missing_ok=True)
        self.lock_dir.rmdir()
        self.held = False

    def __enter__(self) -> "RuntimeLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    def _owner(self) -> str | None:
        try:
            return (self.lock_dir / "owner").read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None

    def _try_break(self) -> bool:
        owner = self._owner()
        if owner is None:
            try:
                age = time.time() - self.lock_dir.stat().st_mtime
            except FileNotFoundError:
                return True
            if age < 30:
                return False
        else:
            pid = _owner_pid(owner)
            if pid is None or _pid_alive(pid) or _project_containers_running():
                return False
        marker = self.lock_dir / "breaking"
        try:
            marker.mkdir()
        except FileExistsError:
            return False
        except FileNotFoundError:
            return True
        try:
            if self._owner() != owner:
                return False
            (self.lock_dir / "owner").unlink(missing_ok=True)
        finally:
            marker.rmdir()
        try:
            self.lock_dir.rmdir()
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ENOTEMPTY):
                raise
        return True


def _owner_pid(owner: str) -> int | None:
    for token in owner.split():
        if token.startswith("pid=") and token[4:].isdigit():
            return int(token[4:])
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _project_containers_running() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell: do not break
    return any(name.startswith("synapse-commander-") for name in out.stdout.split())
