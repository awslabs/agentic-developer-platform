"""Keep native fork/crypto/Unix IPC checks in a supported single-thread process."""

import os
import subprocess
import sys
from pathlib import Path


def test_real_native_mcp_coordinator_lifecycle_and_containment():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/cli/native_credential_handoff_cases.py", "-q", "-n", "0"],
        cwd=Path(__file__).parents[2],
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout
