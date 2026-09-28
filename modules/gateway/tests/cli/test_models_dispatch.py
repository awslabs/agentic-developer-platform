"""The `adp models` helper must be dispatchable, installable and downloadable."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).parents[2] / "cli"
ADP = CLI_DIR / "adp"
INSTALL = CLI_DIR / "install.sh"
HELPER_NAME = "adp-models.py"


def cli_files(script: Path) -> list[str]:
    match = re.search(r'^CLI_FILES="([^"]+)"', script.read_text(), re.MULTILINE)
    assert match
    return match.group(1).split()


@pytest.fixture(scope="module")
def allowlist():
    from src.cli_download.routes import ALLOWED_SCRIPTS

    return ALLOWED_SCRIPTS


def test_models_dispatches_to_the_models_helper() -> None:
    arm = re.search(r'\n\s{8}models\)\s*exec_python_helper "([^"]+)"', ADP.read_text())
    assert arm and arm.group(1) == HELPER_NAME


def test_helper_exists_and_is_python() -> None:
    assert (CLI_DIR / HELPER_NAME).read_text().startswith("#!/usr/bin/env python3")


def test_helper_is_downloadable_with_python_media_type(allowlist) -> None:
    from src.cli_download.routes import PYTHON_SCRIPT_MEDIA_TYPE, SCRIPT_MEDIA_TYPES

    assert allowlist[HELPER_NAME] == (CLI_DIR / HELPER_NAME).resolve()
    assert SCRIPT_MEDIA_TYPES[HELPER_NAME] == PYTHON_SCRIPT_MEDIA_TYPE


@pytest.mark.parametrize("script", [ADP, INSTALL], ids=["update", "install"])
def test_install_and_update_carry_the_helper(script: Path) -> None:
    assert HELPER_NAME in cli_files(script)


def test_install_and_update_lists_stay_equal() -> None:
    assert cli_files(ADP) == cli_files(INSTALL)


def test_help_advertises_models() -> None:
    result = subprocess.run(["bash", str(ADP), "help"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0
    assert "models catalog" in result.stdout


def test_unknown_models_subcommand_is_usage_error(tmp_path: Path) -> None:
    result = subprocess.run(
        ["python3", str(CLI_DIR / HELPER_NAME), "not-a-command"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert result.returncode == 1
