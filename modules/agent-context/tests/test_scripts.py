"""
Bash-script compatibility tests.

Tests 20-22 from issue #21:
20. bash -n syntax-check every shell script in scripts/
21. shellcheck clean on those scripts
22. validate.sh is still runnable (live only) or retired
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from .config import MODULE_ROOT, SCRIPTS_DIR

# Collect all .sh files in scripts/
SHELL_SCRIPTS = sorted(SCRIPTS_DIR.glob("*.sh")) if SCRIPTS_DIR.is_dir() else []
SCRIPT_IDS = [s.name for s in SHELL_SCRIPTS]


# ---------------------------------------------------------------------------
# Test 20: bash -n syntax check
# ---------------------------------------------------------------------------


class TestBashSyntax:
    """Verify every shell script passes bash -n (syntax check)."""

    @pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=SCRIPT_IDS)
    def test_bash_syntax_check(self, script: Path):
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"Syntax error in {script.name}:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Test 21: shellcheck
# ---------------------------------------------------------------------------


class TestShellcheck:
    """Verify shell scripts pass shellcheck."""

    @staticmethod
    def _shellcheck_available() -> bool:
        try:
            result = subprocess.run(
                ["shellcheck", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
        except FileNotFoundError:
            return False

    @pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=SCRIPT_IDS)
    def test_shellcheck(self, script: Path):
        if not self._shellcheck_available():
            pytest.skip("shellcheck not installed (pip install shellcheck-py)")

        result = subprocess.run(
            [
                "shellcheck",
                "--severity=error",  # Only fail on errors, not warnings/info
                "--shell=bash",
                str(script),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"shellcheck errors in {script.name}:\n{result.stdout}"
        )


# ---------------------------------------------------------------------------
# Test 22: validate.sh
# ---------------------------------------------------------------------------


class TestValidateScript:
    """validate.sh is now retired in favor of pytest.

    Its checks are fully covered by test_platform_health.py.
    This test verifies the script still has valid syntax (backward compat).
    """

    def test_validate_sh_has_valid_syntax(self):
        validate_sh = SCRIPTS_DIR / "validate.sh"
        if not validate_sh.exists():
            pytest.skip("validate.sh not found")

        result = subprocess.run(
            ["bash", "-n", str(validate_sh)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"validate.sh has syntax errors:\n{result.stderr}"
        )

    @pytest.mark.live_only
    @pytest.mark.kubectl
    def test_validate_sh_runs_successfully(self):
        """In live mode, validate.sh should exit 0."""
        validate_sh = SCRIPTS_DIR / "validate.sh"
        if not validate_sh.exists():
            pytest.skip("validate.sh not found")

        result = subprocess.run(
            ["bash", str(validate_sh)],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(MODULE_ROOT),
        )
        assert result.returncode == 0, (
            f"validate.sh failed:\n{result.stdout[-500:]}\n{result.stderr[-500:]}"
        )
