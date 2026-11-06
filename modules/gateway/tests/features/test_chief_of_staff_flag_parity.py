"""Chief-of-Staff surfaces are configured per environment, in code, in every renderer (#7197).

FEATURE_CHAT_ENABLED was a committed literal and the assistant/Crew flags were unset,
so the delivered chat, assistant and Crew surfaces could not be enabled on dev
without editing the manifest, and a new environment would miss them entirely. The
flags now flow: gateway infra Terraform (SSM parameter, value from tfvars) ->
gateway-deploy.yml / deploy-all.sh (read + render) -> deployment.yaml placeholder.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
DEPLOYMENT = REPO_ROOT / "modules/gateway/k8s/deployment.yaml"
WORKFLOW = REPO_ROOT / ".github/workflows/gateway-deploy.yml"
DEPLOY_ALL = REPO_ROOT / "platform/scripts/deploy-all.sh"
CONFIGMAP = REPO_ROOT / "modules/gateway/k8s/configmap.yaml"
INFRA = REPO_ROOT / "modules/gateway/infra/chief-of-staff-flags.tf"
CREW_CHECKPOINTS_INFRA = REPO_ROOT / "modules/gateway/infra/crew-checkpoints.tf"
AGENTCORE_MEMORY_INFRA = REPO_ROOT / "modules/gateway/infra/agentcore-memory.tf"
TOOLS_CONSENT_INFRA = REPO_ROOT / "modules/gateway/infra/tools-consent.tf"
GITHUB_USER_ACCESS_INFRA = REPO_ROOT / "modules/gateway/infra/github-user-access.tf"
CREW_DASHBOARD_INFRA = REPO_ROOT / "modules/gateway/infra/crew-dashboard.tf"
DEV_TFVARS = REPO_ROOT / "environments/dev/modules/gateway.tfvars"
MODEL_PROBE_INFRA = REPO_ROOT / "modules/gateway/infra/model-probe.tf"
DEV_WEBHOOK_TFVARS = REPO_ROOT / "environments/dev/modules/webhook-ingress.tfvars"
RELEASE_CONFIGURATION = REPO_ROOT / "modules/gateway/infra/release-configuration.tf"
AGENT_FACTORY_CHAT_INFRA = REPO_ROOT / "modules/agent-factory/infra/chat-agent-infra.tf"
GATEWAY_INTAKE_ACCESS = REPO_ROOT / "modules/agent-factory/infra/gateway-intake-access.tf"
PRESERVED_INPUTS = REPO_ROOT / "config/release-defaults/preserved-inputs.json"

FLAGS = {
    "FEATURE_CHAT_ENABLED": "feature-chat",
    "FEATURE_ASSISTANT_UI_ENABLED": "feature-assistant-ui",
    "FEATURE_CREW_DASHBOARD": "feature-crew-dashboard",
    "CREW_PROXY_ENABLED": "crew-proxy-enabled",
    "CREW_DASHBOARD_ORIGIN": "crew-dashboard-origin",
}


@pytest.mark.parametrize("env_var", sorted(FLAGS))
def test_manifest_uses_a_rendered_value_not_a_cross_environment_literal(env_var):
    active = [line.strip() for line in DEPLOYMENT.read_text().splitlines() if line.strip().startswith(("- name:", "value:"))]
    index = active.index(f"- name: {env_var}")
    assert active[index + 1] == f'value: "__{env_var}__"'


@pytest.mark.parametrize("renderer", [WORKFLOW, DEPLOY_ALL], ids=["gateway-deploy.yml", "deploy-all.sh"])
@pytest.mark.parametrize("env_var", sorted(FLAGS))
def test_both_renderers_read_the_same_parameter_and_render_it(renderer, env_var):
    source = renderer.read_text()
    assert f'"/adp/${{ENVIRONMENT}}/gateway/{FLAGS[env_var]}"' in source, f"{renderer.name} must read {FLAGS[env_var]}"
    assert f"s|__{env_var}__|${{{env_var}}}|g" in source, f"{renderer.name} must render __{env_var}__"


@pytest.mark.parametrize(
    "parameter",
    [
        "feature-chat",
        "feature-assistant-ui",
        "feature-crew-dashboard",
        "crew-proxy-enabled",
        "chat-authority-worker-images",
        "chat-model-policy-posture",
    ],
)
def test_gateway_infra_declares_the_parameter(parameter):
    """A new environment gets the parameter from Terraform, not from an operator's shell."""
    assert f'"/adp/${{var.environment}}/gateway/{parameter}"' in INFRA.read_text()


