"""Installer commands must start on clean runners before package installation."""

import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.mark.parametrize("script", ["deploy-infra.py", "worker-lifecycle.py"])
def test_installer_help_without_site_packages(script):
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPTS / script), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_workflow_selection_import_without_site_packages():
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", """
import importlib.util
import sys
spec = importlib.util.spec_from_file_location('l5_install', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.selection('foundation,security') == ['foundation', 'security']
assert not {'httpx', 'l5_services.product_client'}.intersection(sys.modules)
""", str(SCRIPTS / "deploy-infra.py")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_public_product_client_export_remains_compatible():
    from l5_services import ProductClient
    from l5_services.product_client import ProductClient as Implementation

    assert ProductClient is Implementation
