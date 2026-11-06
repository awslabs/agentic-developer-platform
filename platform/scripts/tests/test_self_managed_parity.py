"""The self-managed deploy path declares and applies what the GitHub workflows apply.

Each check pins a fact the operator asked for: a fresh or updated environment must be
reproducible from deploy-all.sh alone, with no GitHub protected environment involved
(#3408). The checks are static so they run without AWS or a cluster.
"""
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
DEPLOY_ALL = (ROOT / "platform/scripts/deploy-all.sh").read_text()
WORKFLOW = (ROOT / ".github/workflows/gateway-deploy.yml").read_text()


class SelfManagedParityTests(unittest.TestCase):
    def test_configmap_chat_and_tools_keys_are_rendered_from_the_same_parameters(self):
        for key, parameter in (
            ("ADP_CHAT_INPUT_QUEUE_URL", "/adp/${ENVIRONMENT}/agent-gateway/chat-input-queue-url"),
            ("ADP_CHAT_RESPONSE_QUEUE_URL", "/adp/${ENVIRONMENT}/agent-gateway/response-queue-url"),
            ("BG_INTAKE_SESSIONS_TABLE", "/adp/${ENVIRONMENT}/agent-gateway/sessions-table"),
            ("BG_INTAKE_CONTEXT_TABLE", "/adp/${ENVIRONMENT}/agent-gateway/context-table"),
            ("BG_INTAKE_INGEST_FUNCTION", "/adp/${ENVIRONMENT}/agent-gateway/ingest-function"),
            ("ADP_TOOLS_GATEWAY_ROLE_ARN", "/adp/${ENVIRONMENT}/tools/target-role-arn"),
            ("ADP_TOOLS_FACADE_ROLE_ARN", "/adp/${ENVIRONMENT}/tools/caller-role-arn"),
            ("ADP_TOOLS_ASSERTION_KEY_ARN", "/adp/${ENVIRONMENT}/tools/assertion-signing-key-arn"),
            ("ADP_CHAT_SANDBOX_CA_CONFIGMAP", "/adp/${ENVIRONMENT}/gateway/chat-sandbox-ca-configmap"),
        ):
            with self.subTest(key=key):
                self.assertIn(f'{key}=$(_get_ssm "{parameter}"', DEPLOY_ALL)
                self.assertIn(f'{key}=$(get_ssm "{parameter}"', WORKFLOW)
                self.assertIn(f's|__{key}__|${{{key}}}|g', DEPLOY_ALL)
                self.assertIn(f's|__{key}__|${{{key}}}|g', WORKFLOW)

    def test_sandbox_data_url_is_derived_identically_from_the_ca_configmap_name(self):
        # Empty CA name -> empty URL; otherwise the fixed in-cluster listener (#7899).
        derivation = (
            'ADP_CHAT_DATA_URL=""\n',
            '[ -z "$ADP_CHAT_SANDBOX_CA_CONFIGMAP" ] || ADP_CHAT_DATA_URL="https://chat-sandbox-gateway.adp-gateway.svc:8443"',
            '[ "$ADP_CHAT_SANDBOX_CA_CONFIGMAP" != "None" ] || ADP_CHAT_SANDBOX_CA_CONFIGMAP=""',
            's|__ADP_CHAT_DATA_URL__|${ADP_CHAT_DATA_URL}|g',
        )
        for renderer in (DEPLOY_ALL, WORKFLOW):
            for line in derivation:
                self.assertIn(line, renderer)
        configmap = (ROOT / "modules/gateway/k8s/configmap.yaml").read_text()
        self.assertIn('ADP_CHAT_DATA_URL: "__ADP_CHAT_DATA_URL__"', configmap)
        self.assertIn('ADP_CHAT_SANDBOX_CA_CONFIGMAP: "__ADP_CHAT_SANDBOX_CA_CONFIGMAP__"', configmap)

    def test_chat_workload_identities_are_rendered_from_the_same_parameters(self):
        for key, parameter in (
            ("ADP_CHAT_WORKER_IMAGE_DIGESTS", "/adp/${ENVIRONMENT}/gateway/chat-authority-worker-images"),
            ("ADP_CHAT_SANDBOX_IMAGE_DIGESTS", "/adp/${ENVIRONMENT}/gateway/chat-sandbox-image-digests"),
            ("ADP_CHAT_CREW_IMAGE_DIGESTS", "/adp/${ENVIRONMENT}/gateway/chat-crew-image-digests"),
        ):
            with self.subTest(key=key):
                for renderer in (DEPLOY_ALL, WORKFLOW):
                    self.assertIn(f'{key}_RAW=$(get_authority_ssm "{parameter}" "")', renderer)
                    self.assertIn(f's|__{key}__|${{{key}_SED}}|g', renderer)
        for renderer in (DEPLOY_ALL, WORKFLOW):
            self.assertIn('s|__ADP_CHAT_WORKER_SERVICE_ACCOUNT__|${ADP_CHAT_WORKER_SERVICE_ACCOUNT}|g', renderer)

    def test_delegated_chat_stage_is_part_of_the_self_managed_path(self):
        self.assertIn('--chat) CHAT_MODE=', DEPLOY_ALL)
        self.assertIn('--chat-exception) CHAT_EXCEPTION=', DEPLOY_ALL)
        for script in ("apply-chat-tls.sh", "deploy-chat-scaledjob.sh"):
            self.assertIn(script, DEPLOY_ALL)
        tls = DEPLOY_ALL.index('bash "$SCRIPT_DIR/apply-chat-tls.sh"')
        scaledjob = DEPLOY_ALL.index('ADP_CHAT_CREW_CONTROLS_ONLY=false \\\n      bash "$ROOT_DIR/modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh"')
        drain = DEPLOY_ALL.index('python3 "$SCRIPT_DIR/upgrade-chat.py" drain')
        self.assertLess(tls, scaledjob)
        self.assertLess(scaledjob, drain)
        launcher = (ROOT / "deploy.sh").read_text()
        self.assertIn("--chat|--chat-exception)", launcher)

    def test_delegated_chat_requires_armed_model_probing_and_runs_one_cycle(self):
        """Probe evidence is required only under chat_model_policy_posture=enforced (#7967, #7997)."""
        posture = DEPLOY_ALL.index('CHAT_MODEL_POLICY_POSTURE=$(_chat_ssm "/adp/${ENVIRONMENT}/gateway/chat-model-policy-posture")')
        guard = DEPLOY_ALL.index('MODEL_PROBE_GUARD_ENABLED=$(_chat_ssm "/adp/${ENVIRONMENT}/gateway/model-probe-enabled")')
        build = DEPLOY_ALL.index('run_codebuild "adp-${ENVIRONMENT}-chat-agent"')
        self.assertLess(DEPLOY_ALL.index('step "Step 10c/11: Delegated chat rollout'), posture)
        self.assertLess(posture, guard, "the posture is read before the probing guard decides fail or warn")
        self.assertIn('case "$CHAT_MODEL_POLICY_POSTURE" in off|advisory|enforced) ;; *) CHAT_MODEL_POLICY_POSTURE="advisory" ;; esac',
                      DEPLOY_ALL[posture:guard])
        self.assertLess(guard, build, "the guard runs before any image is built")
        for parameter in ("model-probe-max-slots-per-cycle", "model-probe-budget-usd-per-cycle", "model-probe-max-budget-usd-per-attempt"):
            self.assertIn(f'_chat_ssm "/adp/${{ENVIRONMENT}}/gateway/{parameter}"', DEPLOY_ALL[guard:build])
        self.assertIn('if [ "$CHAT_MODEL_POLICY_POSTURE" = enforced ]; then\n        fail "Delegated chat enforces model policy '
                      '(chat_model_policy_posture=enforced) and needs invocability evidence; set model_probe_enabled/'
                      'model_probe_max_slots_per_cycle/model_probe_budget_usd_per_cycle/model_probe_max_budget_usd_per_attempt in '
                      'environments/${ENVIRONMENT}/modules/gateway.tfvars and persona_model_probe_enabled=true in '
                      'webhook-ingress.tfvars', DEPLOY_ALL[guard:build])
        self.assertIn('warn "Model invocability probing is not armed; chat model policy posture is ${CHAT_MODEL_POLICY_POSTURE}',
                      DEPLOY_ALL[guard:build])
        scaledjob_ready = DEPLOY_ALL.index('[ "$DELEGATED_SA" = adp-chat-supervisor ]')
        cycle = DEPLOY_ALL.index('bash "$SCRIPT_DIR/run-probe-cycle.sh"')
        drain = DEPLOY_ALL.index('python3 "$SCRIPT_DIR/upgrade-chat.py" drain')
        self.assertLess(scaledjob_ready, cycle)
        self.assertLess(cycle, drain, "evidence is recorded before the legacy worker is drained")
        self.assertIn('3) warn "Model invocability probe cycle had nothing to claim', DEPLOY_ALL[cycle:drain])
        self.assertIn('*) if [ "$CHAT_MODEL_POLICY_POSTURE" = enforced ]; then\n           fail "Model invocability probe cycle did not record '
                      'proven evidence', DEPLOY_ALL[cycle:drain])
        self.assertIn('warn "Model invocability probe cycle did not record proven evidence (exit $PROBE_CYCLE_STATUS); chat model policy '
                      'posture is ${CHAT_MODEL_POLICY_POSTURE}', DEPLOY_ALL[cycle:drain])
        self.assertTrue(os.access(ROOT / "platform/scripts/run-probe-cycle.sh", os.X_OK))

    def test_delegated_mode_keeps_the_model_policy_plumbing_on_in_every_posture(self):
        """ADP_CHAT_MODEL_POLICY_ENABLED is the delegated-chat plumbing (model roots, decision route, sandbox
        admission), not the evidence posture: the ingest answers "Chat is currently unavailable" without it."""
        self.assertIn('[ "$CHAT_MODE" != delegated ] || CHAT_MODEL_POLICY_ENABLED=true', DEPLOY_ALL)
        self.assertIn("ADP_CHAT_EXISTING_NAMESPACE_ONLY=true ADP_CHAT_MODEL_POLICY_ENABLED=true ADP_CHAT_CREW_CONTROLS_ONLY=false", DEPLOY_ALL)
        self.assertIn("CHAT_POLICY_ARGS=(-var=chat_model_policy_enabled=true)", DEPLOY_ALL)
        ingest = (ROOT / "modules/agent-factory/gateway/lambdas/ingest/handler.py").read_text()
        self.assertIn('os.environ.get("ADP_CHAT_MODEL_POLICY_ENABLED", "false").lower() != "true"', ingest)
        self.assertIn("Chat is currently unavailable", ingest)
        scaledjob = (ROOT / "modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh").read_text()
        self.assertIn('[[ "${ADP_CHAT_MODEL_POLICY_ENABLED:-false}" == true ]] ||', scaledjob)
        self.assertIn("chat_model_policy_posture", scaledjob)

    def _run_delegated_guard(self, values):
        start = DEPLOY_ALL.index('    _chat_ssm() { aws ssm get-parameter')
        end = DEPLOY_ALL.index('    if [ "$LOCAL_MODE" = true ] && docker info', start)
        cases = "".join(f'    "/adp/test/gateway/{name}") echo "{value}" ;;\n' for name, value in values.items())
        prefix = f"""set -euo pipefail
ENVIRONMENT=test
AWS_REGION=us-east-1
aws() {{ shift 3; case "$1" in
{cases}    *) return 1 ;;
  esac; }}
fail() {{ echo "FAIL: $*" >&2; exit 1; }}
ok() {{ echo "OK: $*"; }}
warn() {{ echo "WARN: $*"; }}
"""
        return subprocess.run(["/bin/bash", "-c", prefix + DEPLOY_ALL[start:end]], text=True, capture_output=True, cwd=ROOT)

    ARMED = {"model-probe-enabled": "true", "model-probe-max-slots-per-cycle": "24",
             "model-probe-budget-usd-per-cycle": "5", "model-probe-max-budget-usd-per-attempt": "0.50"}
    BROKEN = (("model-probe-enabled", "false"), ("model-probe-enabled", ""), ("model-probe-max-slots-per-cycle", "0"),
              ("model-probe-budget-usd-per-cycle", "0.00"), ("model-probe-max-budget-usd-per-attempt", ""),
              ("model-probe-max-budget-usd-per-attempt", "abc"))

    def test_delegated_guard_fails_fast_when_probing_is_off_under_enforced_and_passes_when_armed(self):
        armed = dict(self.ARMED, **{"chat-model-policy-posture": "enforced"})
        result = self._run_delegated_guard(armed)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK: Chat model policy posture: enforced", result.stdout)
        self.assertIn("OK: Model invocability probing is armed (slots=24/cycle, budget 5 USD/cycle, 0.50 USD/attempt)", result.stdout)
        for name, broken in self.BROKEN:
            with self.subTest(parameter=name, value=broken):
                result = self._run_delegated_guard(dict(armed, **{name: broken}))
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("Delegated chat enforces model policy (chat_model_policy_posture=enforced) and needs invocability evidence",
                              result.stderr)
                self.assertIn("environments/test/modules/gateway.tfvars", result.stderr)
                self.assertIn("persona_model_probe_enabled=true in webhook-ingress.tfvars", result.stderr)
                self.assertIn('or set chat_model_policy_posture = "advisory"', result.stderr)

    def test_delegated_guard_only_warns_when_probing_is_off_under_advisory_or_off(self):
        """#7997: chat answers without evidence under advisory/off, so an unarmed probe must not stop the rollout."""
        for posture, published in (("advisory", "advisory"), ("off", "off"), ("advisory", ""), ("advisory", "None"), ("advisory", "bogus")):
            for name, broken in self.BROKEN:
                with self.subTest(posture=published or "<absent>", parameter=name, value=broken):
                    values = dict(self.ARMED, **{name: broken})
                    if published:
                        values["chat-model-policy-posture"] = published
                    result = self._run_delegated_guard(values)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"OK: Chat model policy posture: {posture}", result.stdout)
                    self.assertIn(f"WARN: Model invocability probing is not armed; chat model policy posture is {posture}, so delegated "
                                  "chat answers without invocability evidence", result.stdout)
                    self.assertIn("before setting the posture to enforced", result.stdout)
                    self.assertNotIn("FAIL:", result.stderr)
        result = self._run_delegated_guard(dict(self.ARMED, **{"chat-model-policy-posture": "advisory"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK: Model invocability probing is armed", result.stdout)
        self.assertNotIn("WARN:", result.stdout)

    def test_probe_cycle_outcome_gates_the_rollout_only_under_enforced(self):
        start = DEPLOY_ALL.index('    ENVIRONMENT="$ENVIRONMENT" AWS_REGION="$AWS_REGION" bash "$SCRIPT_DIR/run-probe-cycle.sh"')
        end = DEPLOY_ALL.index("    esac\n", start) + len("    esac\n")
        cases = ((0, "enforced", 0, "OK: Model invocability probe cycle recorded proven evidence"),
                 (3, "enforced", 0, "WARN: Model invocability probe cycle had nothing to claim"),
                 (1, "enforced", 1, "FAIL: Model invocability probe cycle did not record proven evidence (exit 1)"),
                 (4, "enforced", 1, "(exit 4); delegated chat would refuse every turn under chat_model_policy_posture=enforced"),
                 (0, "advisory", 0, "OK: Model invocability probe cycle recorded proven evidence"),
                 (1, "advisory", 0, "WARN: Model invocability probe cycle did not record proven evidence (exit 1); chat model policy posture "
                                    "is advisory, so delegated chat answers without evidence"),
                 (4, "off", 0, "WARN: Model invocability probe cycle did not record proven evidence (exit 4); chat model policy posture "
                               "is off, so delegated chat answers without evidence"))
        for status, posture, expected_rc, marker in cases:
            with self.subTest(status=status, posture=posture):
                prefix = f"""set -euo pipefail
ENVIRONMENT=test
AWS_REGION=us-east-1
SCRIPT_DIR=/stub
CHAT_MODEL_POLICY_POSTURE={posture}
bash() {{ [ "$1" = /stub/run-probe-cycle.sh ] || exit 99; return {status}; }}
fail() {{ echo "FAIL: $*"; exit 1; }}
ok() {{ echo "OK: $*"; }}
warn() {{ echo "WARN: $*"; }}
"""
                result = subprocess.run(["/bin/bash", "-c", prefix + DEPLOY_ALL[start:end]], text=True, capture_output=True)
                self.assertEqual(result.returncode, expected_rc, result.stderr)
                self.assertIn(marker, result.stdout)
                if expected_rc == 0:
                    self.assertNotIn("FAIL:", result.stdout)

    def test_schema_migration_runs_before_the_deployment_is_applied(self):
        helper = DEPLOY_ALL.index("migrate_gateway_before_rollout() {")
        self.assertIn("migrate-before-rollout.py", DEPLOY_ALL[helper:helper + 400])
        call = DEPLOY_ALL.index('migrate_gateway_before_rollout "$RENDERED_DEPLOYMENT" "$GATEWAY_IMAGE"')
        apply = DEPLOY_ALL.index('DEPLOYMENT_APPLY_RESULT=$(python3 scripts/apply-gateway-deployment.py')
        self.assertLess(call, apply)
        self.assertIn('--manifest "$RENDERED_DEPLOYMENT" --image "$GATEWAY_IMAGE" --namespace adp-gateway',
                      DEPLOY_ALL[apply:apply + 250])

    def test_runtime_verifier_access_entry_lives_in_the_platform_stack(self):
        platform = (ROOT / "platform/infra/runtime-verifier-access.tf").read_text()
        self.assertIn('resource "aws_eks_access_entry" "runtime_verifier"', platform)
        self.assertIn('kubernetes_groups = ["adp:runtime-verifier-${var.environment}"]', platform)
        self.assertIn("cluster_name      = module.eks.cluster_name", platform)
        self.assertFalse((ROOT / "platform/automation-infra/runtime-verifier-access.tf").exists(),
                         "the access entry must not be declared in two stacks")
        for tf in (ROOT / "platform/automation-infra").glob("*.tf"):
            self.assertNotIn('"aws_eks_access_entry" "runtime_verifier"', tf.read_text(), tf.name)

    def test_hand_applied_dev_objects_are_adopted_before_each_apply(self):
        self.assertIn("tf_adopt_existing() {", DEPLOY_ALL)
        for address, import_id in (
            ("aws_ssm_parameter.feature_chat", "/adp/${ENVIRONMENT}/gateway/feature-chat"),
            ("aws_ssm_parameter.chat_authority_worker_images", "/adp/${ENVIRONMENT}/gateway/chat-authority-worker-images"),
        ):
            self.assertIn(f'"{address}={import_id}"', DEPLOY_ALL)
        self.assertIn('tf_adopt_existing "kubernetes_${kind}.runtime_verifier" "adp-gateway/adp-runtime-verifier"', DEPLOY_ALL)
        webhook = (ROOT / "modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh").read_text()
        self.assertIn("adopt_door_identity_rbac() {", webhook)
        self.assertLess(webhook.index("    adopt_door_identity_rbac\n"), webhook.index("terraform_update_apply webhook-ingress"))

    def test_agent_context_manifests_receive_the_environment(self):
        call = DEPLOY_ALL.index('cd "$ROOT_DIR/modules/agent-context"\n  export ENVIRONMENT\n')
        self.assertLess(call, DEPLOY_ALL.index("bash deploy.sh --skip-terraform"))
        deploy_sh = (ROOT / "modules/agent-context/deploy.sh").read_text()
        self.assertRegex(deploy_sh, r'ENVIRONMENT="\$\{ENVIRONMENT:-dev\}"')

    def test_docs_name_the_self_managed_owner_for_each_operator_item(self):
        doc = (ROOT / "docs/adp-platform-deployment/deployment-reference.md").read_text()
        section = doc[doc.index("## Operator-set configuration"):doc.index("## When it breaks")]
        self.assertNotIn("platform/automation-infra/runtime-verifier-access.tf", section)
        self.assertIn("platform/infra/runtime-verifier-access.tf", section)
        self.assertIn("deploy-chat-scaledjob.sh", section)
        self.assertNotIn("Not yet in deploy-all.sh", section)
        self.assertIn("--chat delegated", section)
        self.assertIn("apply-chat-tls.sh", section)
        self.assertIn("/adp/<env>/gateway/chat-sandbox-ca-configmap", section)
        self.assertIn("adp/<env>/chat-sandbox-gateway/issuer", section)
        self.assertIn("### Model policy posture for chat (#7997)", section)
        posture = section[section.index("### Model policy posture for chat"):section.index("### Model invocability probing")]
        for item in ("chat_model_policy_posture", "/adp/<env>/gateway/chat-model-policy-posture", "BG_CHAT_MODEL_POLICY_POSTURE",
                     "`advisory`", "`enforced`", "`off`", "not_permitted", "modules/gateway/infra/chief-of-staff-flags.tf"):
            self.assertIn(item, posture)
        self.assertIn("### Model invocability probing (required when the chat posture is `enforced`)", section)
        self.assertNotIn("### Model invocability probing (required for delegated chat)", section)
        for item in ("model_probe_enabled", "model_probe_evidence_ttl_hours", "persona_model_probe_enabled",
                     "run-probe-cycle.sh", "probing_disabled", "modules/gateway/infra/model-probe.tf"):
            self.assertIn(item, section)
        self.assertNotIn("| `adp-deploy-<env>` | `ADP_DEPLOY_ROLE_ARN`", section)

    # Dev Box (#7998): an OPTIONAL module. Off by default, on for dev through its
    # tfvars, ordered after chat; deploy-all.sh owns only the switch, the journal
    # note and one call into the module's entrypoint.
    def test_dev_box_stage_is_optional_and_module_owned(self):
        self.assertIn('--dev-box) DEV_BOX_MODE=', DEPLOY_ALL)
        self.assertIn('--dev-box-only) DEV_BOX_ONLY=true; DEV_BOX_MODE=on ;;', DEPLOY_ALL)
        self.assertIn('DEV_BOX_MODE="${ADP_DEV_BOX:-}"', DEPLOY_ALL)
        # Journal note only when selected: a default run's journal is unchanged.
        note = DEPLOY_ALL.index('deploy_checkpoint note dev-box-mode "$DEV_BOX_MODE"')
        self.assertIn('if [ "$DEV_BOX_MODE" = on ]; then', DEPLOY_ALL[DEPLOY_ALL.rindex("\n", 0, note - 1) - 60:note])
        # One call, inside the on-branch, after the agent factory and chat.
        entrypoint = 'bash "$ROOT_DIR/modules/user-services/dev-box/scripts/deploy-dev-box.sh"'
        self.assertEqual(DEPLOY_ALL.count(entrypoint), 1)
        step = DEPLOY_ALL.index('if deploy_phase_begin dev-box; then')
        self.assertLess(DEPLOY_ALL.index('step "Step 10c/11: Cache chat release image'), step)
        self.assertLess(DEPLOY_ALL.index('python3 "$SCRIPT_DIR/upgrade-chat.py" drain'), step)
        self.assertLess(step, DEPLOY_ALL.index('if deploy_phase_begin context; then'))
        block = DEPLOY_ALL[step:DEPLOY_ALL.index('if deploy_phase_begin context; then')]
        self.assertIn('if [ "$DEV_BOX_MODE" = on ]; then', block)
        self.assertIn(entrypoint, block)
        self.assertIn('step "Step 12 (optional): Skipping Dev Box (--dev-box on enables it)"', block)
        dev_box_block = block[:block.index("deploy_phase_complete")]
        self.assertNotRegex(dev_box_block, r"Step \d+/\d+", "the optional step carries no /N denominator")
        # Nothing Dev Box-specific lives under platform/scripts.
        self.assertFalse(list((ROOT / "platform/scripts").glob("*dev-box*")))
        self.assertFalse(list((ROOT / "platform/scripts").glob("*dev_box*")))
        for script in ("deploy-dev-box.sh", "build-dev-box-image.sh", "verify-dev-box.py", "self_managed_release.py"):
            self.assertTrue((ROOT / "modules/user-services/dev-box/scripts" / script).exists(), script)
        self.assertTrue((ROOT / "modules/user-services/dev-box/k8s/controller.yaml").exists())
        # Launcher passthrough and checkpoint phase order.
        launcher = (ROOT / "deploy.sh").read_text()
        self.assertIn("--dev-box)", launcher)
        self.assertIn("--dev-box-only)  EXTRA_ARGS+=", launcher)
        phases = runpy.run_path(str(ROOT / "platform/scripts/deploy-checkpoints.py"))["PHASES"]
        # Other optional modules may run between these phases; Dev Box must
        # still follow the factory and precede context/finalization exactly once.
        required_order = ["webhook", "factory", "dev-box", "context", "finalize"]
        self.assertEqual([phase for phase in phases if phase in required_order], required_order)
        self.assertIn("${DEV_BOX_MODE:-off}:${DEV_BOX_ONLY:-false}", (ROOT / "platform/scripts/deploy-checkpoints.sh").read_text())

    def _dev_box_gate(self):
        # Defaults + option parser (shared with the chat gate), then the Dev Box gate
        # itself, which sits after the chat gate and before the mutual-exclusion checks.
        start = DEPLOY_ALL.index('DEV_BOX_MODE="${ADP_DEV_BOX:-}"')
        start = DEPLOY_ALL.rindex("\n", 0, start) + 1
        parser_end = DEPLOY_ALL.index("export AWS_DEFAULT_REGION=\"$AWS_REGION\" ADP_REGION=\"$AWS_REGION\"")
        gate_start = DEPLOY_ALL.index("# Dev Box mode (#7998)")
        gate_start = DEPLOY_ALL.rindex("\n", 0, gate_start) + 1
        gate_end = DEPLOY_ALL.index("# Mutual exclusion checks")
        gate_end = DEPLOY_ALL.rindex("\n", 0, gate_end) + 1
        return ("set -euo pipefail\nAWS_REGION=us-east-1\n"
                "ok() { echo \"ok: $1\"; }; warn() { echo \"warn: $1\"; }; fail() { echo \"fail: $1\" >&2; exit 1; }\n"
                + DEPLOY_ALL[start:parser_end] + DEPLOY_ALL[gate_start:gate_end]
                + '\nprintf "dev_box=%s only=%s\\n" "$DEV_BOX_MODE" "$DEV_BOX_ONLY"\n')

    def _run_dev_box_gate(self, root, *args, env=None):
        child = {k: v for k, v in os.environ.items() if not k.startswith(("ADP_CHAT_", "ADP_DEV_BOX"))}
        child.update(env or {})
        script = "ROOT_DIR=%r; ENVIRONMENT=%s\n" % (str(root), child.get("ENVIRONMENT", "dev")) + self._dev_box_gate()
        result = subprocess.run(["bash", "-c", script, "deploy-all.sh", *args], capture_output=True, text=True, env=child)
        return result.returncode, re.sub(r"\x1b\[[0-9;]*m", "", result.stdout + result.stderr)

    def test_default_run_makes_no_dev_box_call_without_the_environment_tfvars(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "environments/dev/modules").mkdir(parents=True)
            code, out = self._run_dev_box_gate(root)
            self.assertEqual(code, 0, out)
            self.assertIn("dev_box=off only=false", out)
            self.assertIn("Dev Box: off", out)
            code, out = self._run_dev_box_gate(root, "--dev-box", "on")
            self.assertNotEqual(code, 0)
            self.assertIn("requires environments/dev/modules/dev-box.tfvars", out)
            code, out = self._run_dev_box_gate(root, "--dev-box", "maybe")
            self.assertNotEqual(code, 0)
            self.assertIn("--dev-box must be on or off", out)

    def test_dev_tfvars_turn_the_stage_on_and_the_switch_overrides_them(self):
        code, out = self._run_dev_box_gate(ROOT)
        self.assertEqual(code, 0, out)
        self.assertIn("dev_box=on only=false", out)
        self.assertIn("Dev Box: on", out)
        code, out = self._run_dev_box_gate(ROOT, "--dev-box", "off")
        self.assertEqual(code, 0, out)
        self.assertIn("dev_box=off only=false", out)
        code, out = self._run_dev_box_gate(ROOT, env={"ADP_DEV_BOX": "off"})
        self.assertIn("dev_box=off only=false", out)
        code, out = self._run_dev_box_gate(ROOT, "--dev-box-only")
        self.assertEqual(code, 0, out)
        self.assertIn("dev_box=on only=true", out)
        code, out = self._run_dev_box_gate(ROOT, "--dev-box-only", "--dev-box", "off")
        self.assertNotEqual(code, 0)
        self.assertIn("--dev-box-only cannot combine with --dev-box off", out)
        tfvars = (ROOT / "environments/dev/modules/dev-box.tfvars").read_text()
        self.assertRegex(tfvars, r"(?m)^stage_enabled\s*=\s*true")

    def _dev_box_step(self, mode, update=False):
        start = DEPLOY_ALL.index("if deploy_phase_begin dev-box; then")
        end = DEPLOY_ALL.index('if deploy_phase_begin context; then')
        block = DEPLOY_ALL[start:DEPLOY_ALL.rindex("refresh_credentials", start, end)]
        doubles = (
            "set -euo pipefail\n"
            "ENVIRONMENT=dev; AWS_REGION=us-east-1; ROOT_DIR=/repo; UPDATE_MODE=%s; CONFIRM_DESTRUCTIVE=false\n"
            "DEV_BOX_MODE=%s\n"
            "step() { echo \"STEP $1\"; }; ok() { echo \"OK $1\"; }; warn() { echo \"WARN $1\"; }; fail() { echo \"FAIL $1\"; exit 1; }\n"
            "refresh_credentials() { :; }\n"
            "deploy_phase_begin() { echo \"BEGIN $1\"; return 0; }; deploy_phase_complete() { echo COMPLETE; }\n"
            "bash() { echo \"CALL bash $*\"; }\n"
        ) % ("true" if update else "false", mode)
        result = subprocess.run(["bash", "-c", doubles + block], capture_output=True, text=True)
        return result.returncode, result.stdout + result.stderr

    def test_default_off_step_calls_nothing_and_on_calls_the_module_entrypoint_once(self):
        code, out = self._dev_box_step("off")
        self.assertEqual(code, 0, out)
        self.assertNotIn("deploy-dev-box.sh", out)
        self.assertIn("Skipping Dev Box", out)
        self.assertIn("COMPLETE", out)
        code, out = self._dev_box_step("on")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("CALL bash /repo/modules/user-services/dev-box/scripts/deploy-dev-box.sh --env dev --region us-east-1"), 1)
        code, out = self._dev_box_step("on", update=True)
        self.assertIn("deploy-dev-box.sh --env dev --region us-east-1 --update", out)

    def test_dev_box_only_scope_runs_only_that_entrypoint(self):
        scope = (ROOT / "platform/scripts/upgrade-scope.sh").read_text()
        self.assertIn('if [ "${DEV_BOX_ONLY:-false}" = true ]; then\n    DEPLOY_GATEWAY=false; DEPLOY_WEBHOOK=false; DEPLOY_FACTORY=false; DEPLOY_AGENT_CONTEXT=false', scope)
        shell = 'source "%s"; fail() { echo "$*" >&2; exit 1; }; DEV_BOX_ONLY=true resolve_deploy_scope; echo "$DEPLOY_GATEWAY $DEPLOY_WEBHOOK $DEPLOY_FACTORY $DEPLOY_AGENT_CONTEXT"' % (ROOT / "platform/scripts/upgrade-scope.sh")
        result = subprocess.run(["bash", "-c", shell], capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k not in ("UPDATE_MODE", "AGENT_CONTEXT_ENABLED")})
        self.assertEqual(result.stdout.strip(), "false false false false", result.stderr)
        # The admin bootstrap check and the manual next steps follow the other scopes.
        self.assertIn('[ "$DEV_BOX_ONLY" = false ] && [ "$SKIP_ADMIN_BOOTSTRAP" = false ]; then', DEPLOY_ALL)
        self.assertIn('for _scope in "$GATEWAY_ONLY" "$AGENT_FACTORY_ONLY" "$AGENT_CONTEXT_ONLY" "$DEV_BOX_ONLY"', DEPLOY_ALL)

    def test_docs_describe_the_optional_dev_box_stage(self):
        doc = (ROOT / "docs/adp-platform-deployment/deployment-reference.md").read_text()
        self.assertIn("## Phase 12 — Dev Box (optional module)", doc)
        section = doc[doc.index("## Operator-set configuration"):doc.index("## When it breaks")]
        self.assertIn("### Dev Box (optional module, #7998)", section)
        for item in ("--dev-box on", "--dev-box-only", "modules/user-services/dev-box/scripts/deploy-dev-box.sh",
                     "build-dev-box-image.sh", "verify-dev-box.py", "python -m src.cloud_desktop.seed", "/adp/<env>/dev-box/",
                     "environments/<env>/modules/dev-box.tfvars", "dev-box-platform.tfvars"):
            self.assertIn(item, section, item)
        readme = (ROOT / "modules/user-services/dev-box/README.md").read_text()
        self.assertIn("## Self-managed deployment in the platform account (optional module)", readme)
        self.assertIn("deploy-all.sh --dev-box on", readme)


if __name__ == "__main__":
    unittest.main()
