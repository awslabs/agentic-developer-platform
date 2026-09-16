"""Execute scope and finalization shell paths with isolated service doubles."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("network", ROOT / "platform/scripts/upgrade-network.py")
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)


class WorkflowTests(unittest.TestCase):
    def test_scope_updates_only_installed_modules(self):
        scenarios = [
            ({}, ["true", "true", "false", "false"]),
            ({"GATEWAY_ONLY": "true"}, ["true", "false", "false", "false"]),
            ({"UPGRADE_MODULES": "platform,gateway,webhook-ingress,agent-factory,agent-context"}, ["true"] * 4),
            ({"UPGRADE_MODULES": "platform,gateway,agent-context", "SKIP_AGENT_CONTEXT": "true"}, ["true", "false", "false", "false"]),
        ]
        for flags, expected in scenarios:
            env = dict(os.environ, UPDATE_MODE="true", UPGRADE_MODULES="platform,gateway,webhook-ingress")
            env.update(flags)
            command = 'set -euo pipefail; fail() { exit 1; }; source "$1"; resolve_deploy_scope; printf "%s\\n" "$DEPLOY_GATEWAY" "$DEPLOY_WEBHOOK" "$DEPLOY_FACTORY" "$DEPLOY_AGENT_CONTEXT"'
            result = subprocess.run(["bash", "-c", command, "test", str(ROOT / "platform/scripts/upgrade-scope.sh")], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), expected)

    def test_explicit_missing_module_refuses(self):
        env = dict(os.environ, UPDATE_MODE="true", UPGRADE_MODULES="platform,gateway", AGENT_FACTORY_ONLY="true")
        result = subprocess.run(["bash", "-c", 'set -euo pipefail; fail() { exit 7; }; source "$1"; resolve_deploy_scope', "test",
                                 str(ROOT / "platform/scripts/upgrade-scope.sh")], env=env)
        self.assertEqual(result.returncode, 7)

    def test_collector_requires_dns_both_protocols_and_https(self):
        policy = {"spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "adot-collector"}}, "policyTypes": ["Egress"],
                           "egress": [{"ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}, {"port": 443, "protocol": "TCP"}]}]}}
        self.assertTrue(network.collector_allowed(policy))
        policy["spec"]["egress"][0]["ports"].pop()
        self.assertFalse(network.collector_allowed(policy))
        policy["spec"]["egress"][0]["ports"].append({"port": 443, "protocol": "TCP"})
        policy["spec"]["egress"][0]["to"] = [{"podSelector": {}}]
        self.assertFalse(network.collector_allowed(policy))

    def finalize(self, audit_fail=False):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        start = source.index("# Finalize after all installed modules")
        block = source[start:source.index("# Summary\n", start)]
        prefix = r'''set -euo pipefail
step() { :; }
python3() {
  if [ "$1" = -c ]; then command python3 "$@"; return; fi
  echo "python $*" >> "$CALLS"
  if [ "${2:-}" = audit ] && [ "$AUDIT_FAIL" = true ]; then return 1; fi
}
terraform_update_apply() { echo "terraform $1 check=${UPGRADE_CHECK_ONLY:-false}" >> "$CALLS"; }
gateway_alb_vars() { GATEWAY_ALB_ARGS=(-var preserved-albs); }
bash() { echo "frontend $*" >> "$CALLS"; }
aws() { echo frontend.example.test; }
curl() { echo '{"status":"healthy"}'; }
'''
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / "calls"
            env = dict(os.environ, ROOT_DIR=str(ROOT), SCRIPT_DIR=str(ROOT / "platform/scripts"),
                       UPDATE_MODE="true", DEPLOY_GATEWAY="true", SKIP_FRONTEND="false", UPGRADE_RUN_DIR=tmp,
                       ENVIRONMENT="test", AWS_REGION="us-east-1", CALLS=str(calls), AUDIT_FAIL=str(audit_fail).lower())
            result = subprocess.run(["bash", "-c", prefix + block], env=env, text=True, capture_output=True)
            return result, calls.read_text().splitlines()

    def test_audit_precedes_activation_and_convergence_precedes_verification(self):
        result, calls = self.finalize()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("upgrade-network.py audit", calls[0])
        self.assertEqual(calls[1:4], ["terraform platform check=false", "terraform gateway-final check=false", "terraform gateway-final check=true"])
        self.assertIn("deploy-frontend.sh", calls[4])
        self.assertIn("upgrade-state.py verify", calls[5])

    def test_failed_network_audit_stops_before_platform_apply(self):
        result, calls = self.finalize(audit_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(line.startswith("terraform") for line in calls))


if __name__ == "__main__":
    unittest.main()
