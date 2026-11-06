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
    def test_factory_fresh_configuration_avoids_legacy_role_and_inline_quota(self):
        source = (ROOT / 'platform/scripts/deploy-all.sh').read_text()
        start = source.index('  if [ "$UPDATE_MODE" = false ]; then',
                             source.index('# Step 10/11: Agent Factory'))
        block = source[start:source.index('  terraform init', start)]
        prefix = """set -euo pipefail
aws() { return 1; }
kubectl() { return 1; }
"""
        for environment in ('dev', 'customer-test'):
            for update in (False, True):
                with self.subTest(environment=environment, update=update), tempfile.TemporaryDirectory() as tmp:
                    config = Path(tmp) / 'terraform.tfvars'
                    original = 'runner_role_name = "existing-owned-role"\ngateway_intake_managed_policy = false\n'
                    config.write_text(original)
                    recovered = Path(tmp) / 'agent-factory.tfvars.json'
                    recovered.write_text('{"runner_role_name":"recovered-owned-role"}')
                    env = dict(os.environ, ENVIRONMENT=environment, AWS_REGION='eu-west-1',
                               ADP_GITHUB_ORG='customer-org', UPDATE_MODE=str(update).lower(),
                               UPGRADE_RUN_DIR=tmp)
                    result = subprocess.run(['bash', '-c', prefix + block +
                                             '\nprintf "%s" "${FACTORY_VAR_FILE:-}"'],
                                            cwd=tmp, env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(recovered.read_text(), '{"runner_role_name":"recovered-owned-role"}')
                    if update:
                        self.assertEqual(config.read_text(), original)
                        self.assertEqual(result.stdout, str(recovered))
                    else:
                        values = dict(re.findall(r'^(\w+)\s*=\s*(.+)$', config.read_text(), re.M))
                        self.assertEqual(values['runner_role_name'], f'"adp-{environment}-agent-factory-runner-role"')
                        self.assertNotEqual(values['runner_role_name'], f'"adp-{environment}-agent-runner-role"')
                        self.assertEqual(values['gateway_intake_managed_policy'], 'true')
                        self.assertEqual(values['environment'], f'"{environment}"')
                        self.assertEqual(values['aws_region'], '"eu-west-1"')
                        self.assertEqual(values['enable_github_apps'], 'false')
                        self.assertEqual(result.stdout, '')

    def test_gateway_initial_manifests_defer_capacity_to_selected_bundle(self):
        source = (ROOT / 'platform/scripts/deploy-all.sh').read_text()
        start = source.index('  for f in k8s/*.yaml; do')
        block = source[start:source.index('\n  done', start)] + '\n  done\n'
        prefix = '''set -euo pipefail
kubectl() {
  if [ "$1" = create ]; then
    case "$*" in *namespace.yaml*) echo Namespace ;; *) echo Service ;; esac
  else
    echo "$*"
  fi
}
'''
        for update in (False, True):
            with self.subTest(update=update), tempfile.TemporaryDirectory() as tmp:
                k8s = Path(tmp) / 'k8s'
                k8s.mkdir()
                for name in ('hpa', 'pdb', 'namespace', 'service', 'configmap', 'deployment',
                             'serviceaccount', 'targetgroupbinding'):
                    (k8s / f'{name}.yaml').touch()
                result = subprocess.run(['bash', '-c', prefix + block], cwd=tmp,
                                        env=dict(os.environ, UPDATE_MODE=str(update).lower()),
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = ['apply -f k8s/service.yaml -n adp-gateway']
                self.assertEqual(result.stdout.splitlines(), expected)

    def test_fresh_gateway_verification_enables_autoscaling_and_requires_readiness(self):
        source = (ROOT / 'platform/scripts/deploy-all.sh').read_text()
        start = source.index('if [ "$DEPLOY_GATEWAY" = true ]; then',
                             source.index('if deploy_phase_begin verify; then'))
        block = source[start:source.index('\nif [ "$CI_MODE"', start)]
        prefix = '''set -euo pipefail
step() { :; }
fail() { echo "$*" >&2; exit 1; }
record() { echo "$*" >> "$CALLS"; [ "$FAIL_AT" != "$1" ]; }
capacity_lock() { record "capacity-$1"; }
python3() {
  if [ "$1" = "$SCRIPT_DIR/render-gateway-capacity.py" ]; then record "$4"; return; fi
  command python3 "$@"
}
kubectl() { record kubectl "$@"; }
wait_for_gateway_rollout() { record ready; }
aws() { echo example.test; }
curl() { record health || return; echo '{"status":"healthy"}'; }
'''
        cases = [(False, True, ''), (True, True, ''), (False, False, '')]
        cases += [(False, True, failure) for failure in
                  ('capacity-verify', '--verify-bundle', 'kubectl', 'ready', 'health', '--verify-applied', 'capacity-release')]
        cases += [(True, True, failure) for failure in ('capacity-verify', '--verify-applied')]
        for update, gateway, failure in cases:
            with self.subTest(update=update, gateway=gateway, failure=failure), tempfile.TemporaryDirectory() as tmp:
                calls = Path(tmp) / 'calls'
                calls.touch()
                env = dict(os.environ, ROOT_DIR=str(ROOT), ENVIRONMENT='dev', CALLS=str(calls),
                           UPDATE_MODE=str(update).lower(), DEPLOY_GATEWAY=str(gateway).lower(),
                           SCRIPT_DIR=str(ROOT / 'platform/scripts'), CAPACITY_BUNDLE=f'{tmp}/bundle',
                           CAPACITY_LOCK_DIR=f'{tmp}/lock', FAIL_AT=failure)
                result = subprocess.run(['bash', '-c', prefix + block], env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, not failure, result.stderr)
                expected = []
                if gateway:
                    if not update:
                        expected.extend(['capacity-verify', '--verify-bundle',
                                         f'kubectl apply -f {tmp}/bundle/hpa.json -n adp-gateway', 'ready'])
                    expected.extend(['health', 'capacity-verify', '--verify-applied', 'capacity-release'])
                if failure:
                    stopped = next(index for index, event in enumerate(expected) if event.split()[0] == failure)
                    expected = expected[:stopped + 1]
                self.assertEqual(calls.read_text().splitlines(), expected)

    def test_initial_gateway_plan_preserves_engine_and_refuses_failed_discovery(self):
        source = (ROOT / 'platform/scripts/deploy-all.sh').read_text()
        start = source.index('if deploy_phase_begin gateway-infra; then')
        block = source[start:source.index('\nrefresh_credentials', start)]
        prefix = '''set -euo pipefail
deploy_phase_begin() { return 0; }
deploy_phase_complete() { :; }
step() { :; }
fail() { echo "$*" >&2; exit 1; }
python3() {
  if [[ " $* " = *" --current-image-digest "* ]]; then
    [ "$DISCOVERY_FAIL" = false ] || return 1
    echo "$OLD_DIGEST"
  else
    echo quiesce >> "$CALLS"
  fi
}
terraform() { echo init >> "$CALLS"; }
bash() { :; }
gateway_alb_vars() { GATEWAY_ALB_ARGS=(-var preserved-albs); }
terraform_update_apply() { printf '%s\\n' "$*" >> "$CALLS"; }
tf_adopt_existing() { :; }
gateway_role_policy_cutover() { :; }
'''
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                calls = Path(tmp) / 'calls'
                digest = 'sha256:' + 'b' * 64
                env = dict(os.environ, ROOT_DIR=str(ROOT), SCRIPT_DIR=str(ROOT / 'platform/scripts'),
                           UPDATE_MODE='true', DEPLOY_GATEWAY='true', ACCOUNT_ID='123456789012',
                           ENVIRONMENT='dev', AWS_REGION='us-east-1', GATEWAY_UPDATE_VAR_FILE='gateway.json',
                           UPGRADE_RUN_DIR=tmp,
                           CALLS=str(calls), OLD_DIGEST=digest, DISCOVERY_FAIL=str(failure).lower())
                result = subprocess.run(['bash', '-c', prefix + block], env=env, capture_output=True, text=True)
                if failure:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(calls.exists(), 'Discovery must fail before pricing or Terraform changes')
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(calls.read_text().splitlines(), [
                        'quiesce', 'quiesce', 'init', f'gateway gateway.json -var preserved-albs -var orchestration_tick_image_digest={digest}'])

    def test_gateway_retry_does_not_restart_unchanged_pods(self):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        start = source.rindex('  if [ "$UPDATE_MODE" = true ]; then', 0,
                              source.index('    # Applying a changed pod template'))
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
                result = subprocess.run(['bash', '-c', stub + (ROOT / 'platform/scripts/gateway-rollout.sh').read_text() + '\n' + block], env=env, text=True, capture_output=True)
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
        block = source[start:source.index('\ndeploy_phase_complete\n', start)]
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
                           GATEWAY_UPDATE_VAR_FILE="/tmp/gateway.tfvars.json", WORKER_MIGRATION="false")
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

    def finalize(self, audit_fail=False, ci_mode=False, deferred=False, engine_fail=False,
                 worker_migration=False, admission_fail=False):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        start = source.index("if deploy_phase_begin finalize; then")
        block = source[start:source.index("# Summary\n", start)]
        # These assertions exercise phase ordering; checkpoint persistence has
        # separate executable recovery tests.
        prefix = r'''set -euo pipefail
deploy_phase_begin() { return 0; }
deploy_phase_complete() { :; }
step() { :; }
fail() { echo "$*" >&2; exit 1; }
capacity_lock() { echo "capacity $1" >> "$CALLS"; }
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
    [[ " $* " = *" -var orchestration_tick_image_digest=${GATEWAY_IMAGE##*@} "* ]] || return 9
  fi
}
gateway_alb_vars() { GATEWAY_ALB_ARGS=(-var preserved-albs); }
worker_migration() {
  echo "migration $1" >> "$CALLS"
  [ "$1" != admit ] || [ "$ADMISSION_FAIL" = false ]
}
bash() { echo "frontend $*" >> "$CALLS"; }
aws() { echo frontend.example.test; }
curl() { echo '{"status":"healthy"}'; }
'''
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / "calls"
            env = dict(os.environ, ROOT_DIR=str(ROOT), SCRIPT_DIR=str(ROOT / "platform/scripts"),
                       CAPACITY_BUNDLE=f"{tmp}/bundle", CAPACITY_LOCK_DIR=f"{tmp}/lock",
                       UPDATE_MODE="true", DEPLOY_GATEWAY="true", DEPLOY_FACTORY="true", SKIP_FRONTEND="false", UPGRADE_RUN_DIR=tmp,
                       GATEWAY_UPDATE_VAR_FILE="/tmp/gateway.tfvars.json", IMAGE_TAG="a" * 40,
                       GATEWAY_IMAGE="customer-gateway@sha256:" + "b" * 64, ACCOUNT_ID="925091290508",
                       ENGINE_FAIL=str(engine_fail).lower(),
                       WORKER_MIGRATION=str(worker_migration).lower(), ADMISSION_FAIL=str(admission_fail).lower(),
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
        self.assertEqual(calls[7], "capacity verify")
        self.assertIn("render-gateway-capacity.py --environment test --verify-applied", calls[8])
        self.assertEqual(calls[9], "capacity release")
        self.assertIn("enable-bedrock-models.sh --verify", calls[10])

    def test_final_engine_mismatch_prevents_frontend_and_success(self):
        result, calls = self.finalize(engine_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("deploy-frontend.sh" in line or "upgrade-state.py verify" in line for line in calls))

    def test_worker_admission_follows_finalization_and_precedes_schedule_restore(self):
        result, calls = self.finalize(worker_migration=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        admitted = calls.index('migration admit')
        complete = calls.index('migration complete')
        self.assertTrue(any('deploy-frontend.sh' in line for line in calls[:admitted]))
        self.assertIn('deploy-webhook-ingress.sh', calls[admitted + 1])
        self.assertIn('--update --skip-image --skip-lambda', calls[admitted + 1])
        self.assertEqual(calls[complete + 1], 'terraform gateway-final check=false')
        self.assertIn('upgrade-state.py verify', calls[complete + 2])

    def test_failed_admission_does_not_restore_schedule_or_declare_success(self):
        result, calls = self.finalize(worker_migration=True, admission_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls[-1], 'migration admit')

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