# ConfigMap values behind the gateway-infra switches crew_dashboard_enabled /
# crew_checkpoints_enabled (#7902) and agentcore_memory_enabled (#7901): env var ->
# (SSM parameter, Terraform file that declares it). Flipping the switch and
# re-running either renderer is all an operator does; nothing is edited by hand.
CONFIGMAP_VALUES = {
    "CREW_PROXY_KEEPALIVE_SECONDS": ("crew-proxy-keepalive-seconds", INFRA),
    "CREW_PROXY_MAX_CONN_PER_USER": ("crew-proxy-max-conn-per-user", INFRA),
    # #8223 cause 20: the Dev Box controller only sees the ConfigMap, so the Crew dashboard
    # switch and edge origin are rendered there too (the Deployment env keeps its own copy).
    "FEATURE_CREW_DASHBOARD": ("feature-crew-dashboard", INFRA),
    "CREW_DASHBOARD_ORIGIN": ("crew-dashboard-origin", CREW_DASHBOARD_INFRA),
    "ADP_CREW_CHECKPOINTS_ENABLED": ("crew-checkpoints-enabled", CREW_CHECKPOINTS_INFRA),
    "ADP_CREW_CHECKPOINT_BUCKET": ("crew-checkpoint-bucket", CREW_CHECKPOINTS_INFRA),
    "ADP_CREW_CHECKPOINT_RETENTION_SECONDS": ("crew-checkpoint-retention-seconds", CREW_CHECKPOINTS_INFRA),
    "ADP_AGENTCORE_MEMORY_ID": ("agentcore-memory-id", AGENTCORE_MEMORY_INFRA),
    "ADP_AGENTCORE_MEMORY_STRATEGY_ID": ("agentcore-memory-strategy-id", AGENTCORE_MEMORY_INFRA),
    "ADP_AGENTCORE_MEMORY_REGION": ("agentcore-memory-region", AGENTCORE_MEMORY_INFRA),
    "ADP_TOOLS_CONSENT_DEFAULT": ("tools-consent-default", TOOLS_CONSENT_INFRA),
    # #8555: Dev Box gh/git and `adp github token` as the owner; off unless the environment says so.
    "ADP_GITHUB_USER_ACCESS_ENABLED": ("github-user-access-enabled", GITHUB_USER_ACCESS_INFRA),
}

MEMORY_ENV = ("ADP_AGENTCORE_MEMORY_ID", "ADP_AGENTCORE_MEMORY_STRATEGY_ID", "ADP_AGENTCORE_MEMORY_REGION", "ADP_AGENTCORE_NAMESPACE_KEY")


@pytest.mark.parametrize("env_var", sorted(CONFIGMAP_VALUES))
def test_configmap_carries_a_placeholder_for_each_switch_backed_value(env_var):
    assert f'{env_var}: "__{env_var}__"' in CONFIGMAP.read_text()


@pytest.mark.parametrize("renderer", [WORKFLOW, DEPLOY_ALL], ids=["gateway-deploy.yml", "deploy-all.sh"])
@pytest.mark.parametrize("env_var", sorted(CONFIGMAP_VALUES))
def test_both_renderers_read_the_same_parameter_into_the_configmap(renderer, env_var):
    parameter, infra = CONFIGMAP_VALUES[env_var]
    source = renderer.read_text()
    getter = "get_ssm" if renderer is WORKFLOW else "_get_ssm"
    assert f'{env_var}=$({getter} "/adp/${{ENVIRONMENT}}/gateway/{parameter}"' in source, f"{renderer.name} must read {parameter}"
    assert f"s|__{env_var}__|${{{env_var}}}|g" in source, f"{renderer.name} must render __{env_var}__"
    assert f'"/adp/${{var.environment}}/gateway/{parameter}"' in infra.read_text(), f"{infra.name} must declare {parameter}"


