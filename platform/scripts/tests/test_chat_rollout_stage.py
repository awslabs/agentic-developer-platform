"""deploy-all.sh chat rollout mode selection and refusal (#7899, SCN-01).

The option parser and the chat-mode gate are extracted from deploy-all.sh and
executed with shell doubles, so a refusal is proven to happen before preflight,
any build or any apply, and the default stays today's `retire` behaviour.
"""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
SOURCE = (ROOT / "platform/scripts/deploy-all.sh").read_text()
K8S = ROOT / "modules/agent-factory/agent/k8s"
SCALEDJOB_SCRIPT = (K8S / "deploy-chat-scaledjob.sh").read_text()
ACCOUNT = "123456789012"
SUPERVISOR = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/adp-chat-agent@sha256:{'a' * 64}"
SANDBOX = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/adp-chat-agent@sha256:{'b' * 64}"
SUPERVISOR_DIGEST = SUPERVISOR.rpartition("@")[2]
SANDBOX_DIGEST = SANDBOX.rpartition("@")[2]
STALE_DIGEST = f"sha256:{'0' * 64}"


def gate_block():
    # From the option defaults through the option parser, the output helpers and
    # the chat gate itself, up to the first unrelated check. The real `ok`/`warn`/
    # `fail` helpers run (they print to stdout with colour), so assertions read
    # the combined, colour-stripped output.
    start = SOURCE.index("CHAT_MODE=\"${ADP_CHAT_MODE:-retire}\"")
    start = SOURCE.rindex("\n", 0, start) + 1
    end = SOURCE.index('if [ "${ADP_ENABLED_DOMAIN_APPS+x}" = x ]; then')
    block = SOURCE[start:end]
    return (
        "set -euo pipefail\n"
        "AWS_REGION=us-east-1; ENVIRONMENT=dev\n"
        + block
        + '\nprintf "mode=%s exception=%s policy=%s\\n" "$CHAT_MODE" "$CHAT_EXCEPTION" "$CHAT_MODEL_POLICY_ENABLED"\n'
    )


def plain(result):
    return re.sub(r"\x1b\[[0-9;]*m", "", result.stdout + result.stderr)


class ChatModeGateTests(unittest.TestCase):
    def run_gate(self, *args, env=None):
        child = {k: v for k, v in os.environ.items() if not k.startswith("ADP_CHAT_")}
        child.update(env or {})
        return subprocess.run(["bash", "-c", gate_block() + "\n", "deploy-all.sh", *args],
                              capture_output=True, text=True, env=child)

    def test_default_mode_is_retire_and_announces_the_retirement(self):
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, plain(result))
        self.assertIn("mode=retire exception= policy=false", result.stdout)
        self.assertIn("✓ Chat mode: retire", plain(result))
        self.assertIn("will be retired; chat becomes unavailable", plain(result))

    def test_keep_legacy_warns_that_the_credentialed_worker_remains(self):
        result = self.run_gate("--chat", "keep-legacy")
        self.assertEqual(result.returncode, 0, plain(result))
        self.assertIn("mode=keep-legacy exception= policy=false", result.stdout)
        self.assertIn("⚠ Chat mode: keep-legacy", plain(result))
        self.assertIn("will remain running", plain(result))

    def test_delegated_without_a_record_is_refused(self):
        result = self.run_gate("--chat", "delegated")
        self.assertEqual(result.returncode, 1)
        self.assertIn("✗ --chat delegated requires --chat-exception", plain(result))
        self.assertIn("refusing before any build", plain(result))
        self.assertNotIn("mode=", result.stdout)

    def test_delegated_with_a_record_is_accepted_recorded_and_printed(self):
        for record in ("#6937:evidence/42", "#6937 evidence 42", "ADP-7899/exception-1"):
            with self.subTest(record=record):
                result = self.run_gate("--chat", "delegated", "--chat-exception", record)
                self.assertEqual(result.returncode, 0, plain(result))
                self.assertIn(f"mode=delegated exception={record} policy=true", result.stdout)
                self.assertIn(f"✓ Chat mode: delegated (qualification/exception record: {record})", plain(result))

    def test_environment_variables_select_the_same_values(self):
        result = self.run_gate(env={"ADP_CHAT_MODE": "delegated", "ADP_CHAT_EXCEPTION": "#6937-evidence"})
        self.assertEqual(result.returncode, 0, plain(result))
        self.assertIn("mode=delegated exception=#6937-evidence policy=true", result.stdout)
        refused = self.run_gate(env={"ADP_CHAT_MODE": "delegated"})
        self.assertEqual(refused.returncode, 1)
        # The flag wins over the environment.
        result = self.run_gate("--chat", "retire", env={"ADP_CHAT_MODE": "keep-legacy"})
        self.assertIn("mode=retire", result.stdout)

    def test_unknown_mode_and_stray_exception_are_refused(self):
        result = self.run_gate("--chat", "bogus")
        self.assertEqual(result.returncode, 1)
        self.assertIn("retire, delegated or keep-legacy", plain(result))
        result = self.run_gate("--chat-exception", "#6937")
        self.assertEqual(result.returncode, 1)
        self.assertIn("only meaningful with --chat delegated", plain(result))
        result = self.run_gate("--chat", "delegated", "--chat-exception", "bad value; rm -rf")
        self.assertEqual(result.returncode, 1)
        self.assertIn("short record reference", plain(result))
        result = self.run_gate("--chat")
        self.assertNotEqual(result.returncode, 0)

    def test_gate_runs_before_preflight_builds_and_applies(self):
        gate = SOURCE.index('*) fail "--chat must be retire, delegated or keep-legacy')
        for later in ('step "Preflight checks"', "run_codebuild()", 'bash "$SCRIPT_DIR/enable-bedrock-models.sh"',
                      "terraform init", "deploy_checkpoint_init"):
            self.assertLess(gate, SOURCE.index(later), later)

    def test_help_documents_the_chat_options(self):
        for text in ("--chat retire", "--chat keep-legacy", "--chat delegated", "--chat-exception REF"):
            self.assertIn(text, SOURCE)


