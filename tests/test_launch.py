"""Launcher pieces that do not need the engine: start script, layout, lock, teardown."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from synapse_commander import launch


def _write_fake_artifacts(root: Path, engine_body: str,
                          engine_binary: str = launch.HEADLESS_BINARY) -> launch.RuntimeArtifacts:
    engine_dir = root / "engine"
    engine_dir.mkdir()
    engine = engine_dir / engine_binary
    engine.write_text("#!/bin/sh\n" + engine_body)
    engine.chmod(engine.stat().st_mode | stat.S_IXUSR)
    game = root / "BAR.sdd"
    game.mkdir()
    (game / "modinfo.lua").write_text("return {}\n")
    (root / "data" / "maps").mkdir(parents=True)
    (root / "data" / "maps" / launch.MAP_FILE).write_bytes(b"map")
    return launch.RuntimeArtifacts.from_dirs(engine_dir=engine_dir, data_dir=root / "data", game_dir=game,
                                             engine_binary=engine_binary)


class StartScriptTests(unittest.TestCase):
    def test_single_player_no_cheats_with_run_id(self) -> None:
        script = launch.start_script(run_id="run-1")
        self.assertIn("GameType=Synapse Commander 0;", script)
        self.assertIn("synapse_run_id=run-1;", script)
        self.assertIn("Spectator=0;", script)
        self.assertIn("Team=0;", script)
        self.assertNotIn("debugcommands", script)
        self.assertIn("HostIP=127.0.0.1;", script)
        self.assertIn("HostPort=8452;", script)
        self.assertIn("HostPort=40000;", launch.start_script(run_id="r", host_port=40000))
        with self.assertRaises(launch.LaunchError):
            launch.start_script(run_id="r", host_port=80)
        self.assertNotIn("runtestsheadless", script)
        self.assertEqual(script.count("{"), script.count("}"))

    def test_join_is_opt_in_and_otherwise_loopback_only(self) -> None:
        default = launch.start_script(run_id="r")
        self.assertIn("HostIP=127.0.0.1;", default)
        self.assertNotIn("AllowAdditionalPlayers", default)
        joinable = launch.start_script(run_id="r", host_port=launch.JOIN_PORT, allow_join=True)
        self.assertIn("HostIP=0.0.0.0;", joinable)
        self.assertIn("HostPort=8452;", joinable)
        self.assertIn("AllowAdditionalPlayers=1;", joinable)
        self.assertEqual(joinable.count("{"), joinable.count("}"))

    def test_scenario_selection_is_a_modoption_and_absent_by_default(self) -> None:
        self.assertNotIn("synapse_scenario", launch.start_script(run_id="r"))
        script = launch.start_script(run_id="r", scenario="rescue_smoke")
        modoptions = script[script.index("[MODOPTIONS]"):script.index("[ALLYTEAM0]")]
        self.assertIn("\t\tsynapse_scenario=rescue_smoke;\n", modoptions)
        self.assertIn("synapse_run_id=r;", modoptions)
        for bad in ("", "a;b", "../x", "a b"):
            with self.assertRaises(launch.LaunchError):
                launch.start_script(run_id="r", scenario=bad)

    def test_rejects_unsafe_values(self) -> None:
        for run_id in ("", "a;b", "a/b", "x" * 65, "a\nb"):
            with self.assertRaises(launch.LaunchError):
                launch.start_script(run_id=run_id)
        with self.assertRaises(launch.LaunchError):
            launch.start_script(run_id="ok", player_name="x}y")


class LayoutTests(unittest.TestCase):
    def test_links_artifacts_and_refuses_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = _write_fake_artifacts(root, "exit 0\n")
            layout = launch.RunLayout(root / "out" / "r1")
            layout.create(artifacts)
            games = layout.write_dir / "games"
            self.assertEqual((games / launch.GAME_DIR_NAME).resolve(), artifacts.game_dir)
            self.assertEqual((games / launch.APP_ARCHIVE_DIR).resolve(), launch.APP_ARCHIVE)
            self.assertTrue((layout.write_dir / "maps" / launch.MAP_FILE).is_symlink())
            self.assertEqual(layout.mailbox_dir, layout.write_dir / "synapse")
            with self.assertRaises(launch.LaunchError):
                layout.create(artifacts)

    def test_identity_has_hashes_not_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = _write_fake_artifacts(Path(tmp), "exit 0\n")
            identity = artifacts.identity()
            self.assertEqual(len(identity["map"]["sha256"]), 64)
            self.assertNotIn(tmp, repr(identity))
            hashes = artifacts.archive_hashes()
            self.assertIn(f"{launch.APP_ARCHIVE_DIR}/modinfo.lua", hashes)


class EngineBinaryTests(unittest.TestCase):
    def test_headless_is_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = _write_fake_artifacts(Path(tmp), "exit 0\n")
            self.assertEqual(artifacts.engine.name, "spring-headless")
            self.assertEqual(artifacts.presentation, "headless")

    def test_rendered_client_from_the_same_engine_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = _write_fake_artifacts(Path(tmp), "exit 0\n", engine_binary="spring")
            self.assertEqual(artifacts.engine, artifacts.engine_dir / "spring")
            self.assertEqual(artifacts.presentation, "rendered")
            self.assertEqual(artifacts.identity()["engine"]["binary"], "spring")

    def test_only_known_engine_programs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("spring-dedicated", "../spring", "sh"):
                with self.assertRaises(launch.LaunchError):
                    launch.RuntimeArtifacts.from_dirs(engine_dir=Path(tmp), data_dir=Path(tmp), engine_binary=name)

    def test_engine_inherits_display_and_gl_settings_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = _write_fake_artifacts(Path(tmp), "env > seen-env.txt\n", engine_binary="spring")
            layout = launch.RunLayout(Path(tmp) / "out" / "r")
            layout.create(artifacts)
            layout.write_config(launch.start_script(run_id="r"), launch.engine_settings())
            env = {"DISPLAY": ":99", "LIBGL_ALWAYS_SOFTWARE": "1", "GALLIUM_DRIVER": "llvmpipe"}
            with mock.patch.dict(os.environ, env):
                os.environ.pop("LD_PRELOAD", None)
                engine = launch.EngineProcess(artifacts, layout)
                engine.start()
                engine.proc.wait(timeout=10)
                recorded = launch.render_environment()
            seen = dict(line.split("=", 1) for line in
                        (layout.write_dir / "seen-env.txt").read_text().splitlines() if "=" in line)
            for key, value in env.items():
                self.assertEqual(seen.get(key), value)
                self.assertEqual(recorded[key], value)
            self.assertNotIn("LD_PRELOAD", seen, "the launcher adds no preload of its own")

    def test_render_environment_keeps_only_render_variables(self) -> None:
        recorded = launch.render_environment({"DISPLAY": ":1", "MESA_GL_VERSION_OVERRIDE": "4.3",
                                              "HOME": "/x", "API_KEY": "secret"})
        self.assertEqual(recorded, {"DISPLAY": ":1", "MESA_GL_VERSION_OVERRIDE": "4.3"})


class TeardownTests(unittest.TestCase):
    def _engine(self, root: Path, body: str) -> launch.EngineProcess:
        artifacts = _write_fake_artifacts(root, body)
        layout = launch.RunLayout(root / "out" / "r")
        layout.create(artifacts)
        layout.write_config(launch.start_script(run_id="r"), launch.engine_settings())
        return launch.EngineProcess(artifacts, layout)

    def test_sigterm_stops_group_including_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(Path(tmp), "sleep 60 &\nexec sleep 60\n")
            engine.start()
            time.sleep(0.3)
            record = engine.stop(term_timeout_s=5)
            self.assertEqual(record["signal"], "SIGTERM")
            self.assertFalse(record["group_alive_after"])

    def test_escalates_to_sigkill_when_term_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(Path(tmp), "trap '' TERM\nwhile :; do sleep 0.1; done\n")
            engine.start()
            time.sleep(0.3)
            record = engine.stop(term_timeout_s=0.5, kill_timeout_s=5)
            self.assertEqual(record["signal"], "SIGKILL")
            self.assertFalse(record["group_alive_after"])
            self.assertLess(record["teardown_s"], 5)

    def test_a_group_of_only_zombies_is_not_alive(self) -> None:
        # The leader exits but is not reaped yet: signals still reach the group,
        # yet nothing in it runs, so teardown must not report it alive.
        proc = subprocess.Popen(["true"], start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = Path(f"/proc/{proc.pid}/stat").read_text()
                if state[state.rfind(")") + 2] == "Z":
                    break
                time.sleep(0.01)
            os.killpg(proc.pid, 0)  # still signalable while a zombie
            self.assertFalse(launch._group_alive(proc.pid))
        finally:
            proc.wait()
        self.assertFalse(launch._group_alive(proc.pid))

    def test_a_running_group_is_alive(self) -> None:
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            self.assertTrue(launch._group_alive(proc.pid))
        finally:
            proc.kill()
            proc.wait()

    def test_does_not_signal_unrelated_process(self) -> None:
        bystander = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                engine = self._engine(Path(tmp), "exec sleep 60\n")
                engine.start()
                engine.stop(term_timeout_s=5)
            self.assertIsNone(bystander.poll())
        finally:
            bystander.kill()
            bystander.wait()


class OtherEnginesTests(unittest.TestCase):
    """Engines are identified by their executable, not by the name they run under."""

    def _start(self, program: Path, *args: str) -> subprocess.Popen:
        proc = subprocess.Popen([str(program), *args], start_new_session=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        time.sleep(0.3)
        return proc

    def test_sees_a_foreign_engine_executable_but_not_own_group(self) -> None:
        # A unique stand-in name, so this test never looks like an engine to a
        # capture running concurrently on the same host.
        name = f"synapse-test-engine-{os.getpid()}"
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / name
            shutil.copy(shutil.which("sleep"), binary)  # a real executable file of that name
            proc = self._start(binary, "30")
            self.assertIn(proc.pid, launch.other_engines(binary=name))
            self.assertNotIn(proc.pid, launch.other_engines(own_pgid=proc.pid, binary=name))
            self.assertEqual(launch.engine_processes(binary=name)[proc.pid]["identified_by"], "exe")
            self.assertNotIn(proc.pid, launch.other_engines())

    def test_unreadable_executable_falls_back_to_argv0_and_says_so(self) -> None:
        name = f"synapse-test-engine-{os.getpid()}"
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / name
            link.symlink_to(shutil.which("sleep"))
            proc = self._start(link, "30")
            real_readlink = os.readlink

            def readlink(path, *args, **kwargs):
                if str(path) == f"/proc/{proc.pid}/exe":
                    raise PermissionError(path)
                return real_readlink(path, *args, **kwargs)

            with mock.patch("synapse_commander.launch.os.readlink", side_effect=readlink):
                found = launch.engine_processes(binary=name)
            self.assertEqual(found[proc.pid]["identified_by"], "argv0 (exe unreadable)")
            self.assertIn(name, found[proc.pid]["cmdline"])
            # With the executable readable, the same symlink is not an engine.
            self.assertNotIn(proc.pid, launch.engine_processes(binary=name))

    def test_ignores_a_symlink_or_script_merely_named_like_the_engine(self) -> None:
        # Such as this suite's own helpers: a concurrent test run must not look like an engine.
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "bin" / "spring-headless"
            link.parent.mkdir()
            link.symlink_to(shutil.which("sleep"))
            script = Path(tmp) / "spring-headless"
            script.write_text("#!/bin/sh\nexec sleep 30\n")
            script.chmod(0o755)
            linked = self._start(link, "30")
            scripted = self._start(script)
            engines = launch.other_engines()
            self.assertNotIn(linked.pid, engines)
            self.assertNotIn(scripted.pid, engines)


class LockTests(unittest.TestCase):
    def test_lock_seat_is_the_embead_agent_else_the_login_name(self) -> None:
        self.assertEqual(launch.lock_seat({"EMBEAD_AGENT": "nav-api"}), "nav-api")
        login = launch.lock_seat({})
        self.assertTrue(login and " " not in login)
        self.assertEqual(launch.lock_seat({"EMBEAD_AGENT": "bad seat; rm"}), login)

    def test_acquire_release_and_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp) / "locks" / "docker-vm"
            first = launch.RuntimeLock(lock_dir, "test first", wait_s=0.2, poll_s=0.05)
            first.acquire()
            self.assertIn(f"pid={os.getpid()}", (lock_dir / "owner").read_text())
            second = launch.RuntimeLock(lock_dir, "test second", wait_s=0.3, poll_s=0.05)
            with self.assertRaises(launch.LaunchError):
                second.acquire()  # the holder's pid (ours) is alive
            first.release()
            self.assertFalse(lock_dir.exists())
            with second:
                self.assertTrue(lock_dir.exists())
            self.assertFalse(lock_dir.exists())

    def test_breaks_lock_of_dead_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp) / "docker-vm"
            dead = subprocess.Popen(["true"])
            dead.wait()
            lock_dir.mkdir()
            (lock_dir / "owner").write_text(f"someone run pid={dead.pid} 2026-10-17T00:00:00Z\n")
            lock = launch.RuntimeLock(lock_dir, "test", wait_s=2, poll_s=0.05)
            lock.acquire()
            self.assertIn(f"pid={os.getpid()}", (lock_dir / "owner").read_text())
            lock.release()

    def test_enclosing_wrapper_holder_is_inherited_not_released(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp) / "docker-vm"
            lock_dir.mkdir()
            line = f"plt-loop final pid={os.getpid()} 2026-10-17T19:00:00Z"
            (lock_dir / "owner").write_text(line + "\n")
            lock = launch.RuntimeLock(lock_dir, "inner", wait_s=0.2, poll_s=0.05,
                                      environ={"VMLOCK_OWNER": line, "VMLOCK_WAITED": "3"})
            lock.acquire()
            self.assertEqual(lock.owner_line, line)
            self.assertEqual(lock.waited_s, 3.0)
            lock.release()
            self.assertEqual((lock_dir / "owner").read_text().strip(), line)

    def test_malformed_waited_is_a_launch_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp) / "docker-vm"
            lock_dir.mkdir()
            line = f"plt-loop final pid={os.getpid()}"
            (lock_dir / "owner").write_text(line + "\n")
            lock = launch.RuntimeLock(lock_dir, "inner", environ={"VMLOCK_OWNER": line, "VMLOCK_WAITED": "soon"})
            with self.assertRaises(launch.LaunchError):
                lock.acquire()
            self.assertIsNone(lock.owner_line)

    def test_foreign_owner_in_env_does_not_grant_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp) / "docker-vm"
            lock_dir.mkdir()
            (lock_dir / "owner").write_text(f"someone else pid={os.getpid()}\n")
            lock = launch.RuntimeLock(lock_dir, "inner", wait_s=0.2, poll_s=0.05,
                                      environ={"VMLOCK_OWNER": "plt-loop other pid=1"})
            with self.assertRaises(launch.LaunchError):
                lock.acquire()

    def test_fresh_empty_lock_is_not_broken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp) / "docker-vm"
            lock_dir.mkdir()  # another acquirer between mkdir and owner write
            with self.assertRaises(launch.LaunchError):
                launch.RuntimeLock(lock_dir, "test", wait_s=0.3, poll_s=0.05).acquire()
            self.assertTrue(lock_dir.exists())


if __name__ == "__main__":
    unittest.main()
