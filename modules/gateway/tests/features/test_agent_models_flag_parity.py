"""Agent Models stays fail-closed across both gateway deployment paths (#5422)."""

from pathlib import Path

from src.features.routes import get_features

FLAG_KEY = "agent_models"
FLAG_ENV = "FEATURE_AGENT_MODELS_ENABLED"
PLACEHOLDER = f"__{FLAG_ENV}__"
SSM_PARAMETER = "/adp/${ENVIRONMENT}/gateway/feature-agent-models"

REPO_ROOT = Path(__file__).resolve().parents[4]
FRONTEND_FEATURES = REPO_ROOT / "modules/gateway/frontend/src/services/features.ts"
DEPLOYMENT = REPO_ROOT / "modules/gateway/k8s/deployment.yaml"
WORKFLOW = REPO_ROOT / ".github/workflows/gateway-deploy.yml"
DEPLOY_ALL = REPO_ROOT / "platform/scripts/deploy-all.sh"


async def test_endpoint_is_off_when_the_environment_is_absent(monkeypatch):
    monkeypatch.delenv(FLAG_ENV, raising=False)
    payload = await get_features(_current_user=object())
    assert payload["features"][FLAG_KEY] is False


async def test_only_explicit_true_enables_the_endpoint(monkeypatch):
    monkeypatch.setenv(FLAG_ENV, "invalid")
    assert (await get_features(_current_user=object()))["features"][FLAG_KEY] is False
    monkeypatch.setenv(FLAG_ENV, "true")
    assert (await get_features(_current_user=object()))["features"][FLAG_KEY] is True


def test_frontend_pending_and_error_fallback_is_off():
    source = FRONTEND_FEATURES.read_text()
    interface = source.split("export interface FeatureFlags", 1)[1].split("}", 1)[0]
    defaults = source.split("ALL_FEATURES_ENABLED: FeatureFlags = {", 1)[1].split("};", 1)[0]
    assert f"{FLAG_KEY}:" in interface
    assert f"{FLAG_KEY}: false" in defaults


def test_manifest_uses_a_rendered_value_not_a_cross_environment_literal():
    active = [line.strip() for line in DEPLOYMENT.read_text().splitlines() if line.strip().startswith(("- name:", "value:"))]
    index = active.index(f"- name: {FLAG_ENV}")
    assert active[index + 1] == f'value: "{PLACEHOLDER}"'


def test_ci_deploy_reads_and_substitutes_the_default_off_parameter():
    source = WORKFLOW.read_text()
    assert f'get_ssm "{SSM_PARAMETER}" "false"' in source
    assert f"s|{PLACEHOLDER}|${{{FLAG_ENV}}}|g" in source
    assert f'[ "${FLAG_ENV}" = "None" ]' in source


def test_self_managed_deploy_reads_and_substitutes_the_same_parameter():
    source = DEPLOY_ALL.read_text()
    assert f'_get_ssm "{SSM_PARAMETER}" "false"' in source
    assert f"s|{PLACEHOLDER}|${{{FLAG_ENV}}}|g" in source
