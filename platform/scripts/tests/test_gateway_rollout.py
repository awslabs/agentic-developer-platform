"""Run the gateway shell helpers against a controlled kubectl implementation."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]


class GatewayRolloutTests(unittest.TestCase):
    def run_helper(self, command, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mock = root / "kubectl"
            mock.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$CALLS"
if [[ "$1 $2" == "rollout status" ]]; then exit "${ROLLOUT_EXIT:-0}"; fi
if [[ "$*" == *spec.replicas* ]]; then
  printf '%s' "${REPLICAS-4}"
  exit "${READ_EXIT:-0}"
fi
exit "${DIAGNOSTIC_EXIT:-0}"
''')
            mock.chmod(0o755)
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", CALLS=str(root / "calls"))
            env.pop("ADP_GATEWAY_ROLLOUT_TIMEOUT_SECONDS", None)
            env.update(overrides)
            result = subprocess.run(
                ["bash", "-c", 'set -euo pipefail; source "$1"; ' + command,
                 "test", str(SCRIPTS / "gateway-rollout.sh")],
                env=env, text=True, capture_output=True,
            )
            calls = (root / "calls").read_text() if (root / "calls").exists() else ""
            return result, calls

    def test_upgrade_preserves_scaled_and_zero_replicas(self):
        for count in ("4", "12", "0"):
            result, calls = self.run_helper("gateway_deployment_replicas true", REPLICAS=count)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), count)
            self.assertIn("spec.replicas", calls)

    def test_install_keeps_baseline_without_read(self):
        result, calls = self.run_helper("gateway_deployment_replicas false")
        self.assertEqual(result.stdout.strip(), "2")
        self.assertEqual(calls, "")

    def test_missing_or_unreadable_replicas_fail_closed(self):
        for overrides in ({"READ_EXIT": "1"}, {"REPLICAS": ""}, {"REPLICAS": "null"}):
            result, _ = self.run_helper("gateway_deployment_replicas true", **overrides)
            self.assertNotEqual(result.returncode, 0)

    def test_success_uses_default_budget_without_diagnostics(self):
        result, calls = self.run_helper("wait_for_gateway_rollout")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--timeout=2400s", calls)
        self.assertEqual(len(calls.splitlines()), 1)

    def test_override_and_failure_diagnostics_preserve_original_exit(self):
        result, calls = self.run_helper(
            "wait_for_gateway_rollout", ADP_GATEWAY_ROLLOUT_TIMEOUT_SECONDS="1800",
            ROLLOUT_EXIT="7", DIAGNOSTIC_EXIT="1",
        )
        self.assertEqual(result.returncode, 7)
        self.assertIn("--timeout=1800s", calls)
        self.assertIn("get deployment,replicaset,pods", calls)
        self.assertIn("status.conditions", calls)
        self.assertIn("get events", calls)
        self.assertNotIn("get secrets", calls)

    def test_invalid_timeout_does_not_call_kubectl(self):
        for value in ("0", "-1", "forever", "1s", "9999999"):
            result, calls = self.run_helper(
                "wait_for_gateway_rollout", ADP_GATEWAY_ROLLOUT_TIMEOUT_SECONDS=value,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(calls, "")

    def test_rendered_manifest_uses_preserved_count(self):
        script = (SCRIPTS / "deploy-all.sh").read_text()
        start = script.index('  GATEWAY_DEPLOYMENT_REPLICAS=$(gateway_deployment_replicas')
        end = script.index('  echo "$DEPLOYMENT_APPLY_RESULT"', start)
        self.assertIn('gateway_deployment_replicas "$UPDATE_MODE"', script[start:end])
        self.assertIn('replicas: ${GATEWAY_DEPLOYMENT_REPLICAS}', script[start:end])
        self.assertNotIn('kubectl rollout status deployment/bedrockgateway', script)
