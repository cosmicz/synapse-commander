"""CLI argument validation and dispatch without launching an engine."""

from contextlib import redirect_stderr, redirect_stdout
import io
import unittest
from unittest import mock

from synapse_commander import cli


class RunCommandTests(unittest.TestCase):
    def arguments(self, command, *extra):
        return [command, "--engine-dir", "engine", "--data-dir", "data",
                "--out", "evidence", "--rig-commit", "a" * 40,
                "--run-id", "cli-test", *extra]

    def test_smoke_rejects_scenario_before_config_or_dispatch(self):
        for option in (["--scenario", "rescue_smoke"],
                       ["--scenario=rescue_baseline"], ["--scenario", ""]):
            with self.subTest(option=option), \
                    mock.patch.object(cli, "SmokeConfig") as config, \
                    mock.patch.object(cli, "run_smoke", return_value={
                        "run_id": "cli-test", "verdict": "PASS"}) as run, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as raised:
                    cli.main(self.arguments("run-smoke", *option))
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("run-smoke", stderr.getvalue())
                self.assertIn("--scenario", stderr.getvalue())
                self.assertIn("run-provision", stderr.getvalue())
                config.assert_not_called()
                run.assert_not_called()

    def test_plain_smoke_keeps_loop0_dispatch(self):
        with mock.patch.object(cli, "run_smoke", return_value={
                "run_id": "cli-test", "verdict": "PASS"}) as run, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(self.arguments("run-smoke")), 0)
        run.assert_called_once()
        config = run.call_args.args[0]
        self.assertEqual(config.mode, "scripted")
        self.assertIsNone(config.scenario)

    def test_other_commands_keep_scenario_selection(self):
        cases = [
            ("run-provision", [], "provision", "rescue_smoke"),
            ("run-provision", ["--scenario", "rescue_baseline"], "provision", "rescue_baseline"),
            ("run-view", [], "view", "view_smoke"),
            ("run-agent", ["--scenario", "rescue_smoke"], "agent", "rescue_smoke"),
        ]
        for command, options, mode, scenario in cases:
            with self.subTest(command=command, options=options), \
                    mock.patch.object(cli, "run_smoke", return_value={
                        "run_id": "cli-test", "verdict": "PASS"}) as run, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(self.arguments(command, *options)), 0)
                run.assert_called_once()
                config = run.call_args.args[0]
                self.assertEqual(config.mode, mode)
                self.assertEqual(config.scenario, scenario)


if __name__ == "__main__":
    unittest.main()