@pytest.mark.parametrize("renderer", [WORKFLOW, DEPLOY_ALL], ids=["gateway-deploy.yml", "deploy-all.sh"])
def test_both_renderers_apply_the_same_defaults_when_a_parameter_is_absent(renderer):
    """Absent parameter => the code's own default (25 s, 20 connections, checkpoints off, 30 days)."""
    source = renderer.read_text()
    assert '/gateway/crew-proxy-keepalive-seconds" "25")' in source
    assert '/gateway/crew-proxy-max-conn-per-user" "64")' in source
    assert '/gateway/crew-checkpoints-enabled" "false")' in source
    assert '/gateway/github-user-access-enabled" "false")' in source
    # #8223 cause 20: dashboard switch off and no origin unless gateway infra published them.
    assert '/gateway/feature-crew-dashboard" "false")' in source
    assert '/gateway/crew-dashboard-origin" "")' in source
    # #8387: consent is granted by default; the Terraform default agrees.
    assert '/gateway/tools-consent-default" "granted")' in source
    assert 'default     = "granted"' in TOOLS_CONSENT_INFRA.read_text()
    assert '/gateway/crew-checkpoint-bucket" "")' in source
    assert '/gateway/crew-checkpoint-retention-seconds" "2592000")' in source
    getter = "get_ssm" if renderer is WORKFLOW else "_get_ssm"
    for env_var in MEMORY_ENV[:3]:
        assert f'{env_var}=$({getter} "/adp/${{ENVIRONMENT}}/gateway/{CONFIGMAP_VALUES[env_var][0]}" "")' in source


@pytest.mark.parametrize("renderer", [WORKFLOW, DEPLOY_ALL], ids=["gateway-deploy.yml", "deploy-all.sh"])
def test_uvicorn_ws_ping_follows_the_keepalive_parameter_and_is_dropped_at_zero(renderer):
    """The browser-hop ping is uvicorn's; 0 must unset it, not render "0" (a tight loop)."""
    source = renderer.read_text()
    getter = "get_ssm" if renderer is WORKFLOW else "_get_ssm"
    assert f'UVICORN_WS_PING_INTERVAL=$({getter} "/adp/${{ENVIRONMENT}}/gateway/crew-proxy-keepalive-seconds" "25")' in source
    assert "UVICORN_WS_PING_INTERVAL_SED='/^ *- name: UVICORN_WS_PING_INTERVAL$/{N;d;}'" in source
    assert 'UVICORN_WS_PING_INTERVAL_SED="s|__UVICORN_WS_PING_INTERVAL__|${UVICORN_WS_PING_INTERVAL}|g"' in source
    assert '-e "$UVICORN_WS_PING_INTERVAL_SED"' in source
    active = [line.strip() for line in DEPLOYMENT.read_text().splitlines() if line.strip().startswith(("- name:", "value:"))]
    assert active[active.index("- name: UVICORN_WS_PING_INTERVAL") + 1] == 'value: "__UVICORN_WS_PING_INTERVAL__"'


@pytest.mark.parametrize("renderer", [WORKFLOW, DEPLOY_ALL], ids=["gateway-deploy.yml", "deploy-all.sh"])
def test_namespace_key_is_decrypted_into_the_secret_and_a_partial_memory_target_is_refused(renderer):
    """All four memory values together or none: a partial set is "memory target unconfigured"."""
    source = renderer.read_text()
    read = source.index('--name "/adp/${ENVIRONMENT}/gateway/agentcore-namespace-key"')
    assert "--with-decryption" in source[read : read + 200]
    assert "ADP_AGENTCORE_NAMESPACE_KEY=$(aws ssm get-parameter" in source
    assert '--from-literal=agentcore-namespace-key="$ADP_AGENTCORE_NAMESPACE_KEY"' in source
    assert "s|__ADP_AGENTCORE_NAMESPACE_KEY__|" not in source, "the namespace key never goes through the ConfigMap sed chain"
    for env_var in MEMORY_ENV:
        assert f"for {'name' if renderer is WORKFLOW else '_name'} in {' '.join(MEMORY_ENV)}; do" in source, env_var
    assert '-ne 0 ] && [ "$' in source and '" -ne 4 ]; then' in source
    assert "AgentCore memory is partially configured" in source
    assert "differs from the gateway region" in source
    configmap = CONFIGMAP.read_text()
    assert "ADP_AGENTCORE_NAMESPACE_KEY:" not in configmap


