"""Dev Box gateway configuration flows from the module's Terraform through both renderers (#7998).

The Dev Box module is optional: its SSM parameters (/adp/<env>/dev-box/*) exist only
where modules/user-services/dev-box/scripts/deploy-dev-box.sh ran. Both renderers
must read the same parameters into the same ConfigMap keys, apply the same
fail-closed defaults when they are absent ("" deployment id => admission disabled,
"0" bounds => capacity refused, "" SSH grant role => ssh-access 503), and never
carry a committed literal. #8049 adds the SSH grant role ARN to the same contract:
the foundation publishes it, the stage passes the gateway's IRSA role so the grant
exists, and the gateway role gets its sts:AssumeRole inside the module.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = REPO_ROOT / ".github/workflows/gateway-deploy.yml"
DEPLOY_ALL = REPO_ROOT / "platform/scripts/deploy-all.sh"
CONFIGMAP = REPO_ROOT / "modules/gateway/k8s/configmap.yaml"
INFRA = REPO_ROOT / "modules/user-services/dev-box/infra/self_managed.tf"
STAGE = REPO_ROOT / "modules/user-services/dev-box/scripts/deploy-dev-box.sh"
SSH_GRANT = REPO_ROOT / "modules/user-services/dev-box/infra/ssh-grant.tf"
MANAGED_WORKFLOW = REPO_ROOT / ".github/workflows/dev-box-deploy.yml"
SSH_ACCESS = REPO_ROOT / "modules/gateway/src/cloud_desktop/ssh_access.py"
ROLE_ARN_PATTERN = "^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$"
ROUTES = REPO_ROOT / "modules/gateway/src/cloud_desktop/routes.py"
CAPACITY = REPO_ROOT / "modules/gateway/src/cloud_desktop/capacity.py"

# ConfigMap key -> (SSM parameter under /adp/<env>/dev-box, renderer default)
DEV_BOX_VALUES = {
    "BG_DEV_BOX_DEPLOYMENT_ID": ("deployment-id", ""),
    "BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES": ("max-deployment-live-boxes", "0"),
    "BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_BOXES": ("max-deployment-running-boxes", "0"),
    "BG_DEV_BOX_MAX_DEPLOYMENT_STORAGE_GB": ("max-deployment-storage-gb", "0"),
    "BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_HOURS": ("max-deployment-running-hours", "0"),
    "BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD": ("max-deployment-budget-usd", "0"),
    "BG_DEV_BOX_SSH_GRANT_ROLE_ARN": ("ssh-grant-role-arn", ""),
    # #8055: how the Crew profile reaches a guest; absent => image (today's behaviour).
    "BG_DEV_BOX_CREW_BOOTSTRAP": ("crew-bootstrap", "image"),
}


@pytest.mark.parametrize("key", sorted(DEV_BOX_VALUES))
def test_configmap_carries_a_placeholder_for_each_dev_box_key(key):
    assert f'{key}: "__{key}__"' in CONFIGMAP.read_text()


def test_no_dev_box_literal_remains_in_the_configmap():
    active = [line.strip() for line in CONFIGMAP.read_text().splitlines() if line.strip().startswith("BG_DEV_BOX_")]
    assert len(active) == len(DEV_BOX_VALUES), active
    for line in active:
        key, _, value = line.partition(":")
        assert value.strip() == f'"__{key}__"', line


@pytest.mark.parametrize("renderer", [WORKFLOW, DEPLOY_ALL], ids=["gateway-deploy.yml", "deploy-all.sh"])
@pytest.mark.parametrize("key", sorted(DEV_BOX_VALUES))
def test_both_renderers_read_the_same_parameter_with_the_fail_closed_default(renderer, key):
    parameter, default = DEV_BOX_VALUES[key]
    source = renderer.read_text()
    getter = "get_ssm" if renderer is WORKFLOW else "_get_ssm"
    assert f'{key}=$({getter} "/adp/${{ENVIRONMENT}}/dev-box/{parameter}" "{default}")' in source, f"{renderer.name} must read {parameter}"
    assert f"s|__{key}__|${{{key}}}|g" in source, f"{renderer.name} must render __{key}__"


@pytest.mark.parametrize("renderer", [WORKFLOW, DEPLOY_ALL], ids=["gateway-deploy.yml", "deploy-all.sh"])
def test_both_renderers_validate_identically_and_treat_absence_as_disabled(renderer):
    source = renderer.read_text()
    assert '[ "$BG_DEV_BOX_DEPLOYMENT_ID" != "None" ] && [ "$BG_DEV_BOX_DEPLOYMENT_ID" != " " ] || BG_DEV_BOX_DEPLOYMENT_ID=""' in source
    for key in (
        "BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES",
        "BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_BOXES",
        "BG_DEV_BOX_MAX_DEPLOYMENT_STORAGE_GB",
        "BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_HOURS",
    ):
        assert f'[[ "${key}" =~ ^[0-9]+$ ]] || {key}="0"' in source, key
    assert '[[ "$BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD" =~ ^[0-9]+(\\.[0-9]{1,6})?$ ]] || BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD="0"' in source
    # SSH grant role (#8049): " "/None sentinel => "", anything but an IAM role ARN => "".
    assert (
        '[ "$BG_DEV_BOX_SSH_GRANT_ROLE_ARN" != "None" ] && [ "$BG_DEV_BOX_SSH_GRANT_ROLE_ARN" != " " ] || BG_DEV_BOX_SSH_GRANT_ROLE_ARN=""' in source
    )
    assert f'[[ "$BG_DEV_BOX_SSH_GRANT_ROLE_ARN" =~ {ROLE_ARN_PATTERN} ]] || BG_DEV_BOX_SSH_GRANT_ROLE_ARN=""' in source
    assert '[[ "$BG_DEV_BOX_CREW_BOOTSTRAP" =~ ^(image|user-data)$ ]] || BG_DEV_BOX_CREW_BOOTSTRAP="image"' in source


@pytest.mark.parametrize("key", sorted(DEV_BOX_VALUES))
def test_the_module_terraform_declares_each_parameter(key):
    parameter = DEV_BOX_VALUES[key][0]
    infra = INFRA.read_text()
    assert f'"{parameter}"' in infra, f"self_managed.tf must publish {parameter}"
    assert 'name  = "${local.ssm_prefix}/${each.key}"' in infra and 'ssm_prefix                   = "/adp/${var.environment}/dev-box"' in infra


def test_the_stage_patches_the_live_configmap_with_the_same_keys():
    stage = STAGE.read_text()
    for key, (parameter, default) in DEV_BOX_VALUES.items():
        assert f'"{key}": ("{parameter}", "{default}")' in stage, key
    assert "kubectl patch configmap bedrockgateway-config" in stage
    assert "kubectl rollout restart deploy/bedrockgateway" in stage


def test_the_stage_treats_the_terraform_sentinel_as_the_default():
    """SSM holds " " where Terraform has no value; the patch must not write that literal."""
    assert 'desired[key] = default if value in ("", " ", "None") else value' in STAGE.read_text()


def test_the_stage_passes_the_live_gateway_role_so_the_grant_exists():
    stage = STAGE.read_text()
    assert "kubectl -n \"$GATEWAY_NAMESPACE\" get sa gateway-service -o jsonpath='{.metadata.annotations.eks\\.amazonaws\\.com/role-arn}'" in stage
    assert f'[[ "$GATEWAY_ROLE_ARN" =~ {ROLE_ARN_PATTERN} ]]' in stage
    assert 'inputs_missing "cannot resolve the gateway IRSA role' in stage
    assert 'FOUNDATION_ARGS+=(-var "gateway_principal_arn=$GATEWAY_ROLE_ARN")' in stage
    assert "TF_VAR_gateway_principal_arn: ${{ vars.ADP_DEV_BOX_GATEWAY_PRINCIPAL_ARN }}" in MANAGED_WORKFLOW.read_text()


def test_the_module_publishes_the_grant_and_lets_only_the_gateway_assume_it():
    infra = INFRA.read_text()
    assert '"ssh-grant-role-arn"           = var.gateway_principal_arn == "" ? " " : aws_iam_role.ssh_grant[0].arn' in infra
    grant = SSH_GRANT.read_text()
    assert 'resource "aws_iam_role_policy" "gateway_ssh_grant"' in grant
    assert 'count = var.gateway_principal_arn == "" ? 0 : 1' in grant
    assert 'role  = element(split("/", var.gateway_principal_arn), length(split("/", var.gateway_principal_arn)) - 1)' in grant
    assert 'Action   = ["sts:AssumeRole"]' in grant
    assert "Resource = aws_iam_role.ssh_grant[0].arn" in grant
    assert 'role = os.getenv("BG_DEV_BOX_SSH_GRANT_ROLE_ARN", "")' in SSH_ACCESS.read_text()


def test_gateway_fails_closed_without_the_module():
    """Routes deny admission with an empty deployment id; capacity refuses "0" bounds."""
    routes = ROUTES.read_text()
    assert 'value = os.getenv("BG_DEV_BOX_DEPLOYMENT_ID", "")' in routes
    assert 'raise HTTPException(403, "Dev Box admission is disabled")' in routes
    capacity = CAPACITY.read_text()
    assert "if not value.isdecimal() or int(value) <= 0:" in capacity
    assert 'raise HTTPException(403, "Dev Box deployment capacity is not configured")' in capacity
    assert 'raise HTTPException(503, "Dev Box SSH access is not configured")' in SSH_ACCESS.read_text()


def test_gateway_reports_the_crew_bootstrap_mode_and_defaults_to_image(monkeypatch):
    """GET /cloud-desktops/{id} exposes crew_bootstrap; an absent or foreign value reads as image."""
    routes = ROUTES.read_text()
    assert 'os.getenv("BG_DEV_BOX_CREW_BOOTSTRAP", "image")' in routes
    assert '"crew_bootstrap": crew_bootstrap_mode(),' in routes
    from src.cloud_desktop.routes import crew_bootstrap_mode

    monkeypatch.delenv("BG_DEV_BOX_CREW_BOOTSTRAP", raising=False)
    assert crew_bootstrap_mode() == "image"
    monkeypatch.setenv("BG_DEV_BOX_CREW_BOOTSTRAP", "user-data")
    assert crew_bootstrap_mode() == "user-data"
    monkeypatch.setenv("BG_DEV_BOX_CREW_BOOTSTRAP", "__BG_DEV_BOX_CREW_BOOTSTRAP__")
    assert crew_bootstrap_mode() == "image"
