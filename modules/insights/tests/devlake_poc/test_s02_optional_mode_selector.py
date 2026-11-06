"""Offline mode selector checks; draining and retained shared data are unverified."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[2] / "poc/devlake/scripts/optional_mode_scenario.py"
spec = importlib.util.spec_from_file_location("optional_mode_scenario", SCRIPT)
scenario = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scenario)


class OptionalModeSelectorTest(unittest.TestCase):
    def test_selects_exact_modes_deadline_and_retained_intent_checks(self):
        with mock.patch.object(scenario.subprocess, "run") as run:
            result = scenario.select("test_optional_deployment_modes")
        self.assertEqual(result, {
            "scenario": "test_optional_deployment_modes",
            "result": "offline-mode-intent-and-artifact-checks-passed",
            "worker_drain": "not_implemented", "optional_resource_cleanup": "not_implemented",
            "retained_shared_data": "not_verified", "gateway_broker_admission": "not_integrated",
            "live_transitions": "not_run",
        })
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            [scenario.sys.executable, "-m", "unittest", "-q", name] for name in scenario.CHECKS
        ])
        self.assertTrue(all(call.kwargs == {"cwd": scenario.ROOT, "check": True}
                            for call in run.call_args_list))
        self.assertFalse(any(argument in ("terraform", "aws", "psql")
                             for call in run.call_args_list for argument in call.args[0]))

    def test_drifted_pins_modes_deadline_or_checks_refuse_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "optional-mode.json"
            fixture = json.loads(scenario.FIXTURE.read_text())
            for change in (
                dict(pins={**fixture["pins"], "mode": "0" * 64}),
                dict(pins={**fixture["pins"], "intent_tests": "0" * 64}),
                dict(pins={**fixture["pins"], "artifact_finalizer": "0" * 64}),
                dict(pins={**fixture["pins"], "artifact_tests": "0" * 64}),
                dict(modes={**fixture["modes"], "connector-only": ["false", "true"]}),
                dict(poll_seconds=1), dict(deadline_seconds=3600),
                dict(checks=list(scenario.CHECKS[:-1])), dict(scope="live-mode-proof"),
                dict(unreviewed=True),
            ):
                with self.subTest(change=change), mock.patch.object(scenario.subprocess, "run") as run:
                    path.write_text(json.dumps({**fixture, **change}))
                    with self.assertRaisesRegex(ValueError, "pinned modes or checks"):
                        scenario.select("test_optional_deployment_modes", path)
                    run.assert_not_called()

    def test_wrong_selector_and_failed_check_do_not_claim_safe_disable(self):
        with mock.patch.object(scenario.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "unsupported optional-mode scenario"):
                scenario.select("test_restart")
            run.assert_not_called()
        with mock.patch.object(scenario.subprocess, "run",
                               side_effect=subprocess.CalledProcessError(1, "unittest")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                scenario.select("test_optional_deployment_modes")
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