def test_dev_tfvars_arms_only_the_switches_the_owner_turned_on():
    """Dev enables the Crew dashboard in code (#7938, owner go 2026-10-16) and Crew checkpoints
    (#8295, owner go 2026-10-19: the Crew host's restore-context route sits behind the switch,
    #8223); memory stays documented but commented until the operator flips it."""
    source = DEV_TFVARS.read_text()
    active = [line.strip() for line in source.splitlines() if not line.lstrip().startswith("#")]
    assert any(line.startswith("crew_dashboard_enabled") and line.endswith("true") for line in active)
    assert any(line.startswith("crew_dashboard_edge") and line.endswith('"cloudfront"') for line in active)
    assert any(line.startswith("crew_checkpoints_enabled") and line.endswith("true") for line in active)
    for switch in ("agentcore_memory_enabled",):
        assert f"# {switch} = true" in source, switch
        assert not any(line.startswith(f"{switch} ") for line in active), f"{switch} must stay commented"


def test_dev_turns_chat_and_assistant_on_in_tfvars_not_by_hand():
    source = DEV_TFVARS.read_text()
    assert "feature_chat_enabled         = true" in source
    assert "feature_assistant_ui_enabled = true" in source
    assert "chat_authority_worker_images = [" in source


def test_model_root_bindings_are_declared_and_dev_binds_the_ingest_producer():
    """Model policy refuses every producer until a binding names it (#7899 cutover finding)."""
    infra = INFRA.read_text()
    assert 'resource "aws_ssm_parameter" "model_root_bindings"' in infra
    assert 'name = "/adp/${var.environment}/gateway/model-root-bindings"' in infra
    tfvars = DEV_TFVARS.read_text()
    assert "model_root_bindings = [{" in tfvars
    assert 'producer_role        = "arn:aws:iam::879318057152:role/adp-dev-agent-gateway-ingest"' in tfvars
    assert 'chat_supervisor_role = "arn:aws:iam::879318057152:role/adp-dev-chat-supervisor-role"' in tfvars
    assert '"intent-refinement"' in tfvars
    for renderer in (WORKFLOW, DEPLOY_ALL):
        assert 'get_authority_ssm "/adp/${ENVIRONMENT}/gateway/model-root-bindings"' in renderer.read_text()
    assert '"aws_ssm_parameter.model_root_bindings=/adp/${ENVIRONMENT}/gateway/model-root-bindings"' in DEPLOY_ALL.read_text()


def test_authority_table_falls_back_to_its_deterministic_name_in_both_renderers():
    """A renderer that cannot decrypt the SecureString must not blank AGENT_AUTHORITY_TABLE."""
    for renderer in (WORKFLOW, DEPLOY_ALL):
        assert (
            'get_authority_ssm "/adp/${ENVIRONMENT}/webhook-ingress/agent-authority-table" "adp-${ENVIRONMENT}-agent-authority"'
        ) in renderer.read_text()


def test_chat_data_switch_is_declared_rendered_and_published_by_delegated_mode():
    """The chat data plane answers 503 chat_data_disabled unless ADP_CHAT_DATA_ENABLED is true (dev finding 2026-10-16)."""
    assert 'resource "aws_ssm_parameter" "chat_data_enabled"' in INFRA.read_text()
    assert 'ADP_CHAT_DATA_ENABLED: "__ADP_CHAT_DATA_ENABLED__"' in (REPO_ROOT / "modules/gateway/k8s/configmap.yaml").read_text()
    for renderer in (WORKFLOW, DEPLOY_ALL):
        text = renderer.read_text()
        assert '/adp/${ENVIRONMENT}/gateway/chat-data-enabled" "false"' in text
        assert "s|__ADP_CHAT_DATA_ENABLED__|${ADP_CHAT_DATA_ENABLED}|g" in text
    # One publisher on both deploy paths (#8086): the ScaledJob rollout script publishes the
    # switch and the matching ConfigMap key right after the ScaledJob apply; deploy-all.sh's
    # delegated stage invokes that script instead of publishing itself.
    scaledjob_script = (REPO_ROOT / "modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh").read_text()
    assert "publish_chat_value chat-data-enabled true\n" in scaledjob_script
    assert 'parameter="/adp/${ENVIRONMENT}/gateway/$1"' in scaledjob_script
    assert '"ADP_CHAT_DATA_ENABLED": "true"' in scaledjob_script
    assert scaledjob_script.index('kubectl apply -f "$WORK/objects.json"') < scaledjob_script.index("publish_chat_value chat-data-enabled true")
    deploy_all = DEPLOY_ALL.read_text()
    delegated = deploy_all[deploy_all.index('step "Step 10c/11: Delegated chat rollout') : deploy_all.index('ok "Delegated chat ScaledJob is Ready')]
    assert 'ADP_CHAT_CREW_CONTROLS_ONLY=false \\\n      bash "$ROOT_DIR/modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh"' in delegated
    assert "chat-data-enabled=true" in delegated[delegated.index("deploy-chat-scaledjob.sh") :], "the run log still names the published switch"
    assert "ssm put-parameter" not in deploy_all
    assert '"ADP_CHAT_DATA_ENABLED": "true"' not in deploy_all


