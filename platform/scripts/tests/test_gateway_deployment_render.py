"""Render the actual deploy script's Deployment block without AWS/Kubernetes."""
import os
from pathlib import Path
import shlex
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]


class DeploymentRenderTests(unittest.TestCase):
    def test_image_and_all_feature_flags_are_rendered(self):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        start = source.index("  # Render deployment settings as well as the ConfigMap.")
        end = source.index('\n  if [ "$UPDATE_MODE" = true ]; then', start)
        block = source[start:end].split("  CAPACITY_BUNDLE=$(mktemp -d", 1)[0]
        block += '\n  migrate_gateway_before_rollout "$RENDERED_DEPLOYMENT" "$GATEWAY_IMAGE"\n  kubectl apply -f "$RENDERED_DEPLOYMENT" -n adp-gateway\n'
        for enabled in ("false", "true"):
            with self.subTest(enabled=enabled):
                env = dict(os.environ, ROOT_DIR=str(ROOT), ENVIRONMENT="test", REGISTRY="example.test",
                           IMAGE_TAG="release-sha", TEST_FLAG=enabled,
                           GATEWAY_IMAGE="example.test/adp-gateway:release-sha",
                           PERSONA_MODEL_MAPPING_ENABLED="false")
                prefix = '''set -euo pipefail
UPDATE_MODE=false
_get_ssm() { if [[ "$1" == */l5-consumer-targets ]]; then echo '{}'; else echo "$TEST_FLAG"; fi; }
fail() { echo "$*" >&2; exit 1; }
gateway_deployment_replicas() { echo 2; }
kubectl() { shift; shift; cat "$1"; }
migrate_gateway_before_rollout() { printf 'MIGRATED manifest=%s image=%s\\n' "$1" "$2"; cat "$1" >&2; }
'''
                result = subprocess.run(["/bin/bash", "-c", prefix + block],
                                        cwd=ROOT / "modules/gateway", env=env,
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotRegex(result.stdout, r'__[A-Z_]+__|REPLACE_WITH_GATEWAY_IMAGE')
                # The schema migration runs on the rendered manifest with the release image
                # before the Deployment is applied (parity with gateway-deploy.yml).
                self.assertRegex(result.stdout, r'MIGRATED manifest=\S+ image=example.test/adp-gateway:release-sha')
                self.assertLess(result.stdout.index("MIGRATED"), result.stdout.index("image: example.test"))
                self.assertIn("image: example.test/adp-gateway:release-sha", result.stderr)
                self.assertIn("image: example.test/adp-gateway:release-sha", result.stdout)
                for flag in ("FEATURE_ORCHESTRATION_ENGINE_ENABLED",
                             "FEATURE_AGENT_CONTROL_ENABLED", "FEATURE_AGENT_EXPLANATIONS_ENABLED", "FEATURE_NEW_UI_ENABLED"):
                    self.assertRegex(result.stdout, rf'name: {flag}\s+value: "{enabled}"')


KEEPALIVE_PARAMETER = "/adp/test/gateway/crew-proxy-keepalive-seconds"


def _workflow_step(name):
    workflow = yaml.safe_load((ROOT / ".github/workflows/gateway-deploy.yml").read_text())
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == name:
                return step["run"]
    raise AssertionError(f"workflow step {name!r} not found")


class CrewKeepaliveDeploymentRenderTests(unittest.TestCase):
    """UVICORN_WS_PING_INTERVAL follows the Crew keepalive parameter in both renderers (#7902).

    uvicorn/websockets treat ``0`` as a tight ping loop, not as "off", so the only
    safe rendering of a disabled keepalive is to drop the variable; every other
    value renders as-is next to the ConfigMap's CREW_PROXY_KEEPALIVE_SECONDS.
    """

    def _deploy_all_block(self):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        start = source.index("  # Render deployment settings as well as the ConfigMap.")
        end = source.index('\n  if [ "$UPDATE_MODE" = true ]; then', start)
        return source[start:end].split("  CAPACITY_BUNDLE=$(mktemp -d", 1)[0] +             '\n  kubectl apply -f "$RENDERED_DEPLOYMENT" -n adp-gateway\n'

    def _workflow_block(self):
        run = _workflow_step("Migrate schema then roll out new image")
        start = run.index("get_ssm() {")
        end = run.index("# Nullable schema expansion", start)
        return run[start:end].split('python3 "$RUNNER_TEMP/adp-gateway-control/render-capacity.py"', 1)[0]

    def _render(self, renderer, keepalive):
        env = dict(os.environ, ROOT_DIR=str(ROOT), ENVIRONMENT="test", REGISTRY="example.test", IMAGE_TAG="release-sha",
                   KEEPALIVE=keepalive, GATEWAY_IMAGE="example.test/adp-gateway:release-sha",
                   PERSONA_MODEL_MAPPING_ENABLED="false", RELEASE_IMAGE="example.test/adp-gateway@sha256:0",
                   NAMESPACE="adp-gateway", GITHUB_OUTPUT=os.devnull)
        # The stub answers the keepalive parameter from KEEPALIVE and every other
        # parameter with its default, exactly like an environment without overrides.
        stub = f'''[ "$1" != "{KEEPALIVE_PARAMETER}" ] && {{ echo "${{2:-}}"; return; }}; echo "$KEEPALIVE"; }}
'''
        if renderer == "deploy-all.sh":
            prefix = '''set -euo pipefail
UPDATE_MODE=false
_get_ssm() { ''' + stub + '''fail() { echo "$*" >&2; exit 1; }
gateway_deployment_replicas() { echo 2; }
kubectl() { shift; shift; cat "$1"; }
migrate_gateway_before_rollout() { :; }
'''
            # The kubectl stub prints the rendered manifest; the block removes the temp file itself.
            script = prefix + self._deploy_all_block()
            cwd = ROOT / "modules/gateway"
        else:
            # Everything up to the workflow's own get_ssm definition is replaced by the stub.
            block = self._workflow_block()
            block = block[block.index("\n") + 1:]
            prefix = '''set -euo pipefail
RENDERED_DEPLOYMENT=$(mktemp)
trap 'rm -f "$RENDERED_DEPLOYMENT"' EXIT
get_ssm() { ''' + stub
            script = prefix + block + '\ncat "$RENDERED_DEPLOYMENT"\n'
            cwd = ROOT
        result = subprocess.run(["/bin/bash", "-c", script], cwd=cwd, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = result.stdout[result.stdout.index("apiVersion:"):]
        self.assertNotRegex(rendered, r"__[A-Z_]+__")
        container = yaml.safe_load(rendered)["spec"]["template"]["spec"]["containers"][0]
        return {e["name"]: e.get("value") for e in container["env"]}

    def test_a_positive_keepalive_renders_the_ping_interval_in_both_renderers(self):
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            with self.subTest(renderer=renderer):
                env = self._render(renderer, "25")
                self.assertEqual(env["UVICORN_WS_PING_INTERVAL"], "25")

    def test_a_zero_keepalive_drops_the_variable_instead_of_rendering_zero(self):
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            with self.subTest(renderer=renderer):
                env = self._render(renderer, "0")
                self.assertNotIn("UVICORN_WS_PING_INTERVAL", env)
                # The neighbouring Crew entries survive the two-line deletion.
                self.assertIn("CREW_DASHBOARD_ORIGIN", env)
                self.assertIn("FEATURE_SYSTEM_DASHBOARD_ENABLED", env)

    def test_an_absent_or_malformed_parameter_falls_back_to_the_code_default(self):
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            for raw in ("", "None", "twenty"):
                with self.subTest(renderer=renderer, raw=raw):
                    self.assertEqual(self._render(renderer, raw)["UVICORN_WS_PING_INTERVAL"], "25")


class InsightsResultsDeploymentRenderTests(unittest.TestCase):
    def _render(self, renderer, bucket):
        env = dict(os.environ, ROOT_DIR=str(ROOT), ENVIRONMENT="test", REGISTRY="example.test", IMAGE_TAG="release-sha",
                   GATEWAY_IMAGE="example.test/adp-gateway:release-sha", INSIGHTS_BUCKET=bucket,
                   PERSONA_MODEL_MAPPING_ENABLED="false", RELEASE_IMAGE="example.test/adp-gateway@sha256:0",
                   NAMESPACE="adp-gateway", GITHUB_OUTPUT=os.devnull)
        stub = """if [ "$1" = "/adp/test/gateway/insights-results-bucket" ]; then
  printf "%s\\n" "$INSIGHTS_BUCKET"
else
  printf "%s\\n" "${2:-}"
fi
}
"""
        blocks = CrewKeepaliveDeploymentRenderTests()
        if renderer == "deploy-all.sh":
            prefix = """set -euo pipefail
UPDATE_MODE=false
_get_ssm() { """ + stub + """fail() { echo "$*" >&2; exit 1; }
gateway_deployment_replicas() { echo 2; }
kubectl() { shift; shift; cat "$1"; }
migrate_gateway_before_rollout() { echo MIGRATED; }
"""
            script = prefix + blocks._deploy_all_block()
            cwd = ROOT / "modules/gateway"
        else:
            block = blocks._workflow_block()
            block = block[block.index("\n") + 1:]
            prefix = """set -euo pipefail
RENDERED_DEPLOYMENT=$(mktemp)
trap 'rm -f "$RENDERED_DEPLOYMENT"' EXIT
get_ssm() { """ + stub
            script = prefix + block + '\ncat "$RENDERED_DEPLOYMENT"\n'
            cwd = ROOT
        return subprocess.run(["/bin/bash", "-c", script], cwd=cwd, env=env, text=True, capture_output=True)

    def test_absent_and_configured_bucket_render_identically_in_both_paths(self):
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            for bucket in ("", "example-insights-results-123"):
                with self.subTest(renderer=renderer, bucket=bucket):
                    result = self._render(renderer, bucket)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    rendered = result.stdout[result.stdout.index("apiVersion:"):]
                    self.assertNotRegex(rendered, r"__[A-Z_]+__")
                    container = yaml.safe_load(rendered)["spec"]["template"]["spec"]["containers"][0]
                    values = {entry["name"]: entry.get("value") for entry in container["env"]}
                    self.assertEqual(values["BG_INSIGHTS_RESULTS_BUCKET"], bucket)

    def test_insights_and_runtime_proof_settings_survive_both_renderers(self):
        bucket = "example-insights-results-123"
        expected = {"BG_INSIGHTS_RESULTS_BUCKET": {"value": bucket}}
        for name, field in (("ADP_POD_UID", "metadata.uid"),
                            ("ADP_POD_NAMESPACE", "metadata.namespace")):
            expected[name] = {"valueFrom": {"fieldRef": {"fieldPath": field}}}
        for name, key in (("ADP_RUNTIME_PROOF_ROLE_ARNS", "reader-role-arns"),
                          ("ADP_RUNTIME_PROOF_INVOKE_URL", "internal-invoke-url"),
                          ("ADP_PRODUCT_EVALUATOR_ROLE_ARN", "evaluator-role-arn"),
                          ("ADP_PRODUCT_RECOVERY_ROLE_ARN", "recovery-role-arn"),
                          ("ADP_PRODUCT_BROWSER_RETIREMENT_ROLE_ARN", "browser-retirement-role-arn")):
            expected[name] = {"valueFrom": {"configMapKeyRef": {
                "name": "bedrockgateway-runtime-proof", "key": key, "optional": True,
            }}}
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            with self.subTest(renderer=renderer):
                result = self._render(renderer, bucket)
                self.assertEqual(result.returncode, 0, result.stderr)
                rendered = result.stdout[result.stdout.index("apiVersion:"):]
                container = yaml.safe_load(rendered)["spec"]["template"]["spec"]["containers"][0]
                for name, settings in expected.items():
                    entries = [entry for entry in container["env"] if entry["name"] == name]
                    self.assertEqual(entries, [{"name": name, **settings}])

    def test_invalid_bucket_is_rejected_before_migration_or_rendered_output(self):
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            for bucket in ("Invalid_Bucket", "example|bucket", "example\nbucket", "x", "a" * 64):
                with self.subTest(renderer=renderer, bucket=bucket):
                    result = self._render(renderer, bucket)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Invalid Insights results bucket setting", result.stdout + result.stderr)
                    self.assertNotIn("MIGRATED", result.stdout)
                    self.assertNotIn("apiVersion:", result.stdout)


class AgentCoreMemoryRenderGuardTests(unittest.TestCase):
    """Both renderers refuse a partial AgentCore memory target (#7901).

    agentcore_recall() raises "memory target unconfigured" when some but not all of
    the four values are set, so the renderers fail the deploy instead of rolling out
    an outage. The namespace key is decrypted and never printed.
    """

    PLAIN = {
        "/adp/test/gateway/agentcore-memory-id": "mem-1",
        "/adp/test/gateway/agentcore-memory-strategy-id": "strat-1",
        "/adp/test/gateway/agentcore-memory-region": "us-east-1",
    }
    KEY_PARAMETER = "/adp/test/gateway/agentcore-namespace-key"

    def _deploy_all_block(self):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        start = source.index("  # Crew proxy tuning and checkpoints (#7902)")
        end = source.index("  SECRET_APPLY_RESULT=$(kubectl create secret", start)
        return source[start:end]

    def _workflow_block(self):
        run = _workflow_step("Ensure namespace and render K8s prerequisites")
        start = run.index("# Crew proxy tuning and checkpoints (#7902)")
        end = run.index('sed -e "s|__AWS_REGION__|', start)
        return run[start:end]

    def _run(self, renderer, plain, key):
        cases = "".join(f'    "{name}") echo "{value}" ;;\n' for name, value in plain.items())
        lookup = f'''lookup() {{
  case "$1" in
{cases}    *) echo "${{2:-}}" ;;
  esac
}}
'''
        getter = "_get_ssm" if renderer == "deploy-all.sh" else "get_ssm"
        prefix = f'''set -euo pipefail
ENVIRONMENT=test
AWS_REGION=us-east-1
{lookup}
{getter}() {{ lookup "$@"; }}
aws() {{ [ -n "$KEY" ] || return 1; echo "$KEY"; }}
fail() {{ echo "$*" >&2; exit 1; }}
'''
        block = self._deploy_all_block() if renderer == "deploy-all.sh" else self._workflow_block()
        suffix = ('\nprintf "memory_id=%s key_set=%s\\n" "$ADP_AGENTCORE_MEMORY_ID" '
                  '"$([ -n "$ADP_AGENTCORE_NAMESPACE_KEY" ] && echo yes || echo no)"\n')
        return subprocess.run(["/bin/bash", "-c", prefix + block + suffix], cwd=ROOT, text=True,
                              capture_output=True, env=dict(os.environ, KEY=key))

    def test_all_four_values_render_and_the_key_is_never_printed(self):
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            with self.subTest(renderer=renderer):
                result = self._run(renderer, self.PLAIN, "ab" * 32)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("memory_id=mem-1 key_set=yes", result.stdout)
                self.assertNotIn("ab" * 32, result.stdout + result.stderr)

    def test_no_values_render_an_empty_target_without_failing(self):
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            with self.subTest(renderer=renderer):
                result = self._run(renderer, {}, "")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("memory_id= key_set=no", result.stdout)

    def test_a_partial_target_fails_the_render(self):
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            with self.subTest(renderer=renderer, missing="namespace-key"):
                result = self._run(renderer, self.PLAIN, "")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("partially configured (3/4", result.stdout + result.stderr)
            with self.subTest(renderer=renderer, missing="plain-parameters"):
                result = self._run(renderer, {}, "ab" * 32)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("partially configured (1/4", result.stdout + result.stderr)
                self.assertNotIn("ab" * 32, result.stdout + result.stderr)

    def test_a_memory_region_other_than_the_gateway_region_fails_the_render(self):
        plain = dict(self.PLAIN, **{"/adp/test/gateway/agentcore-memory-region": "eu-west-1"})
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            with self.subTest(renderer=renderer):
                result = self._run(renderer, plain, "ab" * 32)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("differs from the gateway region us-east-1", result.stdout + result.stderr)


class ChatStoreConfigMapRenderTests(unittest.TestCase):
    """Both renderers resolve the chat memory/artifact stores identically (#8028).

    The tables fall back to their deterministic names when the agent-factory parameter is
    absent or malformed (including the CLI's literal ``None``); the bucket is the task-api
    artifact bucket and renders empty when it is absent or not a bucket name, which the
    gateway reports as ``chat_artifacts_unconfigured`` and the sandbox degrades on.
    """

    MEMORY = "/adp/test/gateway/chat-memory-table"
    ARTIFACTS = "/adp/test/gateway/chat-artifacts-table"

    def _block(self, renderer):
        if renderer == "deploy-all.sh":
            source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
            start = source.index("  # Delegated chat memory/artifact stores (#8028)")
            return source[start:source.index("  ADP_TASK_API_QUEUE_URL=$(get_authority_ssm", start)]
        run = _workflow_step("Ensure namespace and render K8s prerequisites")
        start = run.index("# Delegated chat memory/artifact stores (#8028)")
        return run[start:run.index("ADP_TASK_API_QUEUE_URL=$(get_authority_ssm", start)]

    def _render(self, renderer, values, bucket):
        cases = "".join(f'    "{name}") printf %s {shlex.quote(value)} ;;\n' for name, value in values.items())
        getter = "_get_ssm" if renderer == "deploy-all.sh" else "get_ssm"
        prefix = f"""set -euo pipefail
ENVIRONMENT=test
TASK_ARTIFACT_BUCKET_NAME={shlex.quote(bucket)}
{getter}() {{ case "$1" in
{cases}    *) echo "${{2:-}}" ;;
  esac; }}
"""
        suffix = """
sed -e "s|__MEMORY_TABLE__|${MEMORY_TABLE}|g" \\
    -e "s|__ARTIFACTS_TABLE__|${ARTIFACTS_TABLE}|g" \\
    -e "s|__ARTIFACTS_BUCKET__|${ARTIFACTS_BUCKET}|g" \\
    modules/gateway/k8s/configmap.yaml | grep -E '^  (MEMORY_TABLE|ARTIFACTS_TABLE|ARTIFACTS_BUCKET):'
"""
        result = subprocess.run(["/bin/bash", "-c", prefix + self._block(renderer) + suffix], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = yaml.safe_load(result.stdout)
        self.assertEqual(sorted(rendered), ["ARTIFACTS_BUCKET", "ARTIFACTS_TABLE", "MEMORY_TABLE"])
        return rendered

    def test_absent_parameters_render_the_deterministic_tables_and_an_empty_bucket(self):
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            with self.subTest(renderer=renderer):
                self.assertEqual(self._render(renderer, {}, ""), {
                    "MEMORY_TABLE": "adp-test-agent-memory", "ARTIFACTS_TABLE": "adp-test-chat-artifacts", "ARTIFACTS_BUCKET": "",
                })

    def test_well_formed_values_render_verbatim_in_both_renderers(self):
        values = {self.MEMORY: "custom_memory.v2", self.ARTIFACTS: "Custom-Artifacts"}
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            with self.subTest(renderer=renderer):
                self.assertEqual(self._render(renderer, values, "adp-test-chat-artifacts-123456789012"), {
                    "MEMORY_TABLE": "custom_memory.v2", "ARTIFACTS_TABLE": "Custom-Artifacts",
                    "ARTIFACTS_BUCKET": "adp-test-chat-artifacts-123456789012",
                })

    def test_malformed_values_fall_back_in_both_renderers(self):
        for bad in ("None", "", "a", "not a table", "x|y"):
            for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
                with self.subTest(renderer=renderer, bad=bad):
                    rendered = self._render(renderer, {self.MEMORY: bad, self.ARTIFACTS: bad}, bad)
                    self.assertEqual(rendered["MEMORY_TABLE"], "adp-test-agent-memory")
                    self.assertEqual(rendered["ARTIFACTS_TABLE"], "adp-test-chat-artifacts")
                    self.assertEqual(rendered["ARTIFACTS_BUCKET"], "")
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            with self.subTest(renderer=renderer, bucket="uppercase"):
                self.assertEqual(self._render(renderer, {}, "Not-A-Bucket")["ARTIFACTS_BUCKET"], "")


if __name__ == "__main__":
    unittest.main()


class ModelProbeConfigMapRenderTests(unittest.TestCase):
    """Both renderers validate the six model-probe controls identically (#7967).

    The model-policy plane fails closed without probe evidence, so a malformed SSM
    value must render the fail-closed default rather than pass through, and a
    well-formed JSON allowlist must survive the sed chain (including | and &).
    """

    PARAMETERS = ("model-probe-enabled", "model-probe-max-slots-per-cycle", "model-probe-budget-usd-per-cycle",
                  "model-probe-max-budget-usd-per-attempt", "model-probe-model-allowlist", "model-probe-evidence-ttl-hours")
    KEYS = ("BG_MODEL_PROBE_ENABLED", "BG_MODEL_PROBE_MAX_SLOTS_PER_CYCLE", "BG_MODEL_PROBE_BUDGET_USD_PER_CYCLE",
            "BG_MODEL_PROBE_MAX_BUDGET_USD_PER_ATTEMPT", "BG_MODEL_PROBE_MODEL_ALLOWLIST", "BG_MODEL_PROBE_EVIDENCE_TTL_HOURS")

    def _block(self, renderer):
        if renderer == "deploy-all.sh":
            source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
            start = source.index("  # Model invocability probing (#7967)")
            return source[start:source.index("  # Issue #3960: CIDRs", start)]
        run = _workflow_step("Ensure namespace and render K8s prerequisites")
        start = run.index("# Model invocability probing (#7967)")
        return run[start:run.index("# Issue #3960: CIDRs", start)]

    def _render(self, renderer, values):
        cases = "".join(f'    "/adp/test/gateway/{name}") printf %s {shlex.quote(value)} ;;\n' for name, value in values.items())
        getter = "_get_ssm" if renderer == "deploy-all.sh" else "get_ssm"
        prefix = f"""set -euo pipefail
ENVIRONMENT=test
{getter}() {{ case "$1" in
{cases}    *) echo "${{2:-}}" ;;
  esac; }}
"""
        suffix = """
sed -e "s|__MODEL_PROBE_ENABLED__|${MODEL_PROBE_ENABLED}|g" \\
    -e "s|__MODEL_PROBE_MAX_SLOTS_PER_CYCLE__|${MODEL_PROBE_MAX_SLOTS_PER_CYCLE}|g" \\
    -e "s|__MODEL_PROBE_BUDGET_USD_PER_CYCLE__|${MODEL_PROBE_BUDGET_USD_PER_CYCLE}|g" \\
    -e "s|__MODEL_PROBE_MAX_BUDGET_USD_PER_ATTEMPT__|${MODEL_PROBE_MAX_BUDGET_USD_PER_ATTEMPT}|g" \\
    -e "s|__MODEL_PROBE_MODEL_ALLOWLIST__|${MODEL_PROBE_MODEL_ALLOWLIST_SED}|g" \\
    -e "s|__MODEL_PROBE_EVIDENCE_TTL_HOURS__|${MODEL_PROBE_EVIDENCE_TTL_HOURS}|g" \\
    modules/gateway/k8s/configmap.yaml | grep -E '^  BG_MODEL_PROBE_'
"""
        result = subprocess.run(["/bin/bash", "-c", prefix + self._block(renderer) + suffix], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = yaml.safe_load(result.stdout)
        self.assertEqual(sorted(rendered), sorted(self.KEYS))
        return rendered

    def test_an_absent_parameter_set_renders_the_fail_closed_defaults(self):
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            with self.subTest(renderer=renderer):
                self.assertEqual(list(self._render(renderer, {}).values()), ["false", "0", "0", "0", "[]", "24"])

    def test_well_formed_values_render_verbatim_including_a_json_allowlist(self):
        values = dict(zip(self.PARAMETERS, ("true", "24", "5", "0.50", '["global.anthropic.claude-opus-5", "x|y&z"]', "48")))
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            with self.subTest(renderer=renderer):
                rendered = self._render(renderer, values)
                self.assertEqual(list(rendered.values()), ["true", "24", "5", "0.50", '["global.anthropic.claude-opus-5","x|y&z"]', "48"])

    def test_malformed_values_fall_back_to_the_defaults_in_both_renderers(self):
        values = dict(zip(self.PARAMETERS, ("yes", "-1", "5.1234567", "abc", '{"not": "a list"}', "None")))
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            with self.subTest(renderer=renderer):
                self.assertEqual(list(self._render(renderer, values).values()), ["false", "0", "0", "0", "[]", "24"])
        values["model-probe-model-allowlist"] = '["ok", 1]'
        self.assertEqual(self._render("deploy-all.sh", values)["BG_MODEL_PROBE_MODEL_ALLOWLIST"], "[]")


class ChatModelPolicyPostureRenderTests(unittest.TestCase):
    """Both renderers render the chat model-policy posture identically (#7997).

    off | advisory | enforced pass through verbatim; an absent or unrecognised value
    renders the default, advisory, so a typo can neither enforce nor switch off.
    """

    def _block(self, renderer):
        if renderer == "deploy-all.sh":
            source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
            start = source.index("  # Model policy posture for chat (#7997)")
            return source[start:source.index("  # Issue #3960: CIDRs", start)]
        run = _workflow_step("Ensure namespace and render K8s prerequisites")
        start = run.index("# Model policy posture for chat (#7997)")
        return run[start:run.index("# Issue #3960: CIDRs", start)]

    def _render(self, renderer, value):
        getter = "_get_ssm" if renderer == "deploy-all.sh" else "get_ssm"
        case = "" if value is None else f'    "/adp/test/gateway/chat-model-policy-posture") printf %s {shlex.quote(value)} ;;\n'
        prefix = f"""set -euo pipefail
ENVIRONMENT=test
{getter}() {{ case "$1" in
{case}    *) echo "${{2:-}}" ;;
  esac; }}
"""
        suffix = """
sed -e "s|__CHAT_MODEL_POLICY_POSTURE__|${CHAT_MODEL_POLICY_POSTURE}|g" modules/gateway/k8s/configmap.yaml | grep -E '^  BG_CHAT_MODEL_POLICY_POSTURE:'
"""
        result = subprocess.run(["/bin/bash", "-c", prefix + self._block(renderer) + suffix], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return yaml.safe_load(result.stdout)["BG_CHAT_MODEL_POLICY_POSTURE"]

    def test_each_posture_renders_verbatim_and_anything_else_renders_advisory(self):
        for renderer in ("deploy-all.sh", "gateway-deploy.yml"):
            with self.subTest(renderer=renderer):
                for value in ("off", "advisory", "enforced"):
                    self.assertEqual(self._render(renderer, value), value)
                for value in (None, "", "None", "Enforced", "enforcing", "report_only", "true"):
                    self.assertEqual(self._render(renderer, value), "advisory", value)
