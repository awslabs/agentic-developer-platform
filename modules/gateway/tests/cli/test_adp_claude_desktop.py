"""`adp claude-desktop setup` pins the tenant it resolves at setup time (#7426).

The helper it installs runs `adp token`, which refuses to mint a token for a
multi-tenant login until a tenant is selected. Setup therefore resolves the
tenant itself, bakes the pins into the helper beside the deployment pins, and
reports a tenant problem as a tenant problem rather than as "run adp login".
"""

import json
import shlex

import pytest

from .conftest import write_adp_config as _write_config
from .conftest import write_adp_session as _write_session

PINS = {
    "ADP_TENANT_ID": "aws-e",
    "ADP_TENANT_SUB": "sub-123",
    "ADP_TENANT_SOURCE": "environment",
    "ADP_TENANT_MODE": "scoped",
    "ADP_TENANT_MEMBERSHIP": "m-1",
}


@pytest.fixture
def tenant_aware_bin(adp_bin, cli_dir):
    """The installed prefix with the tenant helper the real installer ships."""
    for name in ("adp-tenant.py",):
        target = adp_bin / name
        target.write_bytes((cli_dir / name).read_bytes())
        target.chmod(0o755)
    return adp_bin


def _helper(adp_home):
    return adp_home / ".adp" / "bin" / "adp-claude-desktop-token"


def _lease_exchange(mock_gateway, adp_home, membership="m-1"):
    """`core token` exchanges the pinned tenant for a context lease on every call."""
    _write_config(adp_home, gateway_url=mock_gateway.url + "/api")
    mock_gateway.set_response(
        "/api/workspaces/context",
        200,
        {"tenant_id": "aws-e", "identity": "sub-123", "context_token": "ctx.tok.en", "membership_id": membership},
    )


def test_setup_pins_the_resolved_tenant_into_the_helper(run_adp, adp_home, tenant_aware_bin, mock_gateway):
    _write_session(adp_home)
    _lease_exchange(mock_gateway, adp_home)
    result = run_adp(["claude-desktop", "setup", "--json"], extra_env=PINS)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["tenant"] == "aws-e"
    assert report["settings_written"] is False
    helper = _helper(adp_home).read_text()
    assert "ADP_TENANT_ID=aws-e" in helper
    assert "ADP_TENANT_SUB=sub-123" in helper
    assert "token" in shlex.split(helper.splitlines()[-1]) or "adp" in helper


def test_setup_names_the_tenant_in_its_summary(run_adp, adp_home, tenant_aware_bin, mock_gateway):
    _write_session(adp_home)
    _lease_exchange(mock_gateway, adp_home)
    result = run_adp(["claude-desktop", "setup"], extra_env=PINS)
    assert result.returncode == 0, result.stderr
    assert "Tenant: aws-e" in result.stdout
    assert "no Desktop settings were changed" in result.stdout


def test_multi_tenant_login_without_a_selection_is_told_to_select_a_tenant(run_adp, adp_home, tenant_aware_bin, mock_gateway):
    _write_session(adp_home)
    _write_config(adp_home, gateway_url=mock_gateway.url + "/api")
    mock_gateway.set_response(
        "/api/workspaces",
        200,
        {"items": [{"org_id": "aws-e", "name": "aws-e"}, {"org_id": "personal", "name": "personal"}]},
    )
    result = run_adp(["claude-desktop", "setup", "--check"])
    assert result.returncode == 4, result.stdout + result.stderr
    assert "adp tenant use" in result.stderr
    assert "Run adp login" not in result.stderr
    assert not _helper(adp_home).exists()


def test_single_tenant_login_needs_no_selection(run_adp, adp_home, tenant_aware_bin, mock_gateway):
    _write_session(adp_home)
    _write_config(adp_home, gateway_url=mock_gateway.url + "/api")
    mock_gateway.set_response("/api/workspaces", 200, {"items": [{"org_id": "aws-e", "name": "aws-e"}]})
    mock_gateway.set_response(
        "/api/workspaces/context",
        200,
        {"tenant_id": "aws-e", "identity": "sub-123", "context_token": "ctx.tok.en", "membership_id": "m-9"},
    )
    result = run_adp(["claude-desktop", "setup", "--json"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["tenant"] == "aws-e"
    assert "ADP_TENANT_ID=aws-e" in _helper(adp_home).read_text()
