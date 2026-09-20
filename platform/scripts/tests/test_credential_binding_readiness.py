"""Exercise the public read-only shell gate and adversarial metric evidence."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

UTC = timezone.utc

SCRIPTS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("binding_readiness", SCRIPTS / "credential-binding-readiness.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def observations(now):
    start = datetime.combine(now.date() - timedelta(days=7), datetime.min.time(), tzinfo=UTC)
    return {
        name: {
            "Datapoints": [
                {"Timestamp": (start + timedelta(days=i)).isoformat(), "Sum": 10 if name.endswith(("Checked", "FromRegistry")) else 0}
                for i in range(7)
            ]
        }
        for name in gate.METRICS
    }


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 20, 12, tzinfo=UTC)
        self.evidence = observations(self.now)

    def errors(self):
        return gate.validate_observations(self.evidence, now=self.now, window_days=7)

    def test_observed_zero_drift_and_full_coverage_pass(self):
        self.assertEqual(self.errors(), [])

    def test_absent_metrics_and_empty_series_fail(self):
        self.evidence = {}
        self.assertTrue(self.errors())
        self.evidence = {name: {"Datapoints": []} for name in gate.METRICS}
        self.assertTrue(self.errors())

    def test_missing_day_or_partial_ingestion_fail(self):
        for name in gate.METRICS:
            with self.subTest(name=name):
                self.evidence = observations(self.now)
                self.evidence[name]["Datapoints"].pop(2)
                self.assertTrue(self.errors())

    def test_nonzero_drift_and_fallback_fail(self):
        for name in gate.METRICS[2:]:
            with self.subTest(name=name):
                self.evidence = observations(self.now)
                self.evidence[name]["Datapoints"][1]["Sum"] = 1
                self.assertTrue(self.errors())

    def test_no_calls_and_inconsistent_registry_denominator_fail(self):
        for count in (0, 9, 11):
            with self.subTest(count=count):
                self.evidence = observations(self.now)
                self.evidence[gate.METRICS[0]]["Datapoints"][0]["Sum"] = count
                self.assertTrue(self.errors())

    def test_malformed_values_and_duplicates_fail(self):
        for value in (None, True, "0", -1, 0.5, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.evidence = observations(self.now)
                self.evidence[gate.METRICS[0]]["Datapoints"][0]["Sum"] = value
                self.assertTrue(self.errors())
        self.evidence = observations(self.now)
        self.evidence[gate.METRICS[0]]["Datapoints"].append(self.evidence[gate.METRICS[0]]["Datapoints"][0])
        self.assertTrue(self.errors())

    def test_invalid_future_and_outside_window_timestamps_fail(self):
        for value in ("not-a-time", "2026-09-13", "2026-09-21T00:00:00Z", "2026-09-01T00:00:00Z"):
            with self.subTest(value=value):
                self.evidence = observations(self.now)
                self.evidence[gate.METRICS[0]]["Datapoints"][0]["Timestamp"] = value
                self.assertTrue(self.errors())

    def test_current_partial_day_drift_is_not_ignored(self):
        for name in gate.METRICS:
            self.evidence[name]["Datapoints"].append({"Timestamp": self.now.isoformat(), "Sum": 1})
        self.assertTrue(self.errors())


class ShellTests(unittest.TestCase):
    def run_gate(self, *, metrics=None, nightly=None, credentials="true", aws_failure=False, extra=()):
        now = datetime.now(UTC)
        if metrics is None:
            metrics = observations(now)
        if nightly is None:
            nightly = [{"status": "completed", "conclusion": "success", "createdAt": now.isoformat(), "databaseId": 123, "headSha": "a" * 40}]
        with tempfile.TemporaryDirectory(prefix="binding gate ") as temporary:
            root = Path(temporary)
            (root / "metrics.json").write_text(json.dumps(metrics))
            (root / "nightly.json").write_text(json.dumps(nightly))
            (root / "aws").write_text("""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
if 'get-metric-statistics' in args:
 if os.environ['GATE_AWS_FAIL']=='true': sys.exit(1)
 value=json.loads((Path(os.environ['GATE_FIXTURE'])/'metrics.json').read_text())[args[args.index('--metric-name')+1]]
else: value={'Parameter': {'Value': os.environ['GATE_CREDENTIALS']}}
print(json.dumps(value))
""")
            (root / "gh").write_text("""#!/usr/bin/env python3
import os
from pathlib import Path
print((Path(os.environ['GATE_FIXTURE'])/'nightly.json').read_text())
""")
            for name in ("aws", "gh"):
                (root / name).chmod(0o755)
            env = {
                **os.environ,
                "PATH": str(root) + os.pathsep + os.environ["PATH"],
                "GATE_FIXTURE": str(root),
                "GATE_CREDENTIALS": credentials,
                "GATE_AWS_FAIL": str(aws_failure).lower(),
            }
            return subprocess.run(["bash", str(SCRIPTS / "flip-gate-check.sh"), *extra], env=env, capture_output=True, text=True, timeout=30)

    def test_public_wrapper_passes_complete_evidence(self):
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["ready"])

    def test_public_wrapper_refuses_empty_data_and_transport_failure(self):
        for options in ({"metrics": {name: {"Datapoints": []} for name in gate.METRICS}}, {"aws_failure": True}):
            with self.subTest(options=options):
                result = self.run_gate(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(json.loads(result.stdout)["ready"])

    def test_nightly_missing_failed_pending_old_or_malformed_fails(self):
        now = datetime.now(UTC)
        for nightly in (
            [],
            [{}],
            [{"status": "completed", "conclusion": "failure", "createdAt": now.isoformat()}],
            [{"status": "in_progress", "conclusion": None, "createdAt": now.isoformat()}],
            [{"status": "completed", "conclusion": "success", "createdAt": (now - timedelta(days=2)).isoformat()}],
        ):
            with self.subTest(nightly=nightly):
                self.assertNotEqual(self.run_gate(nightly=nightly).returncode, 0)

    def test_disabled_sandbox_other_environment_and_short_window_fail(self):
        for options in ({"credentials": "false"}, {"extra": ("--environment", "staging")}, {"extra": ("--window-days", "1")}):
            with self.subTest(options=options):
                self.assertNotEqual(self.run_gate(**options).returncode, 0)

    def test_reads_only_and_scopes_latest_scheduled_main_run(self):
        commands = []

        def read(command):
            commands.append(command)
            if "get-metric-statistics" in command:
                return observations(datetime.now(UTC))[command[command.index("--metric-name") + 1]]
            if command[0] == "gh":
                return [{"status": "completed", "conclusion": "success", "createdAt": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}]
            return {"Parameter": {"Value": "true"}}

        with patch.object(gate, "read_json", read), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.main([]), 0)
        self.assertEqual(len(commands), 6)
        nightly = next(c for c in commands if c[0] == "gh")
        self.assertEqual(nightly[nightly.index("--event") + 1], "schedule")
        self.assertEqual(nightly[nightly.index("--branch") + 1], "main")
        self.assertFalse(any("put-parameter" in c or "workflow" in c for c in commands))


if __name__ == "__main__":
    unittest.main()