# Model invocability probing (#7967): ConfigMap key -> (placeholder, SSM parameter,
# Terraform variable, renderer default). The model-policy plane fails closed without
# probe evidence, so these were the one switch set an environment could never arm:
# committed literals in configmap.yaml. They now flow like every other switch.
MODEL_PROBE_VALUES = {
    "BG_MODEL_PROBE_ENABLED": ("MODEL_PROBE_ENABLED", "model-probe-enabled", "model_probe_enabled", "false"),
    "BG_MODEL_PROBE_MAX_SLOTS_PER_CYCLE": (
        "MODEL_PROBE_MAX_SLOTS_PER_CYCLE",
        "model-probe-max-slots-per-cycle",
        "model_probe_max_slots_per_cycle",
        "0",
    ),
    "BG_MODEL_PROBE_BUDGET_USD_PER_CYCLE": (
        "MODEL_PROBE_BUDGET_USD_PER_CYCLE",
        "model-probe-budget-usd-per-cycle",
        "model_probe_budget_usd_per_cycle",
        "0",
    ),
    "BG_MODEL_PROBE_MAX_BUDGET_USD_PER_ATTEMPT": (
        "MODEL_PROBE_MAX_BUDGET_USD_PER_ATTEMPT",
        "model-probe-max-budget-usd-per-attempt",
        "model_probe_max_budget_usd_per_attempt",
        "0",
    ),
    "BG_MODEL_PROBE_MODEL_ALLOWLIST": ("MODEL_PROBE_MODEL_ALLOWLIST", "model-probe-model-allowlist", "model_probe_model_allowlist", "[]"),
    "BG_MODEL_PROBE_EVIDENCE_TTL_HOURS": ("MODEL_PROBE_EVIDENCE_TTL_HOURS", "model-probe-evidence-ttl-hours", "model_probe_evidence_ttl_hours", "24"),
}


@pytest.mark.parametrize("key", sorted(MODEL_PROBE_VALUES))
def test_configmap_carries_a_placeholder_for_each_model_probe_control(key):
    placeholder = MODEL_PROBE_VALUES[key][0]
    configmap = CONFIGMAP.read_text()
    quote = "'" if key == "BG_MODEL_PROBE_MODEL_ALLOWLIST" else '"'
    assert f"{key}: {quote}__{placeholder}__{quote}" in configmap


def test_no_model_probe_literal_remains_in_the_configmap():
    """A wrong result that must fail: BG_MODEL_PROBE_ENABLED: "false" as a committed literal."""
    active = [line.strip() for line in CONFIGMAP.read_text().splitlines() if line.strip().startswith("BG_MODEL_PROBE_")]
    assert len(active) == len(MODEL_PROBE_VALUES), active
    for line in active:
        key, _, value = line.partition(":")
        assert key in MODEL_PROBE_VALUES, line
        assert value.strip() in (f'"__{MODEL_PROBE_VALUES[key][0]}__"', f"'__{MODEL_PROBE_VALUES[key][0]}__'"), line
    comment = CONFIGMAP.read_text()
    assert "Disabled again after" not in comment
    assert "Re-enable only after" not in comment
    assert "#7605" in comment and "fail-closed" in comment


@pytest.mark.parametrize("renderer", [WORKFLOW, DEPLOY_ALL], ids=["gateway-deploy.yml", "deploy-all.sh"])
@pytest.mark.parametrize("key", sorted(MODEL_PROBE_VALUES))
def test_both_renderers_read_validate_and_render_each_model_probe_control(renderer, key):
    placeholder, parameter, variable, default = MODEL_PROBE_VALUES[key]
    source = renderer.read_text()
    getter = "get_ssm" if renderer is WORKFLOW else "_get_ssm"
    assert f'{placeholder}=$({getter} "/adp/${{ENVIRONMENT}}/gateway/{parameter}" "{default}")' in source, f"{renderer.name} must read {parameter}"
    rendered = f"{placeholder}_SED" if key == "BG_MODEL_PROBE_MODEL_ALLOWLIST" else placeholder
    assert f"s|__{placeholder}__|${{{rendered}}}|g" in source, f"{renderer.name} must render __{placeholder}__"
    infra = MODEL_PROBE_INFRA.read_text()
    assert f'variable "{variable}"' in infra
    assert f'"/adp/${{var.environment}}/gateway/{parameter}"' in infra, f"model-probe.tf must declare {parameter}"


