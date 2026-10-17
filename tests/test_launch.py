"""Launcher pieces that do not need the engine: start script, layout, lock, teardown."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import unittest

from synapse_commander import launch


def _write_fake_artifacts(root: Path, engine_body: str) -> launch.RuntimeArtifacts:
    engine_dir = root / "engine"
    engine_dir.mkdir()
    engine = engine_dir / "spring-headless"
    engine.write_text("#!/bin/sh\n" + engine_body)
    engine.chmod(engine.stat().st_mode | stat.S_IXUSR)
    game = root / "BAR.sdd"
    game.mkdir()
    (game / "modinfo.lua").write_text("return {}\n")
    (root / "data" / "maps").mkdir(parents=True)
    (root / "data" / "maps" / launch.MAP_FILE).write_bytes(b"map")
    return launch.RuntimeArtifacts.from_dirs(engine_dir=engine_dir, data_dir=root / "data", game_dir=game)


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
    def test_sees_foreign_engine_but_not_own_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            named = Path(tmp) / "bin" / "spring-headless"
            named.parent.mkdir()
            named.symlink_to(shutil.which("sleep"))
            proc = subprocess.Popen([str(named), "30"], start_new_session=True)
            try:
                time.sleep(0.2)
                self.assertIn(proc.pid, launch.other_engines())
                self.assertNotIn(proc.pid, launch.other_engines(own_pgid=proc.pid))
            finally:
                proc.kill()
                proc.wait()


class LockTests(unittest.TestCase):
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

    def test_fresh_empty_lock_is_not_broken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp) / "docker-vm"
            lock_dir.mkdir()  # another acquirer between mkdir and owner write
            with self.assertRaises(launch.LaunchError):
                launch.RuntimeLock(lock_dir, "test", wait_s=0.3, poll_s=0.05).acquire()
            self.assertTrue(lock_dir.exists())


if __name__ == "__main__":
    unittest.main()
