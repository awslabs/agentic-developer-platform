"""Run the actual private host cases in a fresh single-threaded native process.

xdist workers have a receiver thread. The production host must refuse to fork
from them; exercise its supported entry context without weakening that guard.
"""

import os
import subprocess
import sys
from pathlib import Path


def test_native_private_host_process_crypto_and_failure_cases():
    gateway = Path(__file__).parents[2]
    environment = {**os.environ, "PYTEST_ADDOPTS": ""}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/cli/native_private_credentials_cases.py", "-q", "-n", "0"],
        cwd=gateway,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout
