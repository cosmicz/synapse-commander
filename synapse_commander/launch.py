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
import socket
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
# The same pinned engine directory ships both; the rendered client needs a display.
HEADLESS_BINARY = "spring-headless"
ENGINE_BINARIES = (HEADLESS_BINARY, "spring")
# Environment the engine inherits that decides how (and whether) it renders;
# recorded in the private verdict. The launcher never sets any of these itself.
RENDER_ENV_PREFIXES = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "LIBGL_", "GALLIUM_", "LP_", "MESA_",
                       "__GL", "__EGL", "EGL_", "SDL_", "LD_PRELOAD", "LD_LIBRARY_PATH")
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
    engine_binary: str = HEADLESS_BINARY

    @classmethod
    def from_dirs(cls, *, engine_dir: Path, data_dir: Path, game_dir: Path | None = None,
                  engine_binary: str = HEADLESS_BINARY) -> "RuntimeArtifacts":
        if engine_binary not in ENGINE_BINARIES:
            raise LaunchError(f"engine binary must be one of {', '.join(ENGINE_BINARIES)}")
        data_dir = data_dir.resolve()
        artifacts = cls(
            engine_dir=engine_dir.resolve(),
            game_dir=(game_dir or data_dir / "games" / GAME_DIR_NAME).resolve(),
            map_path=data_dir / "maps" / MAP_FILE,
            app_archive=APP_ARCHIVE,
            engine_binary=engine_binary,
        )
        artifacts.check()
        return artifacts

    @property
    def engine(self) -> Path:
        return self.engine_dir / self.engine_binary

    @property
    def presentation(self) -> str:
        """The recorder's presentation: the headless binary draws nothing."""
        return "headless" if self.engine_binary == HEADLESS_BINARY else "rendered"

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
    host_port: int = 8452,
    scenario: str | None = None,
) -> str:
    """One player on team 0, no cheats or debug commands, no test runner.

    The engine's own game server listens on loopback only, on HOST_PORT, so
    concurrent engines on one host do not collide on the default port.

    Foreign units for ownership probes are Gaia units the scenario spawns. The
    game never ends by itself; the launcher stops it.
    """
    check_run_id(run_id)
    if scenario is not None and not RUN_ID.fullmatch(scenario):
        raise LaunchError("scenario name must be 1-64 characters of [A-Za-z0-9_-]")
    if not 1024 <= int(host_port) <= 65535:
        raise LaunchError("host port out of range")
    for value in (game_type, map_name, player_name):
        if any(ch in value for ch in ";{}\n\r"):
            raise LaunchError("start script value contains a structural character")
    # No scenario keeps Loop 0 exactly as before; a named one selects the
    # archive's scenarios/<name>.json in the gadget.
    scenario_line = "" if scenario is None else f"\n\t\tsynapse_scenario={scenario};"
    return f"""[GAME]
{{
\tMapName={map_name};
\tGameType={game_type};
\tGameStartDelay=0;
\tStartPosType=0;
\tRecordDemo=0;
\tMyPlayerName={player_name};
\tIsHost=1;
\tHostIP=127.0.0.1;
\tHostPort={int(host_port)};
\tFixedRNGSeed={int(seed)};
\t[MODOPTIONS]
\t{{
\t\tdeathmode=neverend;
\t\tsynapse_run_id={run_id};{scenario_line}
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

    @property
    def mailbox_dir(self) -> Path:
        """synapse-mailbox/0 directory; the engine resolves synapse/ in its write dir."""
        return self.write_dir / "synapse"

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


def free_udp_port() -> int:
    """A currently free loopback UDP port for the engine's game server."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def engine_settings() -> dict[str, object]:
    """A per-run springsettings file, so no user or shared configuration is read."""
    return {}


def check_run_id(run_id: str) -> str:
    if not RUN_ID.fullmatch(run_id):
        raise LaunchError("run id must be 1-64 characters of [A-Za-z0-9_-]")
    return run_id


class EngineProcess:
    """The engine as a child process group with bounded teardown.

    The child inherits this process's environment unchanged, so DISPLAY and GL
    settings reach a rendered client; nothing is added or removed here.
    """

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
        # Kill any straggler left in the group after the leader exited, then give
        # the kernel and init a bounded moment to finish and reap it.
        if _group_alive(pgid):
            _signal_group(pgid, signal.SIGKILL)
            record["group_sigkill"] = True
        record["exit"] = self.proc.returncode
        record["group_alive_after"] = not _wait_group_gone(pgid, GROUP_GONE_TIMEOUT_S)
        record["teardown_s"] = round(time.monotonic() - begin, 3)
        self.teardown = record
        return record


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


