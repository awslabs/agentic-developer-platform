"""Exercise the trusted native CLI in its supported serial host process."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import write_adp_config


def test_native_cli_real_private_consumer_and_transport_contract():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/cli/native_credential_client_cases.py", "-q", "-n", "0"],
        cwd=Path(__file__).parents[2],
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout


@pytest.mark.parametrize("action", ["host-setup", "delivery-status", "use"])
def test_flat_cli_routes_native_commands_without_generic_tool_credentials(tmp_path, action):
    write_adp_config(tmp_path)
    cli = Path(__file__).parents[2] / "cli" / "adp"
    result = subprocess.run(["bash", str(cli), "credential", action, "--help"], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "adp credential" in result.stdout


def test_flat_cli_refuses_tenant_selector_for_private_credentials(tmp_path):
    write_adp_config(tmp_path)
    cli = Path(__file__).parents[2] / "cli" / "adp"
    result = subprocess.run(
        ["bash", str(cli), "--tenant", "forged", "credential", "use", "--help"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "tenant selectors are unsupported" in result.stderr
