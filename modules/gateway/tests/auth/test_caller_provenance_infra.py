"""The infrastructure half of the provenance fix, pinned — #5653 (A01).

The application guard in ``src/auth/caller_provenance.py`` is only sound because
of things that live outside Python:

  1. API Gateway overwrites ``X-Caller-Identity`` on every route, so a value that
     survives to the pod was written by API Gateway from a verified SigV4
     signature. If a future route omits that mapping, the client's value is
     forwarded verbatim and the header becomes forgeable again on that path.
  2. ``BG_TRUST_APIGW_HEADERS`` claims control (1) is deployed. Both ConfigMap
     renderers used to hard-code it ``true``, so the claim was asserted in
     environments where nothing had established it — including the application's
     own safe default, which was overridden on every deploy.
  3. AWS_IAM routes inject an independent proof from a SecureString-backed value,
     and the pod requires it in constant time. Direct-cluster callers can assert
     the identity header but cannot produce this edge-to-application proof.

Each of those is a config-level fact that no unit test of the Python would catch,
and each fails silently: the deploy succeeds, the pods are healthy, and the
header is trusted again. These tests assert the facts directly.

Deliberately text/structure assertions with no cluster, no AWS and no terraform
binary, so they run in the ordinary gateway pre-submit suite
(``python3 -m pytest tests/``) rather than in a separate infra job that a
reviewer may never trigger.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = GATEWAY_ROOT.parents[1]

APIGW_MAIN_TF = GATEWAY_ROOT / "infra" / "modules" / "api-gateway" / "main.tf"
CONFIGMAP = GATEWAY_ROOT / "k8s" / "configmap.yaml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "gateway-deploy.yml"
DEPLOY_ALL = REPO_ROOT / "platform" / "scripts" / "deploy-all.sh"
SCALEDJOB_NETPOL = REPO_ROOT / "modules" / "agent-factory" / "webhook-ingress" / "infra" / "scaledjob-netpol.tf"
PENTEST_ACTOR_TF = REPO_ROOT / "modules" / "agent-factory" / "infra" / "pentest-actor-token.tf"
AGENT_FACTORY_OUTPUTS = REPO_ROOT / "modules" / "agent-factory" / "infra" / "outputs.tf"
COGNITO_JWT = GATEWAY_ROOT / "src" / "auth" / "cognito_jwt.py"
SHARED_CONFIG = GATEWAY_ROOT / "src" / "shared" / "config.py"
GATEWAY_INFRA_MAIN = GATEWAY_ROOT / "infra" / "main.tf"
GATEWAY_INFRA_OUTPUTS = GATEWAY_ROOT / "infra" / "outputs.tf"

CALLER_HEADER_PARAM = "integration.request.header.X-Caller-Identity"
PROVENANCE_HEADER_PARAM = "integration.request.header.X-Adp-Edge-Provenance"
TRUST_PLACEHOLDER = "__TRUST_APIGW_HEADERS__"
AGENT_CLIENTS_PLACEHOLDER = "__AGENT_CLIENTS_TABLE__"


class TestDynamicMachineClientRegistryWiring:
    def test_gateway_infra_publishes_the_authoritative_table_name(self):
        main = GATEWAY_INFRA_MAIN.read_text()
        outputs = GATEWAY_INFRA_OUTPUTS.read_text()

        assert 'resource "aws_ssm_parameter" "agent_clients_table"' in main
        assert 'name        = "/adp/${var.environment}/gateway/agent-clients-table"' in main
        assert "value       = module.cognito.agent_clients_table_name" in main
        assert 'output "agent_clients_table_name"' in outputs
        assert "value       = module.cognito.agent_clients_table_name" in outputs

    def test_both_deploy_paths_render_the_registry_table(self):
        assert f'BG_AGENT_CLIENTS_TABLE: "{AGENT_CLIENTS_PLACEHOLDER}"' in CONFIGMAP.read_text()

        workflow = DEPLOY_WORKFLOW.read_text()
        assert "/gateway/agent-clients-table" in workflow
        assert AGENT_CLIENTS_PLACEHOLDER in workflow

        deploy_all = DEPLOY_ALL.read_text()
        assert "terraform output -raw agent_clients_table_name" in deploy_all
        assert AGENT_CLIENTS_PLACEHOLDER in deploy_all

    def test_gateway_role_can_read_the_registry_table(self):
        main = GATEWAY_INFRA_MAIN.read_text()
        policy_start = main.index('resource "aws_iam_role_policy" "gateway_identity_index"')
        policy_end = main.index('\nresource "', policy_start + 1)
        policy = main[policy_start:policy_end]

        normalized_policy = re.sub(r"\s+", " ", policy)
        assert "role = local.gateway_service_irsa_role_name" in normalized_policy
        assert 'Sid = "AgentClientRegistryRead"' in normalized_policy
        assert 'Action = ["dynamodb:GetItem"]' in normalized_policy
        assert "Resource = [module.cognito.agent_clients_table_arn]" in normalized_policy


class TestEveryRouteMapsTheIdentityHeader:
    """Control 1: no route may forward a client-supplied X-Caller-Identity.

    The Terraform itself carries a plan-time ``postcondition`` that asserts this
    against the rendered OpenAPI body, which is the real enforcement — it fails
    the deploy that introduces an unmapped route. These tests are the fast
    feedback for the same invariant: they run in the ordinary Python suite, where
    no terraform binary, AWS credentials or backend state are available.
    """

    def test_postcondition_guards_the_rendered_body(self):
        """The invariant is asserted at plan time, not just reviewed by eye.

        Checked because the realistic failure is a route ADDED LATER. Reviewing
        five correct routes is easy; noticing an absent line in a sixth, months
        from now, is not.
        """
        tf = APIGW_MAIN_TF.read_text()

        assert "postcondition" in tf, "the api-gateway module must assert the #5653 route invariant at plan time"
        # It must inspect the RENDERED body (self.body), not a hand-maintained
        # list of expected paths, which would drift from what is deployed.
        assert "jsondecode(self.body)" in tf, "the postcondition must check the rendered OpenAPI body, not a duplicated path list"
        assert CALLER_HEADER_PARAM in tf
        assert PROVENANCE_HEADER_PARAM in tf

    def test_no_route_omits_the_mapping(self):
        """Every integration in the real route table maps the header.

        Parses the actual `paths` keys and their requestParameters out of the
        Terraform source rather than trusting the postcondition to be correct,
        so a postcondition that was itself broken (e.g. matching zero routes)
        cannot make this file pass vacuously.
        """
        tf = APIGW_MAIN_TF.read_text()

        # Route keys in the primary body: "/", "/{proxy+}", "/agent", ...
        routes = re.findall(r'^\s*"(/[^"]*)" = \{', tf, re.MULTILINE)
        # The MOCK placeholder body has a /status route with no ALB integration;
        # it forwards nothing to the pod, so the invariant is vacuous there.
        routes = [r for r in routes if r != "/status"]

        assert len(routes) >= 5, f"expected the full route table, found {routes}"

        # Each route's integration block must reference the header mapping, either
        # directly (context.identity.userArn) or via local.blank_caller_identity.
        for route in routes:
            start = tf.index(f'"{route}" = {{')
            nxt = [tf.index(f'"{r}" = {{') for r in routes if tf.index(f'"{r}" = {{') > start]
            block = tf[start : min(nxt)] if nxt else tf[start:]
            assert "local.verified_caller_identity" in block or "local.blank_caller_identity" in block, (
                f"route {route} does not overwrite both provenance headers"
            )

    def test_non_iam_routes_blank_rather_than_forward(self):
        """auth-NONE routes must BLANK, never map a verified identity they lack.

        ``context.identity.userArn`` is only populated where API Gateway verified
        a SigV4 signature. Mapping it on an auth-NONE route would yield an empty
        value today, but expresses the opposite intent and invites someone to
        "fix" it into a passthrough.
        """
        tf = APIGW_MAIN_TF.read_text()

        # The blanking local must map to a STATIC empty string. "''" is API
        # Gateway mapping syntax for a literal; anything else (e.g. a
        # method.request.header reference) would forward the client's value.
        match = re.search(rf'"{re.escape(CALLER_HEADER_PARAM)}"\s*=\s*"(.*?)"', tf)
        assert match is not None
        blank_local = re.search(r"blank_caller_identity = \{(.*?)\}", tf, re.DOTALL)
        assert blank_local is not None, "local.blank_caller_identity must exist"
        assert "\"''\"" in blank_local.group(1), "blanking must use API Gateway's static-literal syntax, not a header passthrough"
        assert "method.request.header" not in blank_local.group(1)
        assert PROVENANCE_HEADER_PARAM in blank_local.group(1)


class TestEdgeProofIsNotLogged:
    def test_data_tracing_is_disabled_when_edge_proof_is_injected(self):
        tf = APIGW_MAIN_TF.read_text()

        verified_mapping = re.search(r"verified_caller_identity = \{(.*?)\}", tf, re.DOTALL)
        assert verified_mapping is not None
        assert PROVENANCE_HEADER_PARAM in verified_mapping.group(1)
        assert "random_password.edge_provenance.result" in verified_mapping.group(1)

        settings_start = tf.index('resource "aws_api_gateway_method_settings" "all"')
        settings_end = tf.index('\nresource "', settings_start + 1)
        method_settings = tf[settings_start:settings_end]
        assert re.search(r"data_trace_enabled\s*=\s*var.enable_payload_tracing", method_settings)
        variables = (APIGW_MAIN_TF.parent / "variables.tf").read_text()
        tracing = re.search(r'variable "enable_payload_tracing" \{(.*?)\n\}', variables, re.DOTALL).group(1)
        assert re.search(r"default\s*=\s*false", tracing)


class TestTrustFlagIsNotHardCoded:
    """Control 2: the flag must reflect deployed reality, not a literal."""

    def test_terraform_publishes_the_flag(self):
        """The param is published by the module that installs the blanking.

        Coupling them is the point: "trust the header" is a claim about the edge,
        so the edge is what gets to make it.
        """
        tf = APIGW_MAIN_TF.read_text()
        assert "trust-apigw-headers" in tf, "the api-gateway module must publish the trust flag it justifies"
        assert 'resource "aws_ssm_parameter" "trust_apigw_headers"' in tf

    def test_renderers_read_the_param_instead_of_forcing_true(self):
        """Neither ConfigMap renderer may hard-code the flag on.

        Both are checked because they are independent implementations of the same
        rendering step (CI workflow and self-managed script). Fixing one and
        leaving the other is the realistic regression, and it is invisible when
        reviewing either file alone.
        """
        for path in (DEPLOY_WORKFLOW, DEPLOY_ALL):
            text = path.read_text()
            assert TRUST_PLACEHOLDER in text, f"{path.name} should still render the ConfigMap placeholder"

            substitution = re.search(rf"s\|{re.escape(TRUST_PLACEHOLDER)}\|([^|]*)\|g", text)
            assert substitution is not None, f"{path.name} must substitute {TRUST_PLACEHOLDER}"
            value = substitution.group(1)

            assert value.strip() != "true", (
                f"{path.name} hard-codes the trust flag to true, which overrides the "
                "application's safe default on every deploy regardless of whether the "
                "edge blanking exists"
            )
            assert "TRUST_APIGW_HEADERS" in value, f"{path.name} must render the value read from SSM"
            assert "trust-apigw-headers" in text, f"{path.name} must read the flag from the SSM param the edge module publishes"

    def test_renderers_default_to_false_when_the_param_is_absent(self):
        """An environment without the edge apply must not trust the header.

        `aws ssm get-parameter` on a missing name yields the fallback, and the
        AWS CLI can also emit the literal "None" — both must land on false, or a
        fresh environment silently honours forged assertions.
        """
        for path in (DEPLOY_WORKFLOW, DEPLOY_ALL):
            text = path.read_text()
            read = re.search(r'TRUST_APIGW_HEADERS=\$\(_?get_ssm "[^"]*trust-apigw-headers" "([^"]*)"\)', text)
            assert read is not None, f"{path.name} must read the trust param with an explicit fallback"
            assert read.group(1) == "false", f"{path.name} must default the trust flag to false, not {read.group(1)!r}"
            assert 'TRUST_APIGW_HEADERS" = "None"' in text, f"{path.name} must coerce the CLI's literal None to false"

    def test_configmap_still_takes_the_value_from_the_renderer(self):
        """The ConfigMap must not reintroduce a literal of its own."""
        text = CONFIGMAP.read_text()
        assert f'BG_TRUST_APIGW_HEADERS: "{TRUST_PLACEHOLDER}"' in text


class TestAgentEgressDefenseInDepth:
    """The agent egress policy remains useful defense in depth.

    Authentication no longer depends on this policy. It still reduces direct
    connectivity from workloads that process untrusted repository content.

    A test guards it because the control is the ABSENCE of a rule. Someone
    "repairing" the removed pod->gateway rule reopens a direct path from pods
    that process untrusted repository and issue content by design — and nothing
    else in the tree would fail.
    """

    def test_agent_pods_have_namespace_wide_default_deny_egress(self):
        tf = SCALEDJOB_NETPOL.read_text()
        assert 'kubernetes_network_policy" "default_deny_egress"' in tf
        assert "pod_selector {}" in tf, "the default-deny must select every pod in the namespace"

    def test_agent_egress_allowlist_has_no_in_cluster_route_to_the_gateway(self):
        """No egress rule may target the GATEWAY namespace or pods.

        Scoped to naming the gateway, not to "no selectors at all": this policy
        legitimately selects the `agent-context` namespace on port 5100 for the
        Knowledge Layer MCP endpoint (#3286). A blanket ban on `namespace_selector`
        would fail on that unrelated rule and would have to be deleted to go
        green — which is how a guard stops guarding.
        """
        tf = SCALEDJOB_NETPOL.read_text()

        # Bound to this resource. `agent_control_listener_ingress` further down the
        # same file legitimately names the gateway namespace — it is the INGRESS
        # direction (gateway -> agent control listener on 8770), the opposite of
        # the path under test, and must not be caught here.
        start = tf.index('kubernetes_network_policy" "agent_scaledjob_egress"')
        end = tf.index('resource "kubernetes_network_policy"', start + 1)
        block = tf[start:end]

        # Strip comments: the file discusses the removed rule at length, and the
        # prose naming `adp-gateway` must not be mistaken for a live rule.
        code = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))

        assert "bedrockgateway" not in code
        assert "adp-gateway" not in code
        assert "gateway_namespace" not in code

    def test_the_omission_is_documented_as_deliberate(self):
        """The comment is part of the control.

        Without it the missing rule looks like an oversight, and the next reader
        adds it back. This asserts the rationale survives, not merely that some
        comment exists.
        """
        tf = SCALEDJOB_NETPOL.read_text()
        assert "NO in-cluster path to the gateway" in tf
        assert "X-Caller-Identity" in tf, "the omission must stay linked to the header it protects"


class TestApplicationGuardUsesRequestProof:
    """The application guard must not depend on network position.

    An earlier cut of this fix pointed at a gateway-side `k8s/networkpolicy.yaml`
    as the control closing the in-cluster bypass. That file was never written,
    and — more importantly — could not have worked: it would have been decoration
    in an auth path, which is worse than nothing because the next reader trusts
    it. These assertions keep the documented boundary matched to the deployed one.
    """

    def test_no_reference_to_a_nonexistent_gateway_networkpolicy(self):
        gateway_netpol = GATEWAY_ROOT / "k8s" / "networkpolicy.yaml"
        referrers = [
            GATEWAY_ROOT / "src" / "auth" / "caller_provenance.py",
            GATEWAY_ROOT / "src" / "internal" / "auth_deps.py",
        ]
        for path in referrers:
            text = path.read_text()
            if "k8s/networkpolicy.yaml" in text:
                assert gateway_netpol.exists(), f"{path.name} cites k8s/networkpolicy.yaml, which does not exist"

    def test_module_requires_the_edge_provenance_header(self):
        text = (GATEWAY_ROOT / "src" / "auth" / "caller_provenance.py").read_text()
        assert "X-Adp-Edge-Provenance" in text
        assert "compare_digest" in text

    def test_module_does_not_depend_on_networkpolicy_enforcement(self):
        text = (GATEWAY_ROOT / "src" / "auth" / "caller_provenance.py").read_text()
        assert "NetworkPolicy" not in text


class TestPentestClientDeploymentContract:
    """The dev pentest client remains explicit without widening the pool policy."""

    PARAMETER_NAME = "/adp/${var.environment}/gateway/cognito-pentest-client-id"
    SETTING_NAME = "cognito_pentest_client_id"
    PLACEHOLDER = "__COGNITO_PENTEST_CLIENT_ID__"

    def test_agent_factory_publishes_only_the_dev_fenced_client(self):
        text = PENTEST_ACTOR_TF.read_text()
        start = text.index('resource "aws_ssm_parameter" "gateway_pentest_client_id"')
        block = text[start:]

        assert 'pentest_actor_token_enabled = var.environment == "dev" && var.gateway_deployed' in text
        assert "count = local.pentest_actor_token_enabled ? 1 : 0" in block
        assert f'name        = "{self.PARAMETER_NAME}"' in block
        assert "value       = module.pentest_actor_token[0].pentest_client_id" in block

    def test_gateway_runtime_allowlist_includes_only_the_configured_value(self):
        assert f'{self.SETTING_NAME}: str = ""' in SHARED_CONFIG.read_text()
        validator = COGNITO_JWT.read_text()
        assert f'getattr(settings, "{self.SETTING_NAME}", "")' in validator

    def test_both_deploy_paths_render_the_optional_setting(self):
        assert f'BG_COGNITO_PENTEST_CLIENT_ID: "{self.PLACEHOLDER}"' in CONFIGMAP.read_text()
        for path in (DEPLOY_WORKFLOW, DEPLOY_ALL):
            text = path.read_text()
            assert "gateway/cognito-pentest-client-id" in text
            assert self.PLACEHOLDER in text

    def test_one_pass_deploy_reconciles_gateway_after_factory_apply(self):
        outputs = AGENT_FACTORY_OUTPUTS.read_text()
        output_start = outputs.index('output "pentest_actor_client_id"')
        output_block = outputs[output_start : outputs.index("\n}", output_start)]
        assert "local.pentest_actor_token_enabled" in output_block
        assert "module.pentest_actor_token[0].pentest_client_id" in output_block

        deploy = DEPLOY_ALL.read_text()
        factory_start = deploy.index('step "Step 10/11: Deploy agent-factory"')
        factory_apply = deploy.index("terraform apply -var-file=terraform.tfvars -auto-approve", factory_start)
        client_output = deploy.index("terraform output -raw pentest_actor_client_id", factory_apply)
        configmap_patch = deploy.index("kubectl patch configmap bedrockgateway-config", client_output)
        rollout_restart = deploy.index("kubectl rollout restart deployment/bedrockgateway", configmap_patch)
        rollout_status = deploy.index("wait_for_gateway_rollout", rollout_restart)
        agent_gateway = deploy.index('step "Step 10b/11: Build and deploy agent gateway"', rollout_status)

        assert factory_apply < client_output < configmap_patch < rollout_restart < rollout_status < agent_gateway
        reconciliation = deploy[factory_apply:agent_gateway]
        assert '[ "$DEPLOY_GATEWAY" = true ] && [ "$ENVIRONMENT" = "dev" ]' in reconciliation
        assert "BG_COGNITO_PENTEST_CLIENT_ID" in reconciliation
        assert 'fail "Agent-factory deployed without publishing the dev pentest Cognito client ID"' in reconciliation

    def test_one_pass_reconciliation_populates_configmap_and_restarts_gateway(self):
        deploy = DEPLOY_ALL.read_text()
        factory_start = deploy.index('step "Step 10/11: Deploy agent-factory"')
        start = deploy.index('  if [ "$DEPLOY_GATEWAY" = true ] && [ "$ENVIRONMENT" = "dev" ]; then', factory_start)
        end = deploy.index("\n  fi\n\n  # --- Agent Gateway build", start) + len("\n  fi")
        reconciliation = deploy[start:end]
        harness = r"""
set -euo pipefail
DEPLOY_GATEWAY=true
ENVIRONMENT=dev
terraform() { printf '%s' 'pentest-client-123'; }
kubectl() { printf 'kubectl'; printf ' <%s>' "$@"; printf '\n'; }
fail() { printf 'FAIL: %s\n' "$1"; exit 1; }
ok() { printf 'OK: %s\n' "$1"; }
"""

        result = subprocess.run(
            ["bash", "-c", harness + (DEPLOY_ALL.parent / "gateway-rollout.sh").read_text() + "\n" + reconciliation],
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert 'BG_COGNITO_PENTEST_CLIENT_ID": "pentest-client-123' in result.stdout
        assert "kubectl <rollout> <restart> <deployment/bedrockgateway>" in result.stdout
        assert "kubectl <rollout> <status> <deployment/bedrockgateway>" in result.stdout


class TestEdgeProvenanceSecret:
    def test_iam_routes_set_identity_and_independent_proof(self):
        tf = APIGW_MAIN_TF.read_text()
        verified_local = re.search(r"verified_caller_identity = \{(.*?)\}", tf, re.DOTALL)

        assert verified_local is not None
        assert '"context.identity.userArn"' in verified_local.group(1)
        assert PROVENANCE_HEADER_PARAM in verified_local.group(1)
        assert "random_password.edge_provenance.result" in verified_local.group(1)

    def test_proof_is_stored_as_secure_ssm_parameter(self):
        tf = APIGW_MAIN_TF.read_text()

        assert 'resource "aws_ssm_parameter" "edge_provenance_secret"' in tf
        assert 'type        = "SecureString"' in tf
        assert "/gateway/apigw-provenance-secret" in tf

    def test_both_deploy_paths_load_the_proof_into_a_kubernetes_secret(self):
        for path in (DEPLOY_WORKFLOW, DEPLOY_ALL):
            text = path.read_text()
            assert "--with-decryption" in text
            assert '--from-literal=apigw-provenance-secret="$APIGW_PROVENANCE_SECRET"' in text
            assert "header trust is enabled but its provenance secret is unavailable" in text