class WorkloadIdentityPublicationTests(unittest.TestCase):
    """Delegated mode publishes the digests/service account the gateway verifies chat
    pods against (src/agentauth/workload.py) and renders them into the ConfigMap."""

    PARAMETERS = (
        ("chat-authority-worker-images", "$SUPERVISOR_DIGEST"),
        ("chat-sandbox-image-digests", "$SANDBOX_DIGEST"),
        ("chat-crew-image-digests", "$SANDBOX_DIGEST"),
        ("chat-worker-service-account", "adp-chat-supervisor"),
    )

    GATEWAY_KEYS = ("ADP_CHAT_WORKER_IMAGE_DIGESTS", "ADP_CHAT_WORKER_SERVICE_ACCOUNT",
                    "ADP_CHAT_SANDBOX_IMAGE_DIGESTS", "ADP_CHAT_CREW_IMAGE_DIGESTS",
                    "ADP_CHAT_SANDBOX_CA_CONFIGMAP", "ADP_CHAT_DATA_URL", "ADP_CHAT_DATA_ENABLED")

    def test_deploy_all_no_longer_publishes_the_identities_itself_but_still_reports_them(self):
        """#8086: one owner on both deploy paths. deploy-all.sh keeps its `ok` lines for the run log
        and journal, but the puts, the ConfigMap patch and the gateway restart live in the script."""
        stage = SOURCE[SOURCE.index('step "Step 10c/11: Delegated chat rollout'):SOURCE.index('ok "Delegated chat ScaledJob is Ready')]
        self.assertNotIn("ssm put-parameter", SOURCE)
        for text in ("kubectl patch configmap bedrockgateway-config", "kubectl rollout restart deployment/bedrockgateway",
                     "wait_for_gateway_rollout", '"ADP_CHAT_WORKER_IMAGE_DIGESTS":'):
            self.assertNotIn(text, stage)
        for name, _ in self.PARAMETERS:
            self.assertNotIn(f'"/adp/${{ENVIRONMENT}}/gateway/{name}=', stage)
        scaledjob = stage.index('ADP_CHAT_CREW_CONTROLS_ONLY=false \\\n      bash "$ROOT_DIR/modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh"')
        published = stage.index('ok "Published chat release identities (deploy-chat-scaledjob.sh)')
        self.assertLess(scaledjob, published)
        for name, _ in self.PARAMETERS:
            self.assertIn(f"{name}=", stage[published:])
        self.assertIn('ok "Gateway reconfigured for delegated chat (CA ConfigMap ${CHAT_SANDBOX_CA_CONFIGMAP}', stage)

    def test_script_publishes_the_identities_and_converges_the_gateway_after_the_scaledjob(self):
        full = SCALEDJOB_SCRIPT[SCALEDJOB_SCRIPT.index('NAMESPACE="${NAMESPACE:-adp-gateway-agents}"'):]
        objects = full.index('kubectl apply -f "$WORK/objects.json"')
        ready = full.index("kubectl wait --for=condition=Ready scaledjob/chat-agent-worker")
        publish = full.index("publish_chat_value chat-authority-worker-images")
        read = full.index("kubectl get configmap bedrockgateway-config -n adp-gateway -o json")
        patch = full.index("kubectl patch configmap bedrockgateway-config -n adp-gateway --type merge")
        restart = full.index("kubectl rollout restart deployment/bedrockgateway -n adp-gateway")
        wait = full.index('kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout="$GATEWAY_ROLLOUT_TIMEOUT"')
        self.assertLess(objects, ready)
        self.assertLess(ready, publish)
        self.assertLess(publish, read)
        self.assertLess(read, patch)
        self.assertLess(patch, restart)
        self.assertLess(restart, wait)
        for name, value in (("chat-authority-worker-images", '"$SUPERVISOR_DIGEST"'), ("chat-sandbox-image-digests", '"$SANDBOX_DIGEST"'),
                            ("chat-crew-image-digests", '"$SANDBOX_DIGEST"'), ("chat-worker-service-account", "adp-chat-supervisor"),
                            ("chat-data-enabled", "true")):
            self.assertIn(f"publish_chat_value {name} {value}\n", full)
        for key in self.GATEWAY_KEYS:
            self.assertIn(f'"{key}":', full[publish:])
        # The gateway rollout bound is validated before anything is applied.
        timeout = full.index('GATEWAY_ROLLOUT_TIMEOUT="${ADP_CHAT_GATEWAY_ROLLOUT_TIMEOUT:-600s}"')
        self.assertLess(timeout, full.index('kubectl apply -f "$WORK/prepull.json"'))
        self.assertIn("ADP_CHAT_GATEWAY_ROLLOUT_TIMEOUT must be <seconds>s", full)

    def test_both_renderers_read_the_parameters_into_the_configmap(self):
        workflow = (ROOT / ".github/workflows/gateway-deploy.yml").read_text()
        configmap = (ROOT / "modules/gateway/k8s/configmap.yaml").read_text()
        for key, parameter, helper in (
            ("ADP_CHAT_SANDBOX_IMAGE_DIGESTS", "chat-sandbox-image-digests", "get_authority_ssm"),
            ("ADP_CHAT_CREW_IMAGE_DIGESTS", "chat-crew-image-digests", "get_authority_ssm"),
        ):
            self.assertIn(f'{key}: "__{key}__"', configmap)
            for renderer in (SOURCE, workflow):
                self.assertIn(f'{key}_RAW=$({helper} "/adp/${{ENVIRONMENT}}/gateway/{parameter}" "")', renderer)
                self.assertIn(f"s|__{key}__|${{{key}_SED}}|g", renderer)
        self.assertIn('ADP_CHAT_WORKER_SERVICE_ACCOUNT: "__ADP_CHAT_WORKER_SERVICE_ACCOUNT__"', configmap)
        self.assertNotIn('ADP_CHAT_WORKER_SERVICE_ACCOUNT: "adp-agent"', configmap)
        self.assertIn('ADP_CHAT_WORKER_SERVICE_ACCOUNT=$(_get_ssm "/adp/${ENVIRONMENT}/gateway/chat-worker-service-account" "adp-agent")', SOURCE)
        self.assertIn('ADP_CHAT_WORKER_SERVICE_ACCOUNT=$(get_ssm "/adp/${ENVIRONMENT}/gateway/chat-worker-service-account" "adp-agent")', workflow)
        for renderer in (SOURCE, workflow):
            self.assertIn("s|__ADP_CHAT_WORKER_SERVICE_ACCOUNT__|${ADP_CHAT_WORKER_SERVICE_ACCOUNT}|g", renderer)
            self.assertIn('=~ ^(adp-agent|adp-chat-supervisor)$ ]] || ADP_CHAT_WORKER_SERVICE_ACCOUNT="adp-agent"', renderer)

    def test_terraform_seeds_the_parameters_and_never_reverts_published_values(self):
        tf = (ROOT / "modules/gateway/infra/chief-of-staff-flags.tf").read_text()
        for resource, name in (
            ("chat_authority_worker_images", "chat-authority-worker-images"),
            ("chat_sandbox_image_digests", "chat-sandbox-image-digests"),
            ("chat_crew_image_digests", "chat-crew-image-digests"),
            ("chat_worker_service_account", "chat-worker-service-account"),
            ("chat_persistent_executor", "chat-persistent-executor"),
        ):
            block = tf[tf.index(f'resource "aws_ssm_parameter" "{resource}"'):]
            block = block[:block.index("\n}\n") + 3]
            self.assertIn(f'name  = "/adp/${{var.environment}}/gateway/{name}"', block)
            self.assertIn("ignore_changes = [value]", block)
            self.assertIn(f'"aws_ssm_parameter.{resource}=/adp/${{ENVIRONMENT}}/gateway/{name}"', SOURCE)
        self.assertIn('value = "adp-agent"', tf)

    def test_blank_ssm_seed_renders_as_no_digests(self):
        renderer = ROOT / "platform/scripts/render-model-root-config.py"
        for raw in (" ", "", "\n"):
            result = subprocess.run(["python3", str(renderer), "images"], input=raw, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")
        digests = f"sha256:{'a' * 64},sha256:{'b' * 64}"
        result = subprocess.run(["python3", str(renderer), "images"], input=digests, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), digests)


