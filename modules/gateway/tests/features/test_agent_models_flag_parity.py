"""Agent Models follows portable deployment configuration in both renderers."""

import os
import subprocess
from pathlib import Path

import pytest

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


def test_ci_deploy_defaults_to_mapping_configuration_and_preserves_ui_override():
    source = WORKFLOW.read_text()
    assert 'get_ssm "/adp/${ENVIRONMENT}/gateway/persona-model-mapping-enabled" "true"' in source
    assert f'get_ssm "{SSM_PARAMETER}" "$PERSONA_MODEL_MAPPING_ENABLED"' in source
    assert f"s|{PLACEHOLDER}|${{{FLAG_ENV}}}|g" in source
    assert f'[ "${FLAG_ENV}" = "None" ]' in source


def test_self_managed_deploy_reads_and_substitutes_the_same_parameter():
    source = DEPLOY_ALL.read_text()
    assert '_get_ssm "/adp/${ENVIRONMENT}/gateway/persona-model-mapping-enabled" "true"' in source
    assert f'_get_ssm "{SSM_PARAMETER}" "$PERSONA_MODEL_MAPPING_ENABLED"' in source
    assert f"s|{PLACEHOLDER}|${{{FLAG_ENV}}}|g" in source


@pytest.mark.parametrize("renderer", [WORKFLOW, DEPLOY_ALL])
@pytest.mark.parametrize(
    "mapping,ui,expected",
    [("", "", "true true"), ("false", "", "false false"), ("true", "false", "true false"), ("false", "true", "false true")],
)
def test_fresh_environment_and_explicit_overrides_execute_identically(renderer, mapping, ui, expected):
    """Run the actual renderer assignments for an environment with no dev overlay."""
    lines = renderer.read_text().splitlines()
    assignments = []
    for variable in ("PERSONA_MODEL_MAPPING_ENABLED", FLAG_ENV):
        matches = [line.strip() for line in lines if line.strip().startswith(f"{variable}=$(")]
        assert matches
        assignments.append(matches[-1])
    script = """
set -eu
ENVIRONMENT=integration
get_ssm() {
  case "$1" in
    /adp/integration/gateway/persona-model-mapping-enabled) value="$TEST_MAPPING" ;;
    /adp/integration/gateway/feature-agent-models) value="$TEST_UI" ;;
    *) exit 9 ;;
  esac
  printf '%s' "${value:-$2}"
}
_get_ssm() { get_ssm "$@"; }
"""
    result = subprocess.run(
        ["bash", "-c", script + "\n".join(assignments) + '\nprintf "%s %s" "$PERSONA_MODEL_MAPPING_ENABLED" "$FEATURE_AGENT_MODELS_ENABLED"'],
        env={**os.environ, "TEST_MAPPING": mapping, "TEST_UI": ui},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