GROUP_GONE_TIMEOUT_S = 1.0


def _group_alive(pgid: int) -> bool:
    """True while any process in the group still runs; zombies awaiting reaping do not count."""
    proc = Path("/proc")
    if not proc.is_dir():
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    for entry in proc.glob("[0-9]*"):
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # "pid (comm) state ppid pgrp ...": comm may hold spaces or parentheses.
        fields = stat[stat.rfind(")") + 2:].split()
        if len(fields) >= 3 and fields[2] == str(pgid) and fields[0] not in ("Z", "X"):
            return True
    return False


def _wait_group_gone(pgid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while _group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def render_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The inherited variables that affect rendering, as the engine will see them."""
    environ = os.environ if environ is None else environ
    return {key: environ[key] for key in sorted(environ) if key.startswith(RENDER_ENV_PREFIXES)}


def other_engines(own_pgid: int | None = None, *,
                  binary: str | tuple[str, ...] = ENGINE_BINARIES) -> list[int]:
    """Pids of engine processes outside our own process group (Linux /proc).

    A process is an engine when its executable (/proc/PID/exe) is a file named
    spring-headless or spring, however it was started; a script or a symlink to another
    program merely named so is not. Only when the executable cannot be read
    does the process name (argv[0]) decide, and the record says so.
    """
    return sorted(engine_processes(own_pgid, binary=binary))


def engine_processes(own_pgid: int | None = None, *,
                     binary: str | tuple[str, ...] = ENGINE_BINARIES) -> dict[int, dict[str, str]]:
    """Engine pids with their command line and how each was identified."""
    names = {binary} if isinstance(binary, str) else set(binary)
    found = {}
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(entry.name)
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            try:
                exe = os.readlink(entry / "exe").removesuffix(" (deleted)")
                identified_by = "exe"
                is_engine = os.path.basename(exe) in names
            except OSError:
                identified_by = "argv0 (exe unreadable)"
                is_engine = os.path.basename(argv[0].decode("utf-8", "replace")) in names
            if not is_engine:
                continue
            if own_pgid is not None and os.getpgid(pid) == own_pgid:
                continue
        except (OSError, ValueError):
            continue
        found[pid] = {"cmdline": b" ".join(argv).decode("utf-8", "replace")[:300], "identified_by": identified_by}
    return found


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


def lock_seat(environ: Mapping[str, str] | None = None) -> str:
    """Who holds the lock: the seat (EMBEAD_AGENT), else the login name, as the rig's run.sh does."""
    environ = os.environ if environ is None else environ
    seat = environ.get("EMBEAD_AGENT") or ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", seat):
        try:
            import pwd
            seat = pwd.getpwuid(os.getuid()).pw_name
        except (ImportError, KeyError):
            seat = "unknown"
    return seat


class RuntimeLock:
    """The project's runtime lock, compatible with the rig's vmlock.sh.

    Acquisition is an atomic mkdir with an owner file carrying ``pid=N``.  A
    waiter breaks the lock only when that pid is gone and no project container
    runs, serialized by a ``breaking`` marker; otherwise it waits up to the
    given bound and fails.
    """

    def __init__(self, lock_dir: Path, owner_text: str, *, wait_s: float = 1200.0, poll_s: float = 1.0,
                 environ: Mapping[str, str] | None = None) -> None:
        self.lock_dir = lock_dir
        self.owner_text = owner_text
        self.wait_s = wait_s
        self.poll_s = poll_s
        self.environ = os.environ if environ is None else environ
        self.held = False
        self.inherited: str | None = None
        self.waited_s = 0.0

    def acquire(self) -> None:
        # The rig's with-vmlock.sh exports its owner line; when that line is the
        # lock's current owner, this process tree already holds the lock.
        enclosing = self.environ.get("VMLOCK_OWNER")
        if enclosing and self._owner() == enclosing:
            waited = self.environ.get("VMLOCK_WAITED") or "0"
            try:
                self.waited_s = float(waited)
            except ValueError:
                raise LaunchError(f"malformed VMLOCK_WAITED from the enclosing lock holder: {waited!r}") from None
            self.inherited = enclosing
            return
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

    @property
    def owner_line(self) -> str | None:
        """The owner line this run holds the lock under, or None."""
        if self.inherited is not None:
            return self.inherited
        return f"{self.owner_text} pid={os.getpid()}" if self.held else None

    def release(self) -> None:
        if not self.held:
            return  # never release an enclosing holder's lock
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
