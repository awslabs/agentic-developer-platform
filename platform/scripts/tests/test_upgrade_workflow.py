"""Execute scope and finalization shell paths with isolated service doubles."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("network", ROOT / "platform/scripts/upgrade-network.py")
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)


class WorkflowTests(unittest.TestCase):
    def test_gateway_retry_does_not_restart_unchanged_pods(self):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        start = source.index('  if [ "$UPDATE_MODE" = true ]; then', source.index('DEPLOYMENT_APPLY_RESULT='))
        block = source[start:source.index('    # Post-rollout health check', start)] + '  fi\n'
        stub = '''set -euo pipefail
fail() { echo "$1" >&2; exit 1; }
kubectl() {
  case "$1 $2" in
    'get deployment/bedrockgateway') printf '%s' "$GATEWAY_IMAGE" ;;
    'rollout restart') echo restarted ;;
    'rollout status') echo ready ;;
    'set image') echo set-image ;;
  esac
}
'''
        for secret, configmap, deployment, restart in (
                ('unchanged', 'unchanged', 'unchanged', False),
                ('unchanged', 'configured', 'unchanged', True),
                ('configured', 'unchanged', 'configured', False)):
            with self.subTest(secret=secret, configmap=configmap, deployment=deployment):
                env = dict(os.environ, UPDATE_MODE='true', GATEWAY_IMAGE='target-image',
                           SECRET_APPLY_RESULT=secret, CONFIGMAP_APPLY_RESULT=configmap,
                           DEPLOYMENT_APPLY_RESULT=deployment)
                result = subprocess.run(['bash', '-c', stub + block], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual('restarted' in result.stdout, restart)
                self.assertIn('ready', result.stdout)

    def test_upgrade_tfvars_reject_foreign_account_before_plan(self):
        helper = ROOT / "platform/scripts/terraform-update.sh"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gateway.tfvars.json"
            command = 'source "$1"; terraform_update_var_file "$2" "" 608380991969'
            path.write_text('{"queue_url":"https://sqs.us-east-1.amazonaws.com/879318057152/queue"}')
            rejected = subprocess.run(["bash", "-c", command, "test", str(helper), str(path)],
                                      text=True, capture_output=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("different AWS account", rejected.stderr)
            path.write_text('{"queue_url":"https://sqs.us-east-1.amazonaws.com/608380991969/queue"}')
            accepted = subprocess.run(["bash", "-c", command, "test", str(helper), str(path)],
                                      text=True, capture_output=True)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_tick_second_pass_runs_only_after_in_scope_gateway_and_refresh(self):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        start = source.index('if [ "$DEPLOY_WEBHOOK" = true ]; then', source.index('# Step 9/11:'))
        block = source[start:source.index('\nrefresh_credentials\n', start)]
        prefix = '''set -euo pipefail
step() { :; }
ok() { :; }
bash() { echo webhook; }
refresh_credentials() { echo refreshed; }
terraform_update_apply() { echo "terraform $1"; }
'''
        for gateway, webhook in ((True, True), (False, True), (True, False)):
            with self.subTest(gateway=gateway, webhook=webhook):
                env = dict(os.environ, ROOT_DIR=str(ROOT), DEPLOY_GATEWAY=str(gateway).lower(),
                           DEPLOY_WEBHOOK=str(webhook).lower(), UPDATE_MODE="true", CONFIRM_DESTRUCTIVE="false",
                           SKIP_WEBHOOK_INGRESS="false", ENVIRONMENT="dev", AWS_REGION="us-east-1",
                           GATEWAY_UPDATE_VAR_FILE="/tmp/gateway.tfvars.json")
                result = subprocess.run(["bash", "-c", prefix + block], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = ["webhook"] if webhook else []
                if webhook and gateway:
                    expected += ["refreshed", "terraform gateway-worker-authority"]
                self.assertEqual(result.stdout.splitlines(), expected)

    def test_full_upgrade_includes_required_factory_and_discovers_optional_modules(self):
        scenarios = [
            ({}, ["true", "true", "true", "false"]),
            ({"GATEWAY_ONLY": "true"}, ["true", "false", "false", "false"]),
            ({"UPGRADE_MODULES": "platform,gateway,webhook-ingress,agent-factory,agent-context"}, ["true"] * 4),
            ({"UPGRADE_MODULES": "platform,gateway,agent-context", "SKIP_AGENT_CONTEXT": "true"}, ["true", "false", "true", "false"]),
            ({"AGENT_FACTORY_ONLY": "true"}, ["false", "true", "true", "false"]),
        ]
        for flags, expected in scenarios:
            env = dict(os.environ, UPDATE_MODE="true", UPGRADE_MODULES="platform,gateway,webhook-ingress")
            env.update(flags)
            command = 'set -euo pipefail; fail() { exit 1; }; source "$1"; resolve_deploy_scope; printf "%s\\n" "$DEPLOY_GATEWAY" "$DEPLOY_WEBHOOK" "$DEPLOY_FACTORY" "$DEPLOY_AGENT_CONTEXT"'
            result = subprocess.run(["bash", "-c", command, "test", str(ROOT / "platform/scripts/upgrade-scope.sh")], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), expected)

    def test_explicit_missing_optional_module_refuses(self):
        env = dict(os.environ, UPDATE_MODE="true", UPGRADE_MODULES="platform,gateway", AGENT_CONTEXT_ONLY="true")
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

    def finalize(self, audit_fail=False, ci_mode=False, deferred=False, engine_fail=False):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        start = source.index("# Finalize after all installed modules")
        block = source[start:source.index("# Summary\n", start)]
        prefix = r'''set -euo pipefail
step() { :; }
fail() { echo "$*" >&2; exit 1; }
python3() {
  if [ "$1" = -c ]; then command python3 "$@"; return; fi
  echo "python $*" >> "$CALLS"
  if [[ "$1" = */sync-gateway-engine.py ]] && [ "$ENGINE_FAIL" = true ]; then return 1; fi
  if [ "${2:-}" = audit ] && [ "$AUDIT_FAIL" = true ]; then return 1; fi
}
terraform_update_apply() {
  echo "terraform $1 check=${UPGRADE_CHECK_ONLY:-false}" >> "$CALLS"
  if [ "$1" = gateway-final ]; then
    [[ " $* " = *" -var orchestration_tick_image_tag=$IMAGE_TAG "* ]] || return 9
  fi
}
gateway_alb_vars() { GATEWAY_ALB_ARGS=(-var preserved-albs); }
bash() { echo "frontend $*" >> "$CALLS"; }
aws() { echo frontend.example.test; }
curl() { echo '{"status":"healthy"}'; }
'''
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / "calls"
            env = dict(os.environ, ROOT_DIR=str(ROOT), SCRIPT_DIR=str(ROOT / "platform/scripts"),
                       UPDATE_MODE="true", DEPLOY_GATEWAY="true", DEPLOY_FACTORY="true", SKIP_FRONTEND="false", UPGRADE_RUN_DIR=tmp,
                       GATEWAY_UPDATE_VAR_FILE="/tmp/gateway.tfvars.json", IMAGE_TAG="a" * 40,
                       GATEWAY_IMAGE="customer-gateway@sha256:" + "b" * 64, ACCOUNT_ID="925091290508",
                       ENGINE_FAIL=str(engine_fail).lower(),
                       ENVIRONMENT="test", AWS_REGION="us-east-1", CALLS=str(calls), AUDIT_FAIL=str(audit_fail).lower(),
                       CI_MODE=str(ci_mode).lower(), ADP_BEDROCK_VERIFY_DEFERRED=str(deferred).lower())
            result = subprocess.run(["bash", "-c", prefix + block], env=env, text=True, capture_output=True)
            return result, calls.read_text().splitlines()

    def test_audit_precedes_activation_and_convergence_precedes_verification(self):
        result, calls = self.finalize()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("upgrade-network.py audit", calls[0])
        self.assertEqual(calls[1:4], ["terraform platform check=false", "terraform gateway-final check=false", "terraform gateway-final check=true"])
        self.assertIn("sync-gateway-engine.py --verify-only --image customer-gateway@sha256:", calls[4])
        self.assertIn("deploy-frontend.sh", calls[5])
        self.assertIn("upgrade-state.py verify", calls[6])
        self.assertIn("--require-module agent-factory", calls[6])
        self.assertIn("enable-bedrock-models.sh --verify", calls[7])

    def test_final_engine_mismatch_prevents_frontend_and_success(self):
        result, calls = self.finalize(engine_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("deploy-frontend.sh" in line or "upgrade-state.py verify" in line for line in calls))

    def test_default_model_invocations_skip_ci_and_wrapper_deferred_checks(self):
        for ci_mode, deferred in ((True, False), (False, True)):
            with self.subTest(ci_mode=ci_mode, deferred=deferred):
                result, calls = self.finalize(ci_mode=ci_mode, deferred=deferred)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(any("enable-bedrock-models.sh" in line for line in calls))

    def test_factory_must_be_ready_on_the_intended_image(self):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        start = source.index("  kubectl wait --for=condition=Ready scaledjob/agent-gateway-worker")
        block = source[start:source.index('\n  if [ "$UPDATE_MODE"', start)]
        prefix = '''set -euo pipefail
fail() { echo "$*" >&2; exit 1; }
kubectl() {
  if [ "$1" = wait ]; then return "$WAIT_EXIT"; fi
  echo "$LIVE_IMAGE"
}
'''
        for wait_exit, image, expected in ((0, "release:sha", True), (1, "release:sha", False), (0, "release:old", False)):
            with self.subTest(wait_exit=wait_exit, image=image):
                env = dict(os.environ, WAIT_EXIT=str(wait_exit), LIVE_IMAGE=image, AGENT_IMAGE="release:sha")
                result = subprocess.run(["bash", "-c", prefix + block], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, expected, result.stderr)

    def test_failed_network_audit_stops_before_platform_apply(self):
        result, calls = self.finalize(audit_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(line.startswith("terraform") for line in calls))

    def test_context_application_deploy_cannot_apply_terraform_again(self):
        source = (ROOT / "modules/agent-context/deploy.sh").read_text()
        start = source.index("# Deploy Terraform infrastructure")
        block = source[start:source.index("# Deploy Ingestion Refresh CronJob", start)]
        prefix = '''set -euo pipefail
terraform() { echo "$*" >> "$CALLS"; [ "$1" = output ] || return 97; echo existing-output; }
'''
        for lean in ("true", "false"):
            with tempfile.TemporaryDirectory() as tmp:
                calls = Path(tmp) / "calls"
                env = dict(os.environ, SCRIPT_DIR=str(ROOT / "modules/agent-context"), SKIP_TERRAFORM="true",
                           PERSONAL_CONTEXT_ONLY=lean, GRAPHRAG_ENABLED="true", CALLS=str(calls))
                result = subprocess.run(["bash", "-c", prefix + block], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(all(line.startswith("output ") for line in calls.read_text().splitlines()))

    def test_worker_replacement_preserves_jobs_and_refuses_wrong_context(self):
        source = (ROOT / "modules/agent-factory/webhook-ingress/infra/scaledjob.tf").read_text()
        source = source[source.index('resource "null_resource" "keda_scaledjob"'):]
        command = re.search(r'when\s*=\s*destroy.*?command\s*=\s*<<-CMD\n(.*?)\n\s*CMD', source, re.S)[1]
        command = command.replace("$${", "${")
        for key, value in (("cluster_name", "target-cluster"), ("cluster_region", "target-region"), ("namespace", "target-namespace")):
            command = command.replace("${self.triggers." + key + "}", value)
        prefix = '''aws() { echo "aws $*" >> "$CALLS"; [ "$AWS_FAIL" = false ]; }
kubectl() { echo "kubectl $*" >> "$CALLS"; }
'''
        for aws_fail in ("false", "true"):
            with tempfile.TemporaryDirectory() as tmp:
                calls = Path(tmp) / "calls"
                env = dict(os.environ, CALLS=str(calls), AWS_FAIL=aws_fail, KUBECONFIG=tmp + "/caller-config")
                result = subprocess.run(["bash", "-c", prefix + command], env=env, text=True, capture_output=True)
                recorded = calls.read_text()
                self.assertIn("--name target-cluster --region target-region --kubeconfig " + env["KUBECONFIG"], recorded)
                if aws_fail == "true":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("kubectl", recorded)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("--cascade=orphan", recorded)

    def test_already_managed_log_group_is_never_imported_again(self):
        source = (ROOT / "modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh").read_text()
        start = source.index("import_bootstrap_log_group() {")
        function = source[start:source.index('\n}\n', start) + 3]
        prefix = '''set -euo pipefail
aws() { echo "$BOOTSTRAP_LOG_GROUP"; }
ok() { :; }
warn() { :; }
fail() { echo "$*" >&2; exit 1; }
terraform() {
  if [ "$1 $2" = "state list" ]; then
    if [ "$STATE_FAIL" = true ]; then return 1; fi
    echo aws_cloudwatch_log_group.agent_bootstrap
    # More than a pipe buffer: the old grep -q pipeline could close the pipe
    # early and turn a successful state read into a failed import check.
    command python3 -c 'print("aws_example.resource\\n" * 10000)'
  else
    echo "UNEXPECTED IMPORT" >&2
    exit 99
  fi
}
'''
        for state_fail in ("false", "true"):
            env = dict(os.environ, BOOTSTRAP_LOG_GROUP="/adp/test/bootstrap", AWS_REGION="us-east-1", STATE_FAIL=state_fail)
            result = subprocess.run(["bash", "-c", prefix + function + '\nimport_bootstrap_log_group'],
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode == 0, state_fail == "false", result.stderr)
            self.assertNotIn("UNEXPECTED IMPORT", result.stderr)


if __name__ == "__main__":
    unittest.main()