@pytest.mark.parametrize("renderer", [WORKFLOW, DEPLOY_ALL], ids=["gateway-deploy.yml", "deploy-all.sh"])
def test_both_renderers_validate_model_probe_values_identically(renderer):
    """Malformed SSM values must render the fail-closed defaults, never pass through."""
    source = renderer.read_text()
    assert '[ "$MODEL_PROBE_ENABLED" = "true" ] || MODEL_PROBE_ENABLED="false"' in source
    assert '[[ "$MODEL_PROBE_MAX_SLOTS_PER_CYCLE" =~ ^[0-9]+$ ]] || MODEL_PROBE_MAX_SLOTS_PER_CYCLE="0"' in source
    assert '[[ "$MODEL_PROBE_BUDGET_USD_PER_CYCLE" =~ ^[0-9]+(\\.[0-9]{1,6})?$ ]] || MODEL_PROBE_BUDGET_USD_PER_CYCLE="0"' in source
    assert ('[[ "$MODEL_PROBE_MAX_BUDGET_USD_PER_ATTEMPT" =~ ^[0-9]+(\\.[0-9]{1,6})?$ ]] || MODEL_PROBE_MAX_BUDGET_USD_PER_ATTEMPT="0"') in source
    assert "assert isinstance(v, list) and all(isinstance(x, str) for x in v)" in source
    assert '[[ "$MODEL_PROBE_EVIDENCE_TTL_HOURS" =~ ^[0-9]+$ ]] || MODEL_PROBE_EVIDENCE_TTL_HOURS="24"' in source
    # The JSON allowlist goes through the same sed escaping as the other JSON values.
    assert r"MODEL_PROBE_MODEL_ALLOWLIST_SED=${MODEL_PROBE_MODEL_ALLOWLIST_SED//|/\\|}" in source


def test_model_probe_controls_are_retained_across_release_upgrades():
    output = RELEASE_CONFIGURATION.read_text()
    preserved = PRESERVED_INPUTS.read_text()
    for _, _, variable, _ in MODEL_PROBE_VALUES.values():
        assert f"{variable} = var.{variable}" in " ".join(output.split()), variable
        assert f'"{variable}"' in preserved, variable


def test_dev_tfvars_arm_probing_with_the_approved_spend_bound():
    """Delegated chat on dev enforces model policy; probing is required and bounded.

    The 12 claude-agent-sdk catalogue models at 0.50 USD reserved per attempt need more
    than the 5 USD bound #7591 approved for the three GPT-6 models; #7974 raised the dev
    ceiling to 12 USD (reservation cap, not expected spend).
    """
    source = DEV_TFVARS.read_text()
    active = [" ".join(line.split()) for line in source.splitlines() if not line.lstrip().startswith("#")]
    for expected in (
        "model_probe_enabled = true",
        "model_probe_max_slots_per_cycle = 24",
        'model_probe_budget_usd_per_cycle = "12"',
        'model_probe_max_budget_usd_per_attempt = "0.50"',
        "model_probe_model_allowlist = []",
        "model_probe_evidence_ttl_hours = 48",
    ):
        assert expected in active, expected
    webhook = [" ".join(line.split()) for line in DEV_WEBHOOK_TFVARS.read_text().splitlines() if not line.lstrip().startswith("#")]
    assert "persona_model_probe_enabled = true" in webhook


