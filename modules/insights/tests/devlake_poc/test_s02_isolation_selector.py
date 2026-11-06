"""Offline selector checks; deployed identity denials are a separate acceptance task."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[2] / "poc/devlake/scripts/isolation_scenario.py"
spec = importlib.util.spec_from_file_location("isolation_scenario", SCRIPT)
scenario = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scenario)


class IsolationSelectorTest(unittest.TestCase):
    def test_isolation_selects_exact_scoped_role_tls_and_mocked_terraform_checks(self):
        with mock.patch.object(scenario.shutil, "which", return_value="/usr/bin/openssl"), \
                mock.patch.object(scenario.subprocess, "run") as run:
            result = scenario.select("test_isolation")
        self.assertEqual(result, {
            "scenario": "test_isolation", "result": "offline-contract-checks-passed",
            "sql_denials": "not_run", "storage_denials": "not_run", "libpq_tls_denials": "not_run",
        })
        self.assertEqual([call.args[0] for call in run.call_args_list[:3]], [
            [scenario.sys.executable, "-m", "unittest", "-q", name] for name in scenario.CHECKS
        ])
        self.assertEqual([call.args[0][2] for call in run.call_args_list[3:]], ["init", "test"])
        self.assertIn("-backend=false", run.call_args_list[3].args[0])
        self.assertTrue(all(call.kwargs["check"] is True and call.kwargs["cwd"] == scenario.ROOT
                            for call in run.call_args_list))
        self.assertFalse(any("psql" in argument or "aws" in argument
                             for call in run.call_args_list for argument in call.args[0]))

    def test_drifted_schema_or_deleted_negative_check_refuses_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "isolation.json"
            fixture = json.loads(scenario.FIXTURE.read_text())
            for change in (dict(schema_manifest_sha256="0" * 64),
                           dict(checks=list(scenario.CHECKS[:-1])),
                           dict(scope="deployed-proof")):
                with self.subTest(change=change), mock.patch.object(scenario.subprocess, "run") as run:
                    path.write_text(json.dumps({**fixture, **change}))
                    with self.assertRaisesRegex(ValueError, "pinned schema or checks"):
                        scenario.select("test_isolation", path)
                    run.assert_not_called()

    def test_unknown_selector_and_missing_tls_tool_fail_closed(self):
        with mock.patch.object(scenario.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "unsupported isolation scenario"):
                scenario.select("test_install")
            with mock.patch.object(scenario.shutil, "which", return_value=None):
                with self.assertRaisesRegex(ValueError, "requires OpenSSL"):
                    scenario.select("test_isolation")
            run.assert_not_called()

    def test_failed_scoped_check_never_reports_success_or_runs_terraform(self):
        with mock.patch.object(scenario.shutil, "which", return_value="/usr/bin/openssl"), \
                mock.patch.object(scenario.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "unittest")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                scenario.select("test_isolation")
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
