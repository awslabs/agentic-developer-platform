"""Execute the factory upgrade stage with service doubles to catch omitted publishes."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SHA = "b" * 40
DIGEST = "sha256:" + "a" * 64
REGISTRY = "111122223333.dkr.ecr.us-east-1.amazonaws.com"
TERRAFORM_DOUBLE = """
terraform() {
  if [ "$1" = console ]; then
    # Like Terraform, read the piped expression before returning its value.
    # Exiting early can SIGPIPE printf and fail the stage under pipefail.
    cat >/dev/null
    echo "$INTAKE_MANAGED_TEST"
  fi
  if [ "$1" = output ]; then echo value; fi
}
"""


class FactoryArtifactsTests(unittest.TestCase):
    def run_stage(self, package_exit=0, chat_exit=0, intake_managed=True,
                  current_pentest_client="value", chat_mode="retire", chat_exception="",
                  sandbox_digest="b" * 64, scaledjob_service_account="adp-chat-supervisor",
                  apigw_invoke_url="https://abc123def4.execute-api.us-east-1.amazonaws.com/dev/",
                  model_probe_enabled="true", probe_cycle_exit=0, chat_model_policy_posture="advisory"):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        start = source.index(
            'if [ "$DEPLOY_FACTORY" = true ]; then\n  step "Step 10/11:'
        )
        # Exercise the factory body, excluding its surrounding checkpoint wrapper.
        block = source[start : source.index("\ndeploy_phase_complete", start)]
        prefix = """set -euo pipefail
