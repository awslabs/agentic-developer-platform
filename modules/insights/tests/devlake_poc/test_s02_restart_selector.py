"""Offline restart selector checks; optional-resource removal is not implemented."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[2] / "poc/devlake/scripts/restart_scenario.py"
spec = importlib.util.spec_from_file_location("restart_scenario", SCRIPT)
scenario = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scenario)


class RestartSelectorTest(unittest.TestCase):
    def test_restart_selects_exact_local_intent_and_artifact_checks(self):
        with mock.patch.object(scenario.subprocess, "run") as run:
            result = scenario.select("test_restart")
        self.assertEqual(result, {
            "scenario": "test_restart", "result": "offline-intent-and-artifact-checks-passed",
            "optional_resource_cleanup": "not_implemented", "state_key_loss": "not_run",
            "live_restart": "not_run",
        })
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            [scenario.sys.executable, "-m", "unittest", "-q", name] for name in scenario.CHECKS
        ])
        self.assertTrue(all(call.kwargs == {"cwd": scenario.ROOT, "check": True}
                            for call in run.call_args_list))

    def test_changed_ledger_finalizer_or_dropped_check_refuses_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "restart.json"
            fixture = json.loads(scenario.FIXTURE.read_text())
            for change in (dict(intent_ledger_sha256="0" * 64),
                           dict(artifact_finalizer_sha256="0" * 64),
                           dict(checks=list(scenario.CHECKS[:-1])),
                           dict(scope="live-cleanup")):
                with self.subTest(change=change), mock.patch.object(scenario.subprocess, "run") as run:
                    path.write_text(json.dumps({**fixture, **change}))
                    with self.assertRaisesRegex(ValueError, "pinned ledger or checks"):
                        scenario.select("test_restart", path)
                    run.assert_not_called()

    def test_wrong_selector_and_failed_check_do_not_claim_cleanup(self):
        with mock.patch.object(scenario.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "unsupported restart scenario"):
                scenario.select("test_install")
            run.assert_not_called()
        with mock.patch.object(scenario.subprocess, "run",
                               side_effect=subprocess.CalledProcessError(1, "unittest")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                scenario.select("test_restart")
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
