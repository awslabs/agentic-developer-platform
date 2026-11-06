"""Resolve both deployment entrypoints through the ConfigMap into real settings."""

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException
from pydantic import ValidationError

from src.internal.credential_routes import _validate_credential_host_binding
from src.shared.config import Settings

ROOT = Path(__file__).resolve().parents[4]
RENDERERS = [ROOT / ".github/workflows/gateway-deploy.yml", ROOT / "platform/scripts/deploy-all.sh"]
VARIABLE = "VAULT_ENFORCE_CREDENTIAL_HOST_BINDING"
ENV_KEY = f"BG_{VARIABLE}"


def resolve_configmap(renderer, value):
    source = renderer.read_text()
    start = source.index(f"{VARIABLE}=$(")
    # Run the full resolution/validation block, not a copy of the new default.
    block = textwrap.dedent(source[source.rfind("\n", 0, start) + 1 : source.index("# #5653", start)]).strip()
    helper_name = "_get_ssm" if renderer.suffix == ".sh" else "get_ssm"
    helper = next(line.strip() for line in source.splitlines() if line.strip().startswith(f"{helper_name}()"))
    substitution = re.search(rf'"s\|__{VARIABLE}__\|[^"\n]+"', source).group(0)
    script = """
set -eu
ENVIRONMENT=integration
AWS_REGION=us-east-1
aws() {
  [ "$1 $2" = "ssm get-parameter" ] || return 99
  [ "$4" = "/adp/integration/gateway/vault-enforce-credential-host-binding" ] || return 99
  [ "$TEST_SSM_VALUE" != missing ] || return 1
  printf '%s' "$TEST_SSM_VALUE"
}
"""
    if renderer.suffix == ".yml":
        # The workflow reads its parameter snapshot before this extracted block.
        # Exercise missing, empty and invalid values through the real getter.
        script += """
declare -A ADP_SSM_VALUES=()
if [ "$TEST_SSM_VALUE" != missing ]; then
  ADP_SSM_VALUES[plain:/adp/integration/gateway/vault-enforce-credential-host-binding]="$TEST_SSM_VALUE"
fi
"""
    return subprocess.run(
        ["bash", "-c", script + helper + "\n" + block + "\nsed -e " + substitution],
        input=(ROOT / "modules/gateway/k8s/configmap.yaml").read_text(),
        env={**os.environ, "TEST_SSM_VALUE": value},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda path: path.name)
@pytest.mark.parametrize("value", ["missing", "", "None", "true", "false"])
def test_effective_binding_posture(renderer, value, monkeypatch, caplog):
    result = resolve_configmap(renderer, value)
    assert result.returncode == 0, result.stderr
    rendered = yaml.safe_load(result.stdout)["data"][ENV_KEY]
    monkeypatch.setenv(ENV_KEY, rendered)
    settings = Settings(_env_file=None)
    _validate_credential_host_binding("github", "https://api.github.com/user", settings)

    if value == "false":
        assert "WARNING" in result.stderr
        _validate_credential_host_binding("github", "https://slack.com/api/auth.test", settings)
        assert "shadow mode, allowed" in caplog.text
    else:
        with pytest.raises(HTTPException) as exc:
            _validate_credential_host_binding("github", "https://slack.com/api/auth.test", settings)
        assert exc.value.status_code == 403
        assert exc.value.detail["error"] == "credential_host_binding_denied"


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda path: path.name)
@pytest.mark.parametrize("value", ["fasle", "disabled", "true\nfalse"])
def test_invalid_deployment_setting_stops_before_rendering(renderer, value):
    result = resolve_configmap(renderer, value)
    assert result.returncode != 0
    assert "must be true or false" in result.stderr
    assert "apiVersion:" not in result.stdout


def test_invalid_application_setting_is_rejected(monkeypatch):
    monkeypatch.setenv(ENV_KEY, "fasle")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