# Model policy posture for chat (#7997): one admin-facing switch, off | advisory |
# enforced, default advisory. Same flow as every other switch: gateway infra
# (chief-of-staff-flags.tf, value from tfvars) -> SSM -> both renderers -> ConfigMap.
def test_chat_model_policy_posture_is_declared_with_advisory_as_the_default():
    infra = INFRA.read_text()
    assert 'variable "chat_model_policy_posture"' in infra
    declaration = infra[infra.index('variable "chat_model_policy_posture"') :]
    assert 'default     = "advisory"' in declaration[: declaration.index("}") + 1]
    assert 'contains(["off", "advisory", "enforced"], var.chat_model_policy_posture)' in infra
    assert 'resource "aws_ssm_parameter" "chat_model_policy_posture"' in infra
    assert 'name  = "/adp/${var.environment}/gateway/chat-model-policy-posture"' in infra
    assert "value = var.chat_model_policy_posture" in infra


def test_chat_model_policy_posture_has_a_configmap_placeholder_and_no_literal():
    configmap = CONFIGMAP.read_text()
    assert 'BG_CHAT_MODEL_POLICY_POSTURE: "__CHAT_MODEL_POLICY_POSTURE__"' in configmap
    active = [line.strip() for line in configmap.splitlines() if line.strip().startswith("BG_CHAT_MODEL_POLICY_POSTURE")]
    assert active == ['BG_CHAT_MODEL_POLICY_POSTURE: "__CHAT_MODEL_POLICY_POSTURE__"']


@pytest.mark.parametrize("renderer", [WORKFLOW, DEPLOY_ALL], ids=["gateway-deploy.yml", "deploy-all.sh"])
def test_both_renderers_read_validate_and_render_the_chat_model_policy_posture(renderer):
    source = renderer.read_text()
    getter = "get_ssm" if renderer is WORKFLOW else "_get_ssm"
    assert f'CHAT_MODEL_POLICY_POSTURE=$({getter} "/adp/${{ENVIRONMENT}}/gateway/chat-model-policy-posture" "advisory")' in source
    assert 'case "$CHAT_MODEL_POLICY_POSTURE" in off|advisory|enforced) ;; *) CHAT_MODEL_POLICY_POSTURE="advisory" ;; esac' in source
    assert "s|__CHAT_MODEL_POLICY_POSTURE__|${CHAT_MODEL_POLICY_POSTURE}|g" in source


def test_chat_model_policy_posture_is_retained_across_release_upgrades():
    assert "chat_model_policy_posture = var.chat_model_policy_posture" in " ".join(RELEASE_CONFIGURATION.read_text().split())
    assert '"chat_model_policy_posture"' in PRESERVED_INPUTS.read_text()


def test_dev_sets_the_chat_posture_to_advisory_in_tfvars():
    """Owner decision 2026-10-17: the model policy is advisory for chat; enforcement stays for agent runs."""
    active = [" ".join(line.split()) for line in DEV_TFVARS.read_text().splitlines() if not line.lstrip().startswith("#")]
    assert 'chat_model_policy_posture = "advisory"' in active


def test_delegated_deploy_reads_the_posture_and_requires_probing_only_when_enforced():
    deploy_all = DEPLOY_ALL.read_text()
    assert 'CHAT_MODEL_POLICY_POSTURE=$(_chat_ssm "/adp/${ENVIRONMENT}/gateway/chat-model-policy-posture")' in deploy_all
    assert 'if [ "$CHAT_MODEL_POLICY_POSTURE" = enforced ]; then\n        fail "Delegated chat enforces model policy' in deploy_all
    assert 'warn "Model invocability probing is not armed; chat model policy posture is ${CHAT_MODEL_POLICY_POSTURE}' in deploy_all
    assert "Probing is fail-closed and REQUIRED whenever the chat model-policy posture" in CONFIGMAP.read_text()
    assert "chat_model_policy_posture" in MODEL_PROBE_INFRA.read_text()


# Delegated chat memory and artifact stores (#8028). chat_data_routes.py answers 503
# chat_memory_unconfigured / chat_artifacts_unconfigured while MEMORY_TABLE or
# ARTIFACTS_TABLE+ARTIFACTS_BUCKET are unset; nothing rendered them and the sandbox
# died on the answer. Tables: agent-factory publishes the names, both renderers read
# them with the deterministic names as defaults. Bucket: the task-api parameter the
# renderers already read into TASK_ARTIFACT_BUCKET_NAME.
CHAT_STORE_TABLES = {
    "MEMORY_TABLE": ("chat-memory-table", "adp-${ENVIRONMENT}-agent-memory", "gateway_chat_memory_table"),
    "ARTIFACTS_TABLE": ("chat-artifacts-table", "adp-${ENVIRONMENT}-chat-artifacts", "gateway_chat_artifacts_table"),
}
CHAT_STORE_ENV = (*CHAT_STORE_TABLES, "ARTIFACTS_BUCKET")


