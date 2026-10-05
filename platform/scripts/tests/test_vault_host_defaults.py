"""Execute both deployment fallback branches without calling AWS."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "deploy-all.sh"


@pytest.mark.parametrize("configured", ["missing", "None", "", "acme.atlassian.net"])
def test_deploy_defaults_never_authorize_other_saas_tenants(configured):
    lines = SCRIPT.read_text().splitlines()
    fragment = "\n".join(line for line in lines if line.strip().startswith(("VAULT_PROXY_HOST_ALLOWLIST=", 'if [ "$VAULT_PROXY_HOST_ALLOWLIST"')))
    assert fragment
    program = '''_get_ssm() { if [ "$CONFIGURED" = missing ]; then printf '%s' "$2"; else printf '%s' "$CONFIGURED"; fi; }
''' + fragment + '\nprintf "%s" "$VAULT_PROXY_HOST_ALLOWLIST"\n'
    result = subprocess.run(["bash", "-c", program], env={**os.environ, "CONFIGURED": configured}, check=True, capture_output=True, text=True)
    hosts = result.stdout.split(",")
    assert "*.atlassian.net" not in hosts
    if configured in ("missing", "None"):
        assert "api.github.com" in hosts
        assert not any(host.endswith("atlassian.net") for host in hosts)
    else:
        assert result.stdout == configured
