"""Offline IAM-renewal selector checks; a live database is not exercised."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[2] / "poc/devlake/scripts/iam_renewal_scenario.py"
spec = importlib.util.spec_from_file_location("iam_renewal_scenario", SCRIPT)
scenario = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scenario)


class IAMRenewalSelectorTest(unittest.TestCase):
    def test_selects_exact_controlled_age_and_connection_checks(self):
        with mock.patch.object(scenario.subprocess, "run") as run:
            result = scenario.select("test_iam_renewal")
        self.assertEqual(result, {
            "scenario": "test_iam_renewal", "result": "offline-client-checks-passed",
            "worker_database_client": "not_implemented",
            "expired_static_token_server_rejection": "not_run",
            "live_aged_backend_connections": "not_run",
        })
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            [scenario.sys.executable, "-m", "unittest", "-q", name] for name in scenario.CHECKS
        ])
        self.assertTrue(all(call.kwargs == {"cwd": scenario.ROOT, "check": True}
                            for call in run.call_args_list))
        self.assertFalse(any("psql" in argument or "aws" in argument
                             for call in run.call_args_list for argument in call.args[0]))

    def test_drifted_clients_suite_controls_or_checks_refuse_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "iam-renewal.json"
            fixture = json.loads(scenario.FIXTURE.read_text())
            for change in (dict(receipt_reader_sha256="0" * 64),
                           dict(migration_runner_sha256="0" * 64),
                           dict(test_suite_sha256="0" * 64),
                           dict(controlled_age_seconds=900),
                           dict(physical_connections=1),
                           dict(checks=list(scenario.CHECKS[:-1])),
                           dict(scope="live-proof")):
                with self.subTest(change=change), mock.patch.object(scenario.subprocess, "run") as run:
                    path.write_text(json.dumps({**fixture, **change}))
                    with self.assertRaisesRegex(ValueError, "pinned clients or checks"):
                        scenario.select("test_iam_renewal", path)
                    run.assert_not_called()

    def test_wrong_selector_and_failed_check_cannot_report_success(self):
        with mock.patch.object(scenario.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "unsupported IAM renewal scenario"):
                scenario.select("test_restart")
            run.assert_not_called()
        with mock.patch.object(scenario.subprocess, "run",
                               side_effect=subprocess.CalledProcessError(1, "unittest")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                scenario.select("test_iam_renewal")
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
