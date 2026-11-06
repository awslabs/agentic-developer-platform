from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".github/workflows/gateway-deploy.yml"
DEPLOY_ALL = ROOT / "platform/scripts/deploy-all.sh"
MANIFEST = ROOT / "modules/gateway/k8s/deployment.yaml"


@pytest.mark.parametrize("renderer", [WORKFLOW, DEPLOY_ALL])
@pytest.mark.parametrize(
    "name,parameter",
    [
        ("BG_PRODUCT_PROFILE_ENABLED", "product-profile-enabled"),
        ("BG_PRODUCT_ONBOARDING_ENABLED", "product-onboarding-enabled"),
    ],
)
def test_product_flags_are_fail_closed_and_rendered(renderer, name, parameter):
    source = renderer.read_text()
    getter = "get_ssm" if renderer == WORKFLOW else "_get_ssm"
    assert f'{name}=$({getter} "/adp/${{ENVIRONMENT}}/gateway/{parameter}" "false")' in source
    assert f'[[ "${name}" == "true" ]] || {name}="false"' in source
    assert f"s|__{name}__|${{{name}}}|g" in source
    assert f'value: "__{name}__"' in MANIFEST.read_text()


def test_product_preflight_preserves_required_live_acceptance_and_legacy_smoke():
    import yaml

    deploy = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    selected = "${{ inputs.product_cases != '' && inputs.product_cases != 'none' }}"
    assert deploy["smoke-test"]["with"]["product_profile"] == selected
    assert deploy["smoke-test"]["with"]["require_token"] == selected
    assert "smoke-test" in deploy["product-acceptance-guard"]["needs"]
    assert deploy["product-cli-evidence"]["needs"] == "product-acceptance-guard"
    assert deploy["product-acceptance"]["needs"] == ["product-acceptance-guard", "product-cli-evidence"]
    smoke = yaml.safe_load((ROOT / ".github/workflows/gateway-smoke.yml").read_text())
    events = smoke.get("on", smoke.get(True))
    assert events["workflow_call"]["inputs"]["product_profile"]["default"] is False
    steps = {step["name"]: step for step in smoke["jobs"]["smoke"]["steps"] if "name" in step}
    assert steps["Authenticated Product preflight"]["if"] == "inputs.product_profile"
    assert not steps["Authenticated Product preflight"].get("continue-on-error", False)
    assert steps["Read refresh token from Secrets Manager"]["if"] == "${{ !inputs.product_profile }}"
    assert "assert_connections_multi_tenant(connections_body, status_code_conns)" in steps["Assert smoke results"]["run"]