@pytest.mark.parametrize("env_var", CHAT_STORE_ENV)
def test_configmap_carries_a_placeholder_for_each_chat_store(env_var):
    active = [line.strip() for line in CONFIGMAP.read_text().splitlines() if line.strip().startswith(f"{env_var}:")]
    assert active == [f'{env_var}: "__{env_var}__"'], active


@pytest.mark.parametrize("renderer", [WORKFLOW, DEPLOY_ALL], ids=["gateway-deploy.yml", "deploy-all.sh"])
@pytest.mark.parametrize("env_var", sorted(CHAT_STORE_TABLES))
def test_both_renderers_read_each_chat_store_table_from_the_same_parameter_with_the_same_default(renderer, env_var):
    parameter, default, _ = CHAT_STORE_TABLES[env_var]
    source = renderer.read_text()
    getter = "get_ssm" if renderer is WORKFLOW else "_get_ssm"
    assert f'{env_var}=$({getter} "/adp/${{ENVIRONMENT}}/gateway/{parameter}" "{default}")' in source, f"{renderer.name} must read {parameter}"
    assert f'[[ "${env_var}" =~ ^[A-Za-z0-9_.-]{{3,255}}$ ]] && [ "${env_var}" != "None" ] || {env_var}="{default}"' in source
    assert f"s|__{env_var}__|${{{env_var}}}|g" in source, f"{renderer.name} must render __{env_var}__"


@pytest.mark.parametrize("renderer", [WORKFLOW, DEPLOY_ALL], ids=["gateway-deploy.yml", "deploy-all.sh"])
def test_both_renderers_take_the_artifacts_bucket_from_the_task_artifact_parameter(renderer):
    """One bucket, one read: the chat data plane stores artifacts where the task API already does."""
    source = renderer.read_text()
    assert 'TASK_ARTIFACT_BUCKET_NAME=$(get_authority_ssm "/adp/${ENVIRONMENT}/gateway/task-artifact-bucket-name" "")' in source
    assert 'ARTIFACTS_BUCKET="${TASK_ARTIFACT_BUCKET_NAME}"' in source
    assert '[[ "$ARTIFACTS_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || ARTIFACTS_BUCKET=""' in source
    assert "s|__ARTIFACTS_BUCKET__|${ARTIFACTS_BUCKET}|g" in source
    assert source.index('ARTIFACTS_BUCKET="${TASK_ARTIFACT_BUCKET_NAME}"') > source.index("TASK_ARTIFACT_BUCKET_NAME=$(get_authority_ssm")


@pytest.mark.parametrize("env_var", sorted(CHAT_STORE_TABLES))
def test_agent_factory_publishes_the_chat_store_table_parameter_it_owns(env_var):
    """A new environment gets the parameter from the stack that owns the table."""
    parameter, _, resource = CHAT_STORE_TABLES[env_var]
    infra = AGENT_FACTORY_CHAT_INFRA.read_text()
    assert f'resource "aws_ssm_parameter" "{resource}"' in infra
    assert f'"/adp/${{var.environment}}/gateway/{parameter}"' in infra
    table = "agent_memory" if env_var == "MEMORY_TABLE" else "chat_artifacts"
    declaration = infra[infra.index(f'resource "aws_ssm_parameter" "{resource}"') :]
    block = declaration[: declaration.index("\n}") + 2]  # the first "}" is the ${var.environment} interpolation
    assert f"value       = aws_dynamodb_table.{table}.name" in block
    assert 'type        = "String"' in block


def test_the_gateway_role_is_granted_the_stores_it_renders():
    """Rendering a table the role cannot read is a 503 with a different name (AccessDenied)."""
    effective = "\n".join(line for line in GATEWAY_INTAKE_ACCESS.read_text().splitlines() if not line.lstrip().startswith("#"))
    for sid in ("ChatMemoryStore", "ChatArtifactCatalog", "ChatArtifactObjects"):
        assert f'Sid    = "{sid}"' in effective, sid
    assert "aws_dynamodb_table.agent_memory.arn" in effective
    assert "aws_dynamodb_table.chat_artifacts.arn" in effective
    assert "${aws_s3_bucket.chat_artifacts.arn}/o/*/t/*/u/*/s/*/*" in effective
