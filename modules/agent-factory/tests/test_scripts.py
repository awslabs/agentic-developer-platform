"""
Bash-script compatibility tests.

Tests 28-29 from the issue:
 28. `bash -n` on every shell script in scripts/ and runner-infra/scripts/.
 29. `shellcheck --severity=error` clean on the same set.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from .config import MODULE_ROOT

# Discover all .sh files in the relevant directories
SCRIPT_DIRS = [
    MODULE_ROOT / "scripts",
    MODULE_ROOT / "runner-infra" / "scripts",
]


def _discover_scripts() -> list[Path]:
    """Find all .sh files in the target directories."""
    scripts = []
    for d in SCRIPT_DIRS:
        if d.exists():
            scripts.extend(sorted(d.glob("*.sh")))
    return scripts


ALL_SCRIPTS = _discover_scripts()
SCRIPT_IDS = [str(s.relative_to(MODULE_ROOT)) for s in ALL_SCRIPTS]


class TestBashSyntax:
    """Test 28: `bash -n` on every shell script."""

    @pytest.mark.parametrize("script", ALL_SCRIPTS, ids=SCRIPT_IDS)
    def test_bash_syntax_valid(self, script: Path):
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"bash -n failed for {script.name}:\n{result.stderr}"
        )


class TestShellcheck:
    """Test 29: shellcheck --severity=error clean on all scripts."""

    @pytest.fixture(autouse=True)
    def _check_shellcheck_available(self):
        """Skip if shellcheck is not installed."""
        import shutil

        if not shutil.which("shellcheck"):
            pytest.skip(
                "shellcheck not installed. Install via: pip install shellcheck-py "
                "or apt-get install shellcheck"
            )

    @pytest.mark.parametrize("script", ALL_SCRIPTS, ids=SCRIPT_IDS)
    def test_shellcheck_no_errors(self, script: Path):
        result = subprocess.run(
            ["shellcheck", "--severity=error", "--format=gcc", str(script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"shellcheck errors in {script.name}:\n{result.stdout}"
        )
