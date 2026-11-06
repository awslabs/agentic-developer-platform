"""Keep provider-free first-install and qualification regressions in module CI."""

import subprocess
import sys
from pathlib import Path


def test_finops_driver_offline_contracts():
    module = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", str(module / "tests/live/finops")],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