step() { :; }; ok() { :; }; warn() { :; }
fail() { echo "$*" >&2; exit 1; }
bash() {
  case "$1" in
    */apply-chat-tls.sh)
      echo "tls env=${ENVIRONMENT} region=${AWS_REGION}" >> "$CALLS"
      echo "chat-sandbox-gateway-ca-0123456789abcdef"; return 0 ;;
    */run-probe-cycle.sh)
      # The real helper creates a Job from the probe CronJob and reads its log (#7967);
      # here only its exit code matters: 0 proven, 3 nothing to claim, else failure.
      echo "probe-cycle env=${ENVIRONMENT}" >> "$CALLS"; return "$PROBE_CYCLE_EXIT" ;;
    */deploy-chat-scaledjob.sh)
      # Shell functions see the caller's variables; only the environment prefixed
      # on the full-mode call is meaningful, so the controls-only call logs its mode.
      if [ "${ADP_CHAT_CREW_CONTROLS_ONLY:-}" = true ]; then echo "scaledjob crew=true" >> "$CALLS"; else
        echo "scaledjob crew=false policy=${ADP_CHAT_MODEL_POLICY_ENABLED:-} ns=${ADP_CHAT_EXISTING_NAMESPACE_ONLY:-} supervisor=${SUPERVISOR_IMAGE:-} sandbox=${SANDBOX_IMAGE:-} origin=${GATEWAY_HTTPS_ORIGIN:-} crew_origin=${ADP_CREW_DASHBOARD_ORIGIN:-}" >> "$CALLS"
      fi
      return 0 ;;
  esac
  echo "script $(basename "$1") image=${AGENT_IMAGE:-}" >> "$CALLS"
  if [[ "$1" == */build-agent-factory-lambdas.sh ]]; then return "$PACKAGE_EXIT"; fi
}
python3() {
  if [[ "$1" == */upgrade-chat.py ]]; then
    if [[ " $* " == *" --delegated "* ]]; then echo "chat $2 --delegated" >> "$CALLS"; else echo "chat $2" >> "$CALLS"; fi
    return 0
  fi
  command python3 "$@"
}
terraform_update_apply() {
  if [[ " $* " == *" -var=chat_model_policy_enabled=true "* ]]; then echo "plan $1 policy=true" >> "$CALLS"; else echo "plan $1" >> "$CALLS"; fi
}
tf_adopt_existing() { :; }
kubectl() {
  if [ "$1" = apply ]; then cat >/dev/null; fi
  if [ "$1" = get ]; then
    if [[ "$*" == *ADP_CHAT_SANDBOX_CA_CONFIGMAP* ]]; then echo "";
    elif [ "$2" = configmap ]; then echo "$CURRENT_PENTEST_CLIENT_TEST";
    elif [ "$2 $3" = "scaledjob chat-agent-worker" ]; then echo "$SCALEDJOB_SA_TEST";
    else echo "${AGENT_IMAGE:-}"; fi
  fi
  if [ "$1 $2" = "rollout restart" ]; then echo "restart gateway" >> "$CALLS"; fi
  if [ "$1" = patch ]; then
    keys="$(echo "$*" | grep -o 'ADP_CHAT_[A-Z_]*' | sort -u | tr '\\n' ' ' | sed 's/ $//' || true)"
    [ -z "$keys" ] || echo "patched $keys" >> "$CALLS"
  fi
}
run_codebuild() {
  echo "build $1 tag=$IMAGE_TAG" >> "$CALLS"
  if [[ "$1" == *-chat-agent ]]; then return "$CHAT_EXIT"; fi
}
""" + TERRAFORM_DOUBLE
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / "calls"
            binary = Path(tmp) / "aws"
            binary.write_text(
                '#!/bin/sh\n'
                'if [ "$1 $2" = "iam list-role-policies" ]; then '
                'echo \'{"PolicyNames":["adp-dev-policy-gateway-intake"]}\'; exit 0; fi\n'
                'if [ "$1 $2" = "iam get-role-policy" ]; then '
                'echo \'{"PolicyDocument":{"Version":"2012-10-17","Statement":[]}}\'; exit 0; fi\n'
                'if [ "$1 $2" = "ssm put-parameter" ]; then echo "ssm-put $4=$8" >> "$CALLS"; exit 0; fi\n'
                'if [ "$1 $2" = "ssm get-parameter" ]; then case "$*" in '
                '*apigw-invoke-url*) echo "' + apigw_invoke_url + '" ;; '
                '*crew-dashboard-origin*) echo "https://crew.example.test" ;; '
                # Model-probe controls the delegated guard reads (#7967); gateway infra
                # publishes them from the environment's gateway tfvars.
                '*model-probe-enabled*) echo "' + model_probe_enabled + '" ;; '
                # Chat model-policy posture (#7997); the guard fails only under enforced.
                '*chat-model-policy-posture*) echo "' + chat_model_policy_posture + '" ;; '
                '*model-probe-max-slots-per-cycle*) echo "24" ;; '
                '*model-probe-budget-usd-per-cycle*) echo "5" ;; '
                '*model-probe-max-budget-usd-per-attempt*) echo "0.50" ;; '
                '*) echo "None" ;; esac; exit 0; fi\n'
                'case "$*" in *imageTag=sandbox-*) printf "sha256:%s\\n" "' + sandbox_digest + '"; exit 0 ;; esac\n'
                'printf "sha256:%s\\n" "' + "a" * 64 + '"\n'
            )
            binary.chmod(0o755)
            env = dict(
                os.environ,
                ROOT_DIR=str(ROOT),
                SCRIPT_DIR=str(ROOT / "platform/scripts"),
                ENVIRONMENT="dev",
                AWS_REGION="us-east-1",
                ACCOUNT_ID="111122223333",
                DEPLOY_FACTORY="true",
                DEPLOY_GATEWAY="true",
                UPDATE_MODE="true",
                LOCAL_MODE="false",
                UPGRADE_RUN_DIR=tmp,
                STATE_BUCKET="state",
                REGISTRY=REGISTRY,
                IMAGE_TAG=SHA,
                CALLS=str(calls),
                PACKAGE_EXIT=str(package_exit),
                CHAT_EXIT=str(chat_exit),
                PROBE_CYCLE_EXIT=str(probe_cycle_exit),
                INTAKE_MANAGED_TEST=str(intake_managed).lower(),
                CURRENT_PENTEST_CLIENT_TEST=current_pentest_client,
                CHAT_MODE=chat_mode,
                CHAT_EXCEPTION=chat_exception,
                SCALEDJOB_SA_TEST=scaledjob_service_account,
                PATH=tmp + os.pathsep + os.environ["PATH"],
            )
            result = subprocess.run(
                ["bash", "-c", prefix + (ROOT / 'platform/scripts/gateway-rollout.sh').read_text() + '\n' + block + '\nprintf "tag=%s\\n" "$IMAGE_TAG"'],
                env=env,
                text=True,
                capture_output=True,
            )
            return result, calls.read_text().splitlines()

    def test_console_double_consumes_input_without_breaking_the_producer(self):
        # Exceed the pipe capacity so an early-closing console fails reliably
        # under pipefail, instead of only when CI schedules it before printf.
        result = subprocess.run(
            ["bash", "-c", "set -euo pipefail\nINTAKE_MANAGED_TEST=true\n"
             + TERRAFORM_DOUBLE
             + "printf '%4194304s\\n' var.gateway_intake_managed_policy "
             "| terraform console -no-color | tail -n 1"],
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "true\n")

    def test_all_artifacts_are_published_with_distinct_worker_images(self):
        result, calls = self.run_stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls,
            [
                "script build-agent-factory-lambdas.sh image=",
                "chat prepare",
                "plan agent-factory",
                "plan agent-factory",
                "chat drain",
                "plan agent-factory",
                "plan agent-factory",
                "chat verify",
                f"build adp-dev-agent-gateway tag={SHA}",
                f"build adp-dev-chat-agent tag={SHA}",
            ],
        )
        self.assertIn("tag=" + SHA, result.stdout)

    def test_inline_mode_runs_one_factory_plan(self):
        result, calls = self.run_stage(intake_managed=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count("plan agent-factory"), 3)

    def test_changed_pentest_client_restarts_gateway(self):
        result, calls = self.run_stage(current_pentest_client="old-value")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count("restart gateway"), 1)

    def test_failed_lambda_package_stops_before_infrastructure(self):
        result, calls = self.run_stage(package_exit=43)
        self.assertEqual(result.returncode, 43)
        self.assertEqual(len(calls), 1)

    def test_failed_chat_build_stops_before_chat_deployment(self):
        result, calls = self.run_stage(chat_exit=44)
        self.assertEqual(result.returncode, 44)
        self.assertFalse(any("deploy-chat-scaledjob.sh" in line for line in calls))

    def test_retire_and_keep_legacy_never_touch_delegated_chat(self):
        for mode in ("retire", "keep-legacy"):
            with self.subTest(mode=mode):
                result, calls = self.run_stage(chat_mode=mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(any(line.startswith(("tls", "scaledjob")) for line in calls))
                self.assertFalse(any("policy=true" in line for line in calls))
                self.assertFalse(any("--delegated" in line for line in calls))

    def test_keep_legacy_skips_drain_denial_removal_and_verification(self):
        result, calls = self.run_stage(chat_mode="keep-legacy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls,
            [
                "script build-agent-factory-lambdas.sh image=",
                "chat prepare",
                "plan agent-factory",
                "plan agent-factory",
                f"build adp-dev-agent-gateway tag={SHA}",
                f"build adp-dev-chat-agent tag={SHA}",
            ],
        )

    def test_delegated_mode_activates_the_supervisor_before_retiring_legacy_work(self):
        result, calls = self.run_stage(chat_mode="delegated", chat_exception="#6937-evidence-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        supervisor = f"{REGISTRY}/adp-chat-agent@{DIGEST}"
        sandbox = f"{REGISTRY}/adp-chat-agent@sha256:{'b' * 64}"
        self.assertEqual(
            calls,
            [
                "script build-agent-factory-lambdas.sh image=",
                "chat prepare --delegated",
                "plan agent-factory policy=true",
                "plan agent-factory policy=true",
                f"build adp-dev-chat-agent tag={SHA}",
                "tls env=dev region=us-east-1",
                # The digest publication (SSM chat-*-digests) and the bedrockgateway-config
                # patch + restart are owned by deploy-chat-scaledjob.sh after the ScaledJob
                # apply, on this path and on chat-agent-deploy.yml alike (#8086).
                "scaledjob crew=true",
                "scaledjob crew=false policy=true ns=true "
                f"supervisor={supervisor} sandbox={sandbox} "
                "origin=https://abc123def4.execute-api.us-east-1.amazonaws.com/dev crew_origin=https://crew.example.test",
                "probe-cycle env=dev",
                "chat drain --delegated",
                "plan agent-factory policy=true",
                "plan agent-factory policy=true",
                "chat verify --delegated",
                f"build adp-dev-agent-gateway tag={SHA}",
            ],
        )
        self.assertFalse(any(line.startswith(("ssm-put", "patched ADP_CHAT", "restart gateway")) for line in calls),
                         "deploy-all.sh no longer publishes the chat identities itself (#8086)")

    def test_delegated_mode_refuses_a_gateway_url_that_is_not_the_api_stage_in_region(self):
        for published in ("https://gateway.example.test", "https://d1g6cal2ts4iis.cloudfront.net",
                          "https://abc123def4.execute-api.eu-west-1.amazonaws.com/dev",
                          "https://abc123def4.execute-api.us-east-1.amazonaws.com/dev/agent", "None"):
            with self.subTest(published=published):
                result, calls = self.run_stage(chat_mode="delegated", chat_exception="#6937-evidence-1",
                                               apigw_invoke_url=published)
                self.assertEqual(result.returncode, 1)
                self.assertIn("apigw-invoke-url", result.stderr)
                self.assertFalse(any(line.startswith("scaledjob") for line in calls))

    def test_delegated_mode_refuses_identical_supervisor_and_sandbox_digests(self):
        result, calls = self.run_stage(chat_mode="delegated", chat_exception="#6937-evidence-1",
                                       sandbox_digest="a" * 64)
        self.assertEqual(result.returncode, 1)
        self.assertIn("distinct", result.stderr)
        self.assertFalse(any(line.startswith(("tls", "scaledjob", "chat drain")) for line in calls))

    def test_delegated_mode_stops_before_retirement_when_the_scaledjob_is_not_delegated(self):
        result, calls = self.run_stage(chat_mode="delegated", chat_exception="#6937-evidence-1",
                                       scaledjob_service_account="adp-agent")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not the delegated supervisor", result.stderr)
        self.assertFalse(any(line.startswith("chat drain") for line in calls))

    def test_delegated_mode_refuses_before_building_when_model_probing_is_off_under_enforced(self):
        """Under chat_model_policy_posture=enforced every turn without probe evidence is 503 probing_disabled (#7967)."""
        for published in ("false", "None", ""):
            with self.subTest(published=published):
                result, calls = self.run_stage(chat_mode="delegated", chat_exception="#6937-evidence-1",
                                               model_probe_enabled=published, chat_model_policy_posture="enforced")
                self.assertEqual(result.returncode, 1)
                self.assertIn("Delegated chat enforces model policy (chat_model_policy_posture=enforced) and needs invocability evidence",
                              result.stderr)
                self.assertIn("environments/dev/modules/gateway.tfvars", result.stderr)
                self.assertFalse(any(line.startswith(("build adp-dev-chat-agent", "tls", "scaledjob", "chat drain")) for line in calls))

    def test_delegated_mode_proceeds_with_a_warning_when_model_probing_is_off_under_advisory(self):
        """#7997: the default posture is advisory; chat answers without evidence, so the rollout continues.

        The plumbing switch stays on (ingest chat_model_policy_enabled=true, ScaledJob
        ADP_CHAT_MODEL_POLICY_ENABLED=true): without it the ingest refuses every turn.
        """
        for posture in ("advisory", "off", "None", ""):
            with self.subTest(posture=posture):
                result, calls = self.run_stage(chat_mode="delegated", chat_exception="#6937-evidence-1",
                                               model_probe_enabled="false", chat_model_policy_posture=posture)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("plan agent-factory policy=true", calls)
                self.assertTrue(any(line.startswith("scaledjob crew=false policy=true") for line in calls), calls)
                self.assertIn("probe-cycle env=dev", calls)
                self.assertIn("chat drain --delegated", calls)
                self.assertNotIn("needs invocability evidence", result.stderr)

    def test_probe_cycle_without_proven_evidence_stops_before_retirement_only_under_enforced(self):
        result, calls = self.run_stage(chat_mode="delegated", chat_exception="#6937-evidence-1", probe_cycle_exit=1,
                                       chat_model_policy_posture="enforced")
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not record proven evidence (exit 1)", result.stderr)
        self.assertIn("probe-cycle env=dev", calls)
        self.assertFalse(any(line.startswith("chat drain") for line in calls))
        result, calls = self.run_stage(chat_mode="delegated", chat_exception="#6937-evidence-1", probe_cycle_exit=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(calls.index("probe-cycle env=dev"), calls.index("chat drain --delegated"))

    def test_probe_cycle_with_nothing_to_claim_lets_retirement_proceed(self):
        result, calls = self.run_stage(chat_mode="delegated", chat_exception="#6937-evidence-1", probe_cycle_exit=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(calls.index("probe-cycle env=dev"), calls.index("chat drain --delegated"))


if __name__ == "__main__":
    unittest.main()