class JournalTests(unittest.TestCase):
    def test_mode_and_record_are_written_to_the_checkpoint_journal(self):
        init = SOURCE.index("deploy_checkpoint_init\n")
        self.assertIn('deploy_checkpoint note chat-mode "$CHAT_MODE"', SOURCE[init:init + 400])
        self.assertIn('deploy_checkpoint note chat-exception "$CHAT_EXCEPTION"', SOURCE[init:init + 500])
        checkpoints = ROOT / "platform/scripts/deploy-checkpoints.py"
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({"version": 1, "binding": {}, "phases": {}, "status": "running"}))
            for key, value in (("chat-mode", "delegated"), ("chat-exception", "#6937 evidence 1")):
                result = subprocess.run(["python3", str(checkpoints), str(state), "note", key, value],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            notes = json.loads(state.read_text())["notes"]
            self.assertEqual(notes, {"chat-mode": "delegated", "chat-exception": "#6937 evidence 1"})
            bad = subprocess.run(["python3", str(checkpoints), str(state), "note", "Bad Key", "x"],
                                 capture_output=True, text=True)
            self.assertEqual(bad.returncode, 2)
        # The mode is part of the resume binding so a resumed run cannot change it.
        helper = (ROOT / "platform/scripts/deploy-checkpoints.sh").read_text()
        self.assertIn("${CHAT_MODE:-retire}:${CHAT_EXCEPTION:-}", helper)


class ScaledJobGuardTests(unittest.TestCase):
    def test_environment_guard_accepts_any_environment_name_but_only_the_reviewed_namespace(self):
        script = ROOT / "modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh"
        source = script.read_text()
        self.assertNotIn('"$ENVIRONMENT" == dev', source)
        self.assertIn('[[ "$ENVIRONMENT" =~ ^[a-z][a-z0-9-]{1,20}$ && "$NAMESPACE" == adp-gateway-agents ]]', source)
        # Execute only the guard lines with the remaining checks still in place.
        start = source.index('NAMESPACE="${NAMESPACE:-adp-gateway-agents}"')
        end = source.index(': "${SUPERVISOR_IMAGE:?')
        guard = "set -euo pipefail\n" + source[start:end] + 'echo guard-passed\n'
        for environment, namespace, accepted in (
            ("dev", "adp-gateway-agents", True),
            ("staging", "adp-gateway-agents", True),
            ("customer-prod", "adp-gateway-agents", True),
            ("Dev", "adp-gateway-agents", False),
            ("dev", "other", False),
            ("d", "adp-gateway-agents", False),
        ):
            with self.subTest(environment=environment, namespace=namespace):
                result = subprocess.run(["bash", "-c", guard], capture_output=True, text=True, env={
                    **os.environ, "ENVIRONMENT": environment, "NAMESPACE": namespace,
                    "ADP_CHAT_EXISTING_NAMESPACE_ONLY": "true", "ADP_CHAT_MODEL_POLICY_ENABLED": "true",
                })
                self.assertEqual(result.returncode == 0, accepted, result.stderr)
        # Every other precondition still refuses.
        result = subprocess.run(["bash", "-c", guard], capture_output=True, text=True, env={
            **os.environ, "ENVIRONMENT": "staging", "ADP_CHAT_EXISTING_NAMESPACE_ONLY": "true",
            "ADP_CHAT_MODEL_POLICY_ENABLED": "false",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("model policy must be enabled", result.stderr)



class ScaledJobSsmResolutionTests(unittest.TestCase):
    """deploy-chat-scaledjob.sh resolves the SSM-backed Crew values itself (#8058).

    The helper functions are extracted from the script and run with an `aws` shell double,
    so the contract — env wins when set, otherwise SSM; `None`/blank mean unset; invalid
    shapes fail with a named message; the source is logged — is proven without a cluster.
    """

    def helpers(self):
        start = SCALEDJOB_SCRIPT.index("_chat_normalise() {")
        end = SCALEDJOB_SCRIPT.index('if [[ "${ADP_CHAT_CREW_CONTROLS_ONLY:-false}" == true ]]; then')
        return SCALEDJOB_SCRIPT[start:end]

    def resolve(self, body, ssm=None, env=None):
        cases = "".join(f'    "{name}") printf "%s\\n" "{value}" ;;\n' for name, value in (ssm or {}).items())
        script = f"""set -euo pipefail
ENVIRONMENT=dev; AWS_REGION=us-east-1
aws() {{
  echo "aws $*" >> "$CALLS"
  [[ "$1 $2 $3" == "ssm get-parameter --name" ]] || {{ echo "unexpected aws $*" >&2; exit 9; }}
  case "$4" in
{cases}    *) echo "An error occurred (${{MOCK_SSM_ERROR:-ParameterNotFound}}) when calling the GetParameter operation: $4" >&2; return 254 ;;
  esac
}}
{self.helpers()}
{body}
"""
        child = {k: v for k, v in os.environ.items() if not k.startswith("ADP_CREW_") and k != "ADP_CHAT_PERSISTENT_EXECUTOR"}
        child.update(env or {})
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / "calls"
            calls.touch()
            child["CALLS"] = str(calls)
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=child)
            return result, calls.read_text()

    ORIGIN = "resolve_chat_value ADP_CREW_DASHBOARD_ORIGIN crew-dashboard-origin\nvalidate_crew_dashboard_origin\n" \
             'bash -c \'printf "child=%s\\n" "$ADP_CREW_DASHBOARD_ORIGIN"\'\n'
    S3_IP = "resolve_chat_value ADP_CREW_S3_INTERFACE_IP crew-s3-interface-ip\nvalidate_crew_s3_interface_ip\n" \
            'bash -c \'printf "child=%s\\n" "$ADP_CREW_S3_INTERFACE_IP"\'\n'
    EXECUTOR = "resolve_chat_value ADP_CHAT_PERSISTENT_EXECUTOR chat-persistent-executor\nvalidate_chat_persistent_executor\n" \
               'bash -c \'printf "child=%s\\n" "$ADP_CHAT_PERSISTENT_EXECUTOR"\'\n'

    def test_unset_variable_is_resolved_from_ssm_and_exported(self):
        result, calls = self.resolve(self.ORIGIN, ssm={"/adp/dev/gateway/crew-dashboard-origin": "https://d19slsojnl7bhj.cloudfront.net"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[deploy-chat] ADP_CREW_DASHBOARD_ORIGIN from ssm /adp/dev/gateway/crew-dashboard-origin: https://d19slsojnl7bhj.cloudfront.net",
                      result.stdout)
        self.assertIn("child=https://d19slsojnl7bhj.cloudfront.net", result.stdout)
        self.assertIn("aws ssm get-parameter --name /adp/dev/gateway/crew-dashboard-origin --query Parameter.Value --output text --region us-east-1", calls)

    def test_set_variable_wins_without_reading_ssm(self):
        result, calls = self.resolve(self.ORIGIN, ssm={"/adp/dev/gateway/crew-dashboard-origin": "https://other.example.test"},
                                     env={"ADP_CREW_DASHBOARD_ORIGIN": "https://crew.example.test"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[deploy-chat] ADP_CREW_DASHBOARD_ORIGIN from env: https://crew.example.test", result.stdout)
        self.assertIn("child=https://crew.example.test", result.stdout)
        self.assertEqual(calls, "")

    def test_none_blank_and_missing_values_resolve_to_empty(self):
        for label, ssm, env in (
            ("None", {"/adp/dev/gateway/crew-dashboard-origin": "None"}, {}),
            ("blank", {"/adp/dev/gateway/crew-dashboard-origin": " "}, {}),
            ("missing", {}, {}),
            ("env blank falls back to a missing parameter", {}, {"ADP_CREW_DASHBOARD_ORIGIN": " "}),
            ("env None falls back", {}, {"ADP_CREW_DASHBOARD_ORIGIN": "None"}),
        ):
            with self.subTest(label=label):
                result, calls = self.resolve(self.ORIGIN, ssm=ssm, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                suffix = " (ParameterNotFound)" if not ssm else ""
                self.assertIn(f"[deploy-chat] ADP_CREW_DASHBOARD_ORIGIN from ssm /adp/dev/gateway/crew-dashboard-origin{suffix}: <empty>", result.stdout)
                self.assertIn("child=\n", result.stdout)
                self.assertIn("crew-dashboard-origin", calls)

    def test_a_failed_ssm_read_stops_the_rollout_instead_of_meaning_empty(self):
        """dev 2026-10-17: the ADP-managed identity lacked ssm:GetParameter, the read was treated
        as empty and the ScaledJob was applied with a blank Crew dashboard origin (#8058)."""
        for code in ("AccessDeniedException", "ThrottlingException", "UnrecognizedClientException"):
            for body in (self.ORIGIN, self.S3_IP):
                with self.subTest(code=code, body=body.split()[1]):
                    result, calls = self.resolve(body, env={"MOCK_SSM_ERROR": code})
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn(f"failed ({code}); refusing to treat the value as empty", result.stderr)
                    self.assertIn("An error occurred", result.stderr)
                    self.assertNotIn("[deploy-chat]", result.stdout)
                    self.assertNotIn("child=", result.stdout)
                    self.assertIn("crew-", calls)
        # A failure without an AWS error code (network, missing CLI) is reported too.
        script = "aws() { echo 'Could not connect to the endpoint URL' >&2; return 255; }\n" + self.ORIGIN
        result, _ = self.resolve(script)
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed (no AWS error code); refusing to treat the value as empty: Could not connect", result.stderr)
        # The env value still wins without any read.
        result, calls = self.resolve(self.ORIGIN, env={"MOCK_SSM_ERROR": "AccessDeniedException", "ADP_CREW_DASHBOARD_ORIGIN": "https://crew.example.test"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, "")

    def test_the_adp_managed_chat_deploy_identity_may_read_the_crew_parameters(self):
        policy = (ROOT / "platform/automation-infra/chat-deployment.tf").read_text()
        statement = policy[policy.index('Action = ["ssm:GetParameter"]'):]
        statement = statement[:statement.index("] },")]
        self.assertIn("for parameter in local.chat_deployment_read_parameters", statement)
        self.assertIn("parameter/adp/${var.environment}/gateway/${parameter}", statement)
        locals_block = policy[policy.index("chat_deployment_read_parameters = concat("):]
        locals_block = locals_block[:locals_block.index("\n  )")]
        for parameter in ("apigw-invoke-url", "crew-dashboard-origin", "crew-s3-interface-ip", "chat-persistent-executor"):
            self.assertIn(f'"{parameter}"', locals_block)
        self.assertIn("local.chat_deployment_publish_parameters", locals_block)
        for parameter in ("chat-worker-service-account", "chat-data-enabled"):
            self.assertIn(f'"{parameter}"', locals_block)

    def test_invalid_dashboard_origin_fails_with_a_named_message_from_either_source(self):
        for value in ("http://crew.example.test", "https://crew.example.test/", "https://crew.example.test:8443",
                      "https://crew.example.test/path", "123456789012", "https://Crew.example.test", "crew.example.test"):
            for source in ("ssm", "env"):
                with self.subTest(value=value, source=source):
                    ssm = {"/adp/dev/gateway/crew-dashboard-origin": value} if source == "ssm" else {}
                    env = {"ADP_CREW_DASHBOARD_ORIGIN": value} if source == "env" else {}
                    result, _ = self.resolve(self.ORIGIN, ssm=ssm, env=env)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn("Crew dashboard origin must be an https origin", result.stderr)
                    self.assertNotIn("child=", result.stdout)

    def test_s3_interface_ip_is_resolved_and_must_be_private_ipv4(self):
        result, _ = self.resolve(self.S3_IP, ssm={"/adp/dev/gateway/crew-s3-interface-ip": "10.0.12.34"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[deploy-chat] ADP_CREW_S3_INTERFACE_IP from ssm /adp/dev/gateway/crew-s3-interface-ip: 10.0.12.34", result.stdout)
        self.assertIn("child=10.0.12.34", result.stdout)
        result, calls = self.resolve(self.S3_IP, env={"ADP_CREW_S3_INTERFACE_IP": "172.16.0.5"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("from env: 172.16.0.5", result.stdout)
        self.assertEqual(calls, "")
        result, _ = self.resolve(self.S3_IP, ssm={"/adp/dev/gateway/crew-s3-interface-ip": "None"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("child=\n", result.stdout)
        for value in ("8.8.8.8", "not-an-ip", "fd00::1", "169.254.169.254", "100.64.0.1"):
            with self.subTest(value=value):
                result, _ = self.resolve(self.S3_IP, ssm={"/adp/dev/gateway/crew-s3-interface-ip": value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Crew S3 interface IP must be a private IPv4 address", result.stderr)

    def test_persistent_executor_default_is_resolved_from_ssm_validated_and_exported(self):
        """#8115: the deployment default for the persistent-session executor follows the same
        fail-closed SSM contract as the Crew values; empty means the renderer's sandbox-loop."""
        result, calls = self.resolve(self.EXECUTOR, ssm={"/adp/dev/gateway/chat-persistent-executor": "claude-code"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[deploy-chat] ADP_CHAT_PERSISTENT_EXECUTOR from ssm /adp/dev/gateway/chat-persistent-executor: claude-code", result.stdout)
        self.assertIn("child=claude-code", result.stdout)
        self.assertIn("aws ssm get-parameter --name /adp/dev/gateway/chat-persistent-executor --query Parameter.Value --output text --region us-east-1", calls)
        result, calls = self.resolve(self.EXECUTOR, ssm={"/adp/dev/gateway/chat-persistent-executor": "claude-code"},
                                     env={"ADP_CHAT_PERSISTENT_EXECUTOR": "kirocrew"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("from env: kirocrew", result.stdout)
        self.assertEqual(calls, "")
        for label, ssm, env in (("missing", {}, {}), ("None", {"/adp/dev/gateway/chat-persistent-executor": "None"}, {}),
                                ("blank", {"/adp/dev/gateway/chat-persistent-executor": " "}, {})):
            with self.subTest(label=label):
                result, _ = self.resolve(self.EXECUTOR, ssm=ssm, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("child=\n", result.stdout)
        for value in ("claude", "Claude-Code", "sandbox-loop;rm", "codex"):
            for source in ("ssm", "env"):
                with self.subTest(value=value, source=source):
                    ssm = {"/adp/dev/gateway/chat-persistent-executor": value} if source == "ssm" else {}
                    env = {"ADP_CHAT_PERSISTENT_EXECUTOR": value} if source == "env" else {}
                    result, _ = self.resolve(self.EXECUTOR, ssm=ssm, env=env)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn("Persistent chat executor must be sandbox-loop, claude-code or kirocrew", result.stderr)
                    self.assertNotIn("child=", result.stdout)
        # A failed read (not ParameterNotFound) stops the rollout instead of rendering sandbox-loop.
        result, _ = self.resolve(self.EXECUTOR, env={"MOCK_SSM_ERROR": "AccessDeniedException"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to treat the value as empty", result.stderr)

    def test_script_resolves_exactly_the_parameters_deploy_all_sources_for_it(self):
        """Parity (#7898, #8058): the SSM keys the script resolves are the ones deploy-all.sh
        --chat delegated reads and passes; the ADP-managed workflow passes none of them."""
        stage = SOURCE[SOURCE.index('step "Step 10c/11: Delegated chat rollout'):SOURCE.index('ok "Delegated chat ScaledJob is Ready')]
        passed = set(re.findall(r'(ADP_CREW_[A-Z0-9_]+)=\$\(_chat_ssm "/adp/\$\{ENVIRONMENT\}/gateway/([a-z0-9-]+)"\)', stage))
        resolved = set(re.findall(r"resolve_chat_value (ADP_CREW_[A-Z0-9_]+) ([a-z0-9-]+)", SCALEDJOB_SCRIPT))
        self.assertEqual(resolved, {("ADP_CREW_DASHBOARD_ORIGIN", "crew-dashboard-origin"), ("ADP_CREW_S3_INTERFACE_IP", "crew-s3-interface-ip")})
        self.assertEqual(passed, resolved)
        self.assertIn('parameter="/adp/${ENVIRONMENT}/gateway/$2"', SCALEDJOB_SCRIPT)
        for name, _ in resolved:
            self.assertIn(f'{name}="${name}"', stage, f"deploy-all.sh passes {name} to the script")
        workflow = yaml.safe_load((ROOT / ".github/workflows/chat-agent-deploy.yml").read_text())
        steps = {step.get("name"): step for step in workflow["jobs"]["build-and-deploy"]["steps"]}
        update = steps["Update chat ScaledJob manifest"]
        self.assertNotIn("ADP_CREW_", json.dumps(update.get("env", {})))
        self.assertIsNone(re.search(r"^\s*(export\s+)?ADP_CREW_", update["run"], re.M), "the workflow exports no Crew value")
        # Full mode resolves the dashboard origin only after the model verify, never before the image checks.
        full = SCALEDJOB_SCRIPT[SCALEDJOB_SCRIPT.index('NAMESPACE="${NAMESPACE:-adp-gateway-agents}"'):]
        self.assertLess(full.index("enable-bedrock-models.sh"), full.index("resolve_chat_value ADP_CREW_DASHBOARD_ORIGIN"))
        self.assertLess(full.index("resolve_chat_value ADP_CREW_DASHBOARD_ORIGIN"), full.index("render-chat-supervisor-job.mjs"))
        # The explicit sandbox start deadline is rendered with its default (#8059).
        self.assertIn('export ADP_CHAT_SANDBOX_START_DEADLINE_MS="${ADP_CHAT_SANDBOX_START_DEADLINE_MS:-120000}"', full)
        # The persistent-executor default (#8115): resolved from SSM after the model verify, rendered
        # into the ScaledJob, passed by deploy-all.sh from the same parameter (an operator export wins)
        # and published back only when the caller named it; the ADP-managed workflow passes none.
        resolve = full.index("resolve_chat_value ADP_CHAT_PERSISTENT_EXECUTOR chat-persistent-executor")
        self.assertLess(full.index("enable-bedrock-models.sh"), resolve)
        self.assertLess(resolve, full.index("validate_chat_persistent_executor\n"))
        self.assertLess(resolve, full.index("render-chat-supervisor-job.mjs"))
        self.assertLess(full.index('PERSISTENT_EXECUTOR_REQUESTED="$(_chat_normalise "${ADP_CHAT_PERSISTENT_EXECUTOR:-}")"'), resolve)
        self.assertIn("ADP_CREW_DASHBOARD_ORIGIN ADP_CHAT_PERSISTENT_EXECUTOR\n", full[resolve:full.index("render-chat-supervisor-job.mjs")])
        publish = full.index('publish_chat_value chat-persistent-executor "$PERSISTENT_EXECUTOR_REQUESTED"')
        self.assertLess(full.index("publish_chat_value chat-data-enabled true"), publish)
        self.assertIn('if [[ -n "$PERSISTENT_EXECUTOR_REQUESTED" ]]; then\n  publish_chat_value chat-persistent-executor', full)
        self.assertIn('ADP_CHAT_PERSISTENT_EXECUTOR="${ADP_CHAT_PERSISTENT_EXECUTOR:-$(_chat_ssm "/adp/${ENVIRONMENT}/gateway/chat-persistent-executor")}"', stage)
        self.assertIn('ADP_CHAT_PERSISTENT_EXECUTOR="$ADP_CHAT_PERSISTENT_EXECUTOR"', stage)
        self.assertNotIn("ADP_CHAT_PERSISTENT_EXECUTOR", json.dumps(update.get("env", {})))
        self.assertIsNone(re.search(r"^\s*(export\s+)?ADP_CHAT_PERSISTENT_EXECUTOR", update["run"], re.M))
        template = yaml.safe_load((K8S / "chat-supervisor-job.yaml").read_text())
        env = {entry["name"]: entry["value"] for entry in template["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["ADP_CHAT_PERSISTENT_EXECUTOR"], "REPLACE_WITH_PERSISTENT_EXECUTOR")


class ChatDigestPublicationTests(unittest.TestCase):
    """deploy-chat-scaledjob.sh publishes the digests the gateway verifies chat workloads against and
    converges the live gateway ConfigMap right after the ScaledJob apply (#8086), idempotently and
    fail-closed. publish_chat_value and the ConfigMap comparison are extracted and run with doubles."""

    def helpers(self):
        start = SCALEDJOB_SCRIPT.index("_chat_normalise() {")
        end = SCALEDJOB_SCRIPT.index('if [[ "${ADP_CHAT_CREW_CONTROLS_ONLY:-false}" == true ]]; then')
        return SCALEDJOB_SCRIPT[start:end]

    def publish(self, body, ssm=None, env=None):
        cases = "".join(f'    "{name}") printf "%s\\n" "{value}" ;;\n' for name, value in (ssm or {}).items())
        script = f"""set -euo pipefail
ENVIRONMENT=dev; AWS_REGION=us-east-1
aws() {{
  echo "aws $*" >> "$CALLS"
  if [[ "$1 $2" == "ssm put-parameter" ]]; then
    [[ -z "${{MOCK_PUT_ERROR:-}}" ]] || {{ echo "An error occurred ($MOCK_PUT_ERROR) when calling the PutParameter operation: $4" >&2; return 254; }}
    return 0
  fi
  [[ "$1 $2 $3" == "ssm get-parameter --name" ]] || {{ echo "unexpected aws $*" >&2; exit 9; }}
  case "$4" in
{cases}    *) echo "An error occurred (${{MOCK_SSM_ERROR:-ParameterNotFound}}) when calling the GetParameter operation: $4" >&2; return 254 ;;
  esac
}}
{self.helpers()}
{body}
echo publish-done
"""
        child = dict(os.environ)
        child.update(env or {})
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / "calls"
            calls.touch()
            child["CALLS"] = str(calls)
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=child)
            return result, calls.read_text()

    PUT = ("aws ssm put-parameter --name /adp/dev/gateway/chat-sandbox-image-digests --type String "
           f"--value {SANDBOX_DIGEST} --overwrite --region us-east-1")
    BODY = f'publish_chat_value chat-sandbox-image-digests "{SANDBOX_DIGEST}"\n'

    def test_a_parameter_already_holding_the_digest_is_not_rewritten(self):
        result, calls = self.publish(self.BODY, ssm={"/adp/dev/gateway/chat-sandbox-image-digests": SANDBOX_DIGEST})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"[deploy-chat] /adp/dev/gateway/chat-sandbox-image-digests already holds {SANDBOX_DIGEST}", result.stdout)
        self.assertIn("publish-done", result.stdout)
        self.assertNotIn("put-parameter", calls)

    def test_a_changed_or_unpublished_digest_is_published_once(self):
        for label, ssm, was in (
            ("previous build", {"/adp/dev/gateway/chat-sandbox-image-digests": STALE_DIGEST}, STALE_DIGEST),
            ("never published (ParameterNotFound)", {}, "<empty>"),
            ("blank seed", {"/adp/dev/gateway/chat-sandbox-image-digests": " "}, "<empty>"),
            ("None seed", {"/adp/dev/gateway/chat-sandbox-image-digests": "None"}, "<empty>"),
        ):
            with self.subTest(label=label):
                result, calls = self.publish(self.BODY, ssm=ssm)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"[deploy-chat] published /adp/dev/gateway/chat-sandbox-image-digests={SANDBOX_DIGEST} (was {was})", result.stdout)
                self.assertEqual(calls.count("put-parameter"), 1)
                self.assertIn(self.PUT, calls)
                self.assertLess(calls.index("get-parameter"), calls.index("put-parameter"))

    def test_a_failed_read_or_put_stops_the_rollout(self):
        for code in ("AccessDeniedException", "ThrottlingException"):
            with self.subTest(read=code):
                result, calls = self.publish(self.BODY, env={"MOCK_SSM_ERROR": code})
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"SSM read of /adp/dev/gateway/chat-sandbox-image-digests failed ({code}); refusing to publish over an unknown value", result.stderr)
                self.assertNotIn("put-parameter", calls)
                self.assertNotIn("publish-done", result.stdout)
            with self.subTest(put=code):
                result, calls = self.publish(self.BODY, ssm={"/adp/dev/gateway/chat-sandbox-image-digests": STALE_DIGEST}, env={"MOCK_PUT_ERROR": code})
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"SSM publication of /adp/dev/gateway/chat-sandbox-image-digests={SANDBOX_DIGEST} failed ({code})", result.stderr)
                self.assertIn("would keep refusing the chat workloads this rollout pinned", result.stderr)
                self.assertIn("put-parameter", calls)
                self.assertNotIn("publish-done", result.stdout)

    def converge(self, live_data, desired=None):
        start = SCALEDJOB_SCRIPT.index("<<'PYCODE'", SCALEDJOB_SCRIPT.index('python3 - "$WORK/gateway-config.json"')) + len("<<'PYCODE'\n")
        code = SCALEDJOB_SCRIPT[start:SCALEDJOB_SCRIPT.index("\nPYCODE\n", start)]
        desired = desired if desired is not None else {
            "ADP_CHAT_SANDBOX_CA_CONFIGMAP": "chat-sandbox-gateway-ca-0123456789abcdef",
            "ADP_CHAT_DATA_URL": "https://chat-sandbox-gateway.adp-gateway.svc:8443",
            "ADP_CHAT_WORKER_IMAGE_DIGESTS": SUPERVISOR_DIGEST, "ADP_CHAT_WORKER_SERVICE_ACCOUNT": "adp-chat-supervisor",
            "ADP_CHAT_SANDBOX_IMAGE_DIGESTS": SANDBOX_DIGEST, "ADP_CHAT_CREW_IMAGE_DIGESTS": SANDBOX_DIGEST,
            "ADP_CHAT_DATA_ENABLED": "true",
        }
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "gateway-config.json"
            live.write_text(json.dumps({"kind": "ConfigMap", "data": live_data} if live_data is not None else {"kind": "ConfigMap"}))
            return subprocess.run(["python3", "-", str(live)], input=code, capture_output=True, text=True,
                                  env={**os.environ, "CHAT_GATEWAY_DESIRED": json.dumps(desired)}), desired

    def test_configmap_is_patched_only_when_a_release_key_differs(self):
        result, desired = self.converge({**{"BG_OTHER": "kept"}, **{
            "ADP_CHAT_SANDBOX_CA_CONFIGMAP": "chat-sandbox-gateway-ca-0123456789abcdef",
            "ADP_CHAT_DATA_URL": "https://chat-sandbox-gateway.adp-gateway.svc:8443",
            "ADP_CHAT_WORKER_IMAGE_DIGESTS": SUPERVISOR_DIGEST, "ADP_CHAT_WORKER_SERVICE_ACCOUNT": "adp-chat-supervisor",
            "ADP_CHAT_SANDBOX_IMAGE_DIGESTS": SANDBOX_DIGEST, "ADP_CHAT_CREW_IMAGE_DIGESTS": SANDBOX_DIGEST,
            "ADP_CHAT_DATA_ENABLED": "true"}})
        self.assertEqual(result.returncode, 0, result.stderr)
        stale = dict(desired, ADP_CHAT_WORKER_IMAGE_DIGESTS=STALE_DIGEST, ADP_CHAT_SANDBOX_IMAGE_DIGESTS=STALE_DIGEST)
        result, _ = self.converge(stale)
        self.assertEqual(result.returncode, 1)
        self.assertIn("[deploy-chat] bedrockgateway-config differs on: ADP_CHAT_SANDBOX_IMAGE_DIGESTS ADP_CHAT_WORKER_IMAGE_DIGESTS", result.stdout)
        result, _ = self.converge(None)
        self.assertEqual(result.returncode, 1, "a ConfigMap without data is converged, not assumed")
        # The patch carries exactly the keys the renderers also read from SSM (configmap.yaml parity).
        self.assertEqual(set(desired), set(WorkloadIdentityPublicationTests.GATEWAY_KEYS))
        for key in desired:
            self.assertIn(f'{key}: "__{key}__"', (ROOT / "modules/gateway/k8s/configmap.yaml").read_text())

    def test_the_adp_managed_identity_gets_minimal_put_and_gateway_converge_grants(self):
        tf = (ROOT / "platform/automation-infra/chat-deployment.tf").read_text()
        self.assertIn('chat_deployment_publish_parameters = ["chat-authority-worker-images", "chat-sandbox-image-digests", "chat-crew-image-digests"]', tf)
        put = tf[tf.index('Action = ["ssm:PutParameter"]'):]
        put = put[:put.index("] },")]
        self.assertIn("for parameter in local.chat_deployment_publish_parameters", put)
        self.assertEqual(tf.count("ssm:PutParameter"), 1)
        self.assertIn('kubernetes_groups = ["adp:chat-release-read", "adp:chat-release-publish"]', tf)
        documents = list(yaml.safe_load_all((ROOT / "platform/automation-infra/chat-release-publish-rbac.yaml").read_text()))
        self.assertEqual([d["kind"] for d in documents], ["Role", "RoleBinding"])
        role, binding = documents
        self.assertEqual(role["metadata"], {"name": "adp-chat-release-publish", "namespace": "adp-gateway"})
        self.assertEqual(role["rules"], [
            {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["bedrockgateway-config"], "verbs": ["get", "patch"]},
            {"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": ["bedrockgateway"], "verbs": ["get", "patch", "watch", "list"]},
        ])
        self.assertEqual(binding["metadata"]["namespace"], "adp-gateway")
        self.assertEqual(binding["subjects"], [{"kind": "Group", "name": "adp:chat-release-publish"}])
        self.assertEqual(binding["roleRef"]["name"], "adp-chat-release-publish")
        # The read group stays get-only (test_chat_release_controls.py); the write grant is a separate manifest.
        self.assertNotIn("patch", (ROOT / "platform/automation-infra/chat-release-read-rbac.yaml").read_text())
        readme = (ROOT / "platform/automation-infra/README.md").read_text()
        self.assertIn("chat-release-publish-rbac.yaml", readme)


class PrepullOwnershipTests(unittest.TestCase):
    """The pre-pull DaemonSet is rendered and waited for by deploy-chat-scaledjob.sh before the
    ScaledJob, on both deploy paths; deploy-all.sh applies it itself only outside delegated
    mode (#8059)."""

    def test_script_prepulls_both_digests_and_waits_before_the_scaledjob(self):
        full = SCALEDJOB_SCRIPT[SCALEDJOB_SCRIPT.index('NAMESPACE="${NAMESPACE:-adp-gateway-agents}"'):]
        role_check = full.index("Unexpected chat queue polling role")
        admission = full.index('kubectl apply -f "${SCRIPT_DIR}/chat-sandbox-admission.yaml"')
        prepull = full.index('kubectl apply -f "$WORK/prepull.json"')
        wait = full.index('kubectl rollout status daemonset/chat-agent-image-prepull -n "$NAMESPACE" --timeout="$PREPULL_TIMEOUT"')
        verify = full.index("Pre-pull DaemonSet does not carry the release digests")
        supervisor = full.index('kubectl apply -f "$WORK/supervisor.yaml"')
        objects = full.index('kubectl apply -f "$WORK/objects.json"')
        self.assertLess(role_check, admission)
        self.assertLess(admission, prepull)
        self.assertLess(prepull, wait)
        self.assertLess(wait, verify)
        self.assertLess(verify, supervisor)
        self.assertLess(supervisor, objects)
        self.assertIn('PREPULL_TIMEOUT="${ADP_CHAT_PREPULL_TIMEOUT:-600s}"', SCALEDJOB_SCRIPT)

    def render_prepull(self, supervisor, sandbox, manifest=None):
        start = SCALEDJOB_SCRIPT.index("<<'PYCODE'", SCALEDJOB_SCRIPT.index('python3 - "$WORK/prepull-base.json"')) + len("<<'PYCODE'\n")
        code = SCALEDJOB_SCRIPT[start:SCALEDJOB_SCRIPT.index("\nPYCODE\n", start)]
        manifest = manifest if manifest is not None else yaml.safe_load(
            (K8S / "image-prepull-daemonset.yaml").read_text().replace("REPLACE_WITH_AGENT_IMAGE", supervisor))
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "prepull-base.json"
            base.write_text(json.dumps(manifest))
            return subprocess.run(["python3", "-", str(base), supervisor, sandbox], input=code, capture_output=True, text=True)

    def test_manifest_declares_one_container_and_the_script_adds_the_sandbox_digest(self):
        manifest = yaml.safe_load((K8S / "image-prepull-daemonset.yaml").read_text())
        containers = manifest["spec"]["template"]["spec"]["containers"]
        self.assertEqual([(c["name"], c["image"]) for c in containers], [("prepull", "REPLACE_WITH_AGENT_IMAGE")])
        result = self.render_prepull(SUPERVISOR, SANDBOX)
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = json.loads(result.stdout)["spec"]["template"]["spec"]["containers"]
        self.assertEqual([(c["name"], c["image"]) for c in rendered], [("prepull", SUPERVISOR), ("prepull-sandbox", SANDBOX)])
        self.assertEqual(rendered[0]["securityContext"], rendered[1]["securityContext"])
        self.assertEqual(rendered[0]["command"], rendered[1]["command"])
        self.assertEqual(rendered[0]["resources"], rendered[1]["resources"])
        # A manifest that drifted from the one-container contract is refused rather than guessed at.
        drifted = yaml.safe_load((K8S / "image-prepull-daemonset.yaml").read_text().replace("REPLACE_WITH_AGENT_IMAGE", SUPERVISOR))
        drifted["spec"]["template"]["spec"]["containers"].append({"name": "extra", "image": SANDBOX})
        self.assertNotEqual(self.render_prepull(SUPERVISOR, SANDBOX, drifted).returncode, 0)
        base = yaml.safe_load((K8S / "image-prepull-daemonset.yaml").read_text().replace("REPLACE_WITH_AGENT_IMAGE", SUPERVISOR))
        self.assertNotEqual(self.render_prepull(SANDBOX, SUPERVISOR, base).returncode, 0, "the base image must be the supervisor digest")

    def test_deploy_all_applies_the_daemonset_only_outside_delegated_mode(self):
        self.assertEqual(SOURCE.count("image-prepull-daemonset.yaml"), 1)
        stage = SOURCE[SOURCE.index('step "Step 10c/11: Delegated chat rollout'):SOURCE.index('ok "Delegated chat ScaledJob is Ready')]
        self.assertNotIn("image-prepull-daemonset.yaml", stage)
        self.assertNotIn("rollout status daemonset", stage)
        delegated = SOURCE.index('step "Step 10c/11: Delegated chat images cached by deploy-chat-scaledjob.sh"')
        legacy = SOURCE.index('step "Step 10c/11: Cache chat release image (delegated chat not selected)"')
        apply = SOURCE.index("image-prepull-daemonset.yaml")
        wait = SOURCE.index("kubectl rollout status daemonset/chat-agent-image-prepull -n adp-gateway-agents --timeout=600s")
        branch_end = SOURCE.index("\n  fi\n", legacy)
        self.assertLess(delegated, legacy)
        self.assertLess(legacy, apply)
        self.assertLess(apply, wait)
        self.assertLess(wait, branch_end, "the DaemonSet apply sits inside the non-delegated branch")
        self.assertLess(branch_end, SOURCE.index('case "$CHAT_MODE" in\n    delegated) ok "Delegated chat is live'))


class VerifyChatRolloutTests(unittest.TestCase):
    """verify-chat-rollout.sh judges the live ScaledJob and DaemonSet against SSM and the
    release independently of the deploy script (chat-agent-deploy.yml drift step)."""

    SCRIPT = K8S / "verify-chat-rollout.sh"

    def live(self, crew_origin="https://crew.example.test", sandbox_env=SANDBOX, prepull_sandbox=SANDBOX, prepull=SUPERVISOR,
             deadline="120000", ready="True", updated=3, cm_worker=SUPERVISOR_DIGEST, cm_sandbox=SANDBOX_DIGEST,
             cm_crew=SANDBOX_DIGEST, cm_service_account="adp-chat-supervisor", executor="sandbox-loop"):
        scaledjob = {"kind": "ScaledJob", "spec": {"jobTargetRef": {"template": {"spec": {
            "serviceAccountName": "adp-chat-supervisor",
            "containers": [{"name": "supervisor", "image": SUPERVISOR, "env": [
                {"name": "ADP_CHAT_SANDBOX_IMAGE", "value": sandbox_env},
                {"name": "ADP_CREW_DASHBOARD_ORIGIN", "value": crew_origin},
                {"name": "ADP_CHAT_SANDBOX_START_DEADLINE_MS", "value": deadline},
                {"name": "ADP_CHAT_PERSISTENT_EXECUTOR", "value": executor},
            ]}]}}}}, "status": {"conditions": [{"type": "Ready", "status": ready}]}}
        daemonset = {"kind": "DaemonSet", "spec": {"template": {"spec": {"containers": [
            {"name": "prepull", "image": prepull}, {"name": "prepull-sandbox", "image": prepull_sandbox}]}}},
            "status": {"desiredNumberScheduled": 3, "updatedNumberScheduled": updated, "numberReady": updated}}
        configmap = {"kind": "ConfigMap", "metadata": {"name": "bedrockgateway-config", "namespace": "adp-gateway"}, "data": {
            "ADP_CHAT_WORKER_IMAGE_DIGESTS": cm_worker, "ADP_CHAT_SANDBOX_IMAGE_DIGESTS": cm_sandbox,
            "ADP_CHAT_CREW_IMAGE_DIGESTS": cm_crew, "ADP_CHAT_WORKER_SERVICE_ACCOUNT": cm_service_account, "BG_OTHER": "x"}}
        return scaledjob, daemonset, configmap

    def run_verify(self, scaledjob, daemonset, configmap, ssm_origin="https://crew.example.test", env=None,
                   ssm_worker=SUPERVISOR_DIGEST, ssm_sandbox=SANDBOX_DIGEST, ssm_crew=SANDBOX_DIGEST, ssm_executor=""):
        with tempfile.TemporaryDirectory() as tmp:
            binaries = Path(tmp) / "bin"
            binaries.mkdir()
            (binaries / "kubectl").write_text(
                "#!/bin/bash\n"
                'echo "kubectl $*" >> "$CALLS"\n'
                'case "$2" in scaledjob) cat "$MOCK_SCALEDJOB" ;; daemonset) cat "$MOCK_DAEMONSET" ;; configmap) cat "$MOCK_CONFIGMAP" ;; *) exit 9 ;; esac\n')
            (binaries / "aws").write_text(
                "#!/bin/bash\n"
                'echo "aws $*" >> "$CALLS"\n'
                '[[ "$1 $2 $3" == "ssm get-parameter --name" ]] || exit 9\n'
                'case "$4" in\n'
                '  /adp/dev/gateway/crew-dashboard-origin) value="$MOCK_SSM_ORIGIN" ;;\n'
                '  /adp/dev/gateway/chat-authority-worker-images) value="$MOCK_SSM_WORKER" ;;\n'
                '  /adp/dev/gateway/chat-sandbox-image-digests) value="$MOCK_SSM_SANDBOX" ;;\n'
                '  /adp/dev/gateway/chat-crew-image-digests) value="$MOCK_SSM_CREW" ;;\n'
                '  /adp/dev/gateway/chat-persistent-executor) value="$MOCK_SSM_EXECUTOR" ;;\n'
                '  *) exit 9 ;;\n'
                'esac\n'
                '[[ -n "$value" ]] || { echo "An error occurred (${MOCK_SSM_ERROR:-ParameterNotFound}) when calling the GetParameter operation: $4" >&2; exit 254; }\n'
                'echo "$value"\n')
            for binary in binaries.iterdir():
                binary.chmod(0o755)
            (Path(tmp) / "scaledjob.json").write_text(json.dumps(scaledjob))
            (Path(tmp) / "daemonset.json").write_text(json.dumps(daemonset))
            (Path(tmp) / "configmap.json").write_text(json.dumps(configmap))
            calls = Path(tmp) / "calls"
            calls.touch()
            child = {k: v for k, v in os.environ.items() if not k.startswith("ADP_CREW_") and not k.startswith("ADP_CHAT_")}
            child.update({
                "PATH": f"{binaries}:{os.environ['PATH']}", "CALLS": str(calls), "MOCK_SSM_ORIGIN": ssm_origin,
                "MOCK_SSM_WORKER": ssm_worker, "MOCK_SSM_SANDBOX": ssm_sandbox, "MOCK_SSM_CREW": ssm_crew, "MOCK_SSM_EXECUTOR": ssm_executor,
                "MOCK_SCALEDJOB": str(Path(tmp) / "scaledjob.json"), "MOCK_DAEMONSET": str(Path(tmp) / "daemonset.json"),
                "MOCK_CONFIGMAP": str(Path(tmp) / "configmap.json"),
                "ENVIRONMENT": "dev", "AWS_REGION": "us-east-1", "SUPERVISOR_IMAGE": SUPERVISOR, "SANDBOX_IMAGE": SANDBOX,
            })
            child.update(env or {})
            result = subprocess.run(["bash", str(self.SCRIPT)], capture_output=True, text=True, env=child)
            return result, calls.read_text()

    def test_matching_live_objects_pass_and_the_expectation_comes_from_ssm(self):
        result, calls = self.run_verify(*self.live())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[verify-chat] expected ADP_CREW_DASHBOARD_ORIGIN from ssm /adp/dev/gateway/crew-dashboard-origin: https://crew.example.test", result.stdout)
        self.assertIn("[verify-chat] live ScaledJob, pre-pull DaemonSet, SSM digests and gateway ConfigMap match the release.", result.stdout)
        self.assertIn("ok DaemonSet container prepull-sandbox image (== ScaledJob ADP_CHAT_SANDBOX_IMAGE)", result.stdout)
        self.assertIn("kubectl get scaledjob chat-agent-worker -n adp-gateway-agents -o json", calls)
        self.assertIn("kubectl get daemonset chat-agent-image-prepull -n adp-gateway-agents -o json", calls)
        # SSM == ScaledJob == ConfigMap (#8086), read independently of the deploy script.
        self.assertIn("kubectl get configmap bedrockgateway-config -n adp-gateway -o json", calls)
        for parameter in ("chat-authority-worker-images", "chat-sandbox-image-digests", "chat-crew-image-digests"):
            self.assertIn(f"aws ssm get-parameter --name /adp/dev/gateway/{parameter} --query Parameter.Value --output text --region us-east-1", calls)
        self.assertIn(f"[verify-chat] published /adp/dev/gateway/chat-authority-worker-images: {SUPERVISOR_DIGEST}", result.stdout)
        self.assertIn("ok SSM chat-authority-worker-images (== ScaledJob supervisor digest)", result.stdout)
        self.assertIn("ok SSM chat-crew-image-digests (== ScaledJob ADP_CHAT_SANDBOX_IMAGE digest)", result.stdout)
        self.assertIn("ok ConfigMap bedrockgateway-config ADP_CHAT_SANDBOX_IMAGE_DIGESTS (== ScaledJob ADP_CHAT_SANDBOX_IMAGE digest)", result.stdout)
        self.assertIn("ok ConfigMap bedrockgateway-config ADP_CHAT_WORKER_SERVICE_ACCOUNT", result.stdout)
        # The persistent-executor default (#8115): an unpublished parameter means sandbox-loop; a
        # published value must be what the live ScaledJob carries, env override included.
        self.assertIn("[verify-chat] expected ADP_CHAT_PERSISTENT_EXECUTOR from ssm /adp/dev/gateway/chat-persistent-executor (ParameterNotFound): sandbox-loop", result.stdout)
        self.assertIn("ok ScaledJob env ADP_CHAT_PERSISTENT_EXECUTOR", result.stdout)
        result, _ = self.run_verify(*self.live(executor="claude-code"), ssm_executor="claude-code")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[verify-chat] expected ADP_CHAT_PERSISTENT_EXECUTOR from ssm /adp/dev/gateway/chat-persistent-executor: claude-code", result.stdout)
        result, calls = self.run_verify(*self.live(executor="kirocrew"), ssm_executor="claude-code", env={"ADP_CHAT_PERSISTENT_EXECUTOR": "kirocrew"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("expected ADP_CHAT_PERSISTENT_EXECUTOR from env: kirocrew", result.stdout)
        self.assertNotIn("chat-persistent-executor", calls)
        # Both empty is the Crew-edge-off steady state, not drift; only ParameterNotFound counts as empty.
        result, _ = self.run_verify(*self.live(crew_origin=""), ssm_origin="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("from ssm /adp/dev/gateway/crew-dashboard-origin (ParameterNotFound): <empty>", result.stdout)
        result, _ = self.run_verify(*self.live(crew_origin=""), ssm_origin="None")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_drift_fails_with_a_named_line_per_mismatch(self):
        for label, live, ssm, expected in (
            ("dashboard origin blanked (dev 2026-10-17)", self.live(crew_origin=""), "https://crew.example.test",
             "DRIFT ScaledJob env ADP_CREW_DASHBOARD_ORIGIN is empty while SSM holds 'https://crew.example.test'"),
            ("DaemonSet still on the previous sandbox digest", self.live(prepull_sandbox=SANDBOX.replace("b" * 64, "c" * 64)),
             "https://crew.example.test", "DRIFT DaemonSet container prepull-sandbox image"),
            ("DaemonSet supervisor digest", self.live(prepull=SANDBOX), "https://crew.example.test",
             "DRIFT DaemonSet container prepull image (supervisor)"),
            ("ScaledJob sandbox env differs from the build", self.live(sandbox_env=SANDBOX.replace("b" * 64, "c" * 64)),
             "https://crew.example.test", "DRIFT ScaledJob env ADP_CHAT_SANDBOX_IMAGE"),
            ("missing deadline", self.live(deadline=""), "https://crew.example.test",
             "DRIFT ScaledJob env ADP_CHAT_SANDBOX_START_DEADLINE_MS: live='' expected 5000..300000"),
            ("ScaledJob not Ready", self.live(ready="False"), "https://crew.example.test", "DRIFT ScaledJob Ready"),
            ("executor default re-rendered without the SSM value (#8115)", self.live(executor="claude-code"), "https://crew.example.test",
             "DRIFT ScaledJob env ADP_CHAT_PERSISTENT_EXECUTOR: live='claude-code' expected='sandbox-loop'"),
            ("executor default missing from the ScaledJob (#8115)", self.live(executor=""), "https://crew.example.test",
             "DRIFT ScaledJob env ADP_CHAT_PERSISTENT_EXECUTOR: live='' expected='sandbox-loop'"),
            ("DaemonSet not rolled out", self.live(updated=1), "https://crew.example.test", "DRIFT DaemonSet updatedNumberScheduled"),
        ):
            with self.subTest(label=label):
                result, _ = self.run_verify(*live, ssm_origin=ssm)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(expected, result.stderr)
                self.assertIn("live chat rollout drifts from SSM, the gateway ConfigMap or the release", result.stderr)

    def test_unpublished_or_stale_digests_in_ssm_or_the_configmap_fail(self):
        """dev 2026-10-17 (#8086): the ScaledJob carried the new digests while SSM and the gateway
        ConfigMap still held the previous build's; every turn ended in 'chat creation image refused'."""
        for label, kwargs, expected in (
            ("SSM supervisor digest from the previous build", {"ssm_worker": STALE_DIGEST},
             f"DRIFT SSM chat-authority-worker-images (== ScaledJob supervisor digest): live={STALE_DIGEST!r} expected={SUPERVISOR_DIGEST!r}"),
            ("SSM sandbox digest from the previous build", {"ssm_sandbox": STALE_DIGEST},
             "DRIFT SSM chat-sandbox-image-digests (== ScaledJob ADP_CHAT_SANDBOX_IMAGE digest)"),
            ("SSM Crew digest from the previous build", {"ssm_crew": STALE_DIGEST},
             "DRIFT SSM chat-crew-image-digests (== ScaledJob ADP_CHAT_SANDBOX_IMAGE digest)"),
            ("ConfigMap supervisor digest stale", {"cm_worker": STALE_DIGEST},
             "DRIFT ConfigMap bedrockgateway-config ADP_CHAT_WORKER_IMAGE_DIGESTS (== ScaledJob supervisor digest)"),
            ("ConfigMap sandbox digest stale", {"cm_sandbox": STALE_DIGEST},
             "DRIFT ConfigMap bedrockgateway-config ADP_CHAT_SANDBOX_IMAGE_DIGESTS"),
            ("ConfigMap Crew digest stale", {"cm_crew": STALE_DIGEST},
             "DRIFT ConfigMap bedrockgateway-config ADP_CHAT_CREW_IMAGE_DIGESTS"),
            ("ConfigMap still names the legacy worker", {"cm_service_account": "adp-agent"},
             "DRIFT ConfigMap bedrockgateway-config ADP_CHAT_WORKER_SERVICE_ACCOUNT: live='adp-agent' expected='adp-chat-supervisor'"),
        ):
            with self.subTest(label=label):
                live_kwargs = {k: v for k, v in kwargs.items() if k.startswith("cm_")}
                ssm_kwargs = {k: v for k, v in kwargs.items() if k.startswith("ssm_")}
                result, _ = self.run_verify(*self.live(**live_kwargs), **ssm_kwargs)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(expected, result.stderr)
                self.assertNotIn("match the release", result.stdout)
        # Never published (ParameterNotFound) is drift against a live digest, not "empty equals empty".
        result, _ = self.run_verify(*self.live(), ssm_sandbox="")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("[verify-chat] published /adp/dev/gateway/chat-sandbox-image-digests (ParameterNotFound): <empty>", result.stdout)
        self.assertIn(f"DRIFT SSM chat-sandbox-image-digests (== ScaledJob ADP_CHAT_SANDBOX_IMAGE digest): published=<empty> while the ScaledJob carries {SANDBOX_DIGEST!r}", result.stderr)
        # A ConfigMap without the keys is drift too.
        scaledjob, daemonset, configmap = self.live()
        configmap["data"] = {"BG_OTHER": "x"}
        result, _ = self.run_verify(scaledjob, daemonset, configmap)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("DRIFT ConfigMap bedrockgateway-config ADP_CHAT_WORKER_IMAGE_DIGESTS", result.stderr)

    def test_a_failed_digest_read_refuses_to_compare_before_any_cluster_read(self):
        for code in ("AccessDeniedException", "ThrottlingException"):
            with self.subTest(code=code):
                result, calls = self.run_verify(*self.live(), ssm_worker="", env={"MOCK_SSM_ERROR": code})
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"SSM read of /adp/dev/gateway/chat-authority-worker-images failed ({code}); refusing to compare against a failed read", result.stderr)
                self.assertNotIn("kubectl", calls)
        # The executor default (#8115) is read after the digests with the same fail-closed rule.
        for code in ("AccessDeniedException", "ThrottlingException"):
            with self.subTest(code=code, parameter="chat-persistent-executor"):
                result, calls = self.run_verify(*self.live(), env={"MOCK_SSM_ERROR": code})
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"SSM read of /adp/dev/gateway/chat-persistent-executor failed ({code}); refusing to compare against a failed read", result.stderr)
                self.assertNotIn("kubectl", calls)

    def test_a_failed_ssm_read_refuses_to_compare(self):
        """The expectation must never come from a failed read: on dev the AccessDenied read was
        treated as empty and `live='' expected=''` passed vacuously (#8058)."""
        for code in ("AccessDeniedException", "ThrottlingException"):
            with self.subTest(code=code):
                result, calls = self.run_verify(*self.live(crew_origin=""), ssm_origin="", env={"MOCK_SSM_ERROR": code})
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"SSM read of /adp/dev/gateway/crew-dashboard-origin failed ({code}); refusing to compare against a failed read", result.stderr)
                self.assertNotIn("ok ", result.stdout)
                self.assertNotIn("kubectl", calls, "nothing is read from the cluster after a failed SSM read")

    def test_explicit_expectations_override_ssm_and_the_default_deadline(self):
        result, calls = self.run_verify(*self.live(crew_origin="https://override.example.test", deadline="90000", executor="claude-code"),
                                        env={"ADP_CREW_DASHBOARD_ORIGIN": "https://override.example.test",
                                             "ADP_CHAT_SANDBOX_START_DEADLINE_MS": "90000", "ADP_CHAT_PERSISTENT_EXECUTOR": "claude-code"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("expected ADP_CREW_DASHBOARD_ORIGIN from env", result.stdout)
        self.assertIn("expected ADP_CHAT_PERSISTENT_EXECUTOR from env: claude-code", result.stdout)
        self.assertNotIn("crew-dashboard-origin", calls)
        self.assertNotIn("chat-persistent-executor", calls)
        # The published digests are never an override: they are always read from SSM (#8086).
        self.assertEqual(calls.count("aws ssm get-parameter"), 3)
        result, _ = self.run_verify(*self.live(deadline="120000"), env={"ADP_CHAT_SANDBOX_START_DEADLINE_MS": "90000"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("DRIFT ScaledJob env ADP_CHAT_SANDBOX_START_DEADLINE_MS: live='120000' expected='90000'", result.stderr)

    def test_inputs_are_validated_before_any_cluster_access(self):
        for env in ({"SUPERVISOR_IMAGE": "adp-chat-agent:latest"}, {"SANDBOX_IMAGE": SUPERVISOR}, {"ENVIRONMENT": "Dev"}, {"NAMESPACE": "other"}):
            with self.subTest(env=env):
                result, calls = self.run_verify(*self.live(), env=env)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(calls, "")


if __name__ == "__main__":
    unittest.main()
