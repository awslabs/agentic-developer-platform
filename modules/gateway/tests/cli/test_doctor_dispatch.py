"""`adp capabilities` / `adp doctor` must be dispatchable, installable, servable.

Issue #5621. A new CLI verb has to be registered on six independent surfaces —
the dispatcher, the help text, the update list, the install list, the public
download allowlist and its media type — and missing ANY one produces a command
that is broken only after it reaches a user's machine. The worst of those is the
install/update list: the verb works perfectly in the repo, the dispatcher finds
nothing after a real install, and the failure appears as "CLI helper missing".

These tests are cheap and they fail loudly at the exact surface that was missed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).parents[2] / "cli"
ADP = CLI_DIR / "adp"
INSTALL = CLI_DIR / "install.sh"
HELPER_NAME = "adp-doctor.py"
VERBS = ("capabilities", "doctor")


def cli_files(script: Path) -> list[str]:
    match = re.search(r'^CLI_FILES="([^"]+)"', script.read_text(), re.MULTILINE)
    assert match
    return match.group(1).split()


@pytest.fixture(scope="module")
def allowlist():
    from src.cli_download.routes import ALLOWED_SCRIPTS

    return ALLOWED_SCRIPTS


def test_both_verbs_dispatch_to_the_doctor_helper() -> None:
    """One shared arm, and it must forward the verb the user actually typed.

    The helper parses `capabilities` and `doctor` as subcommands, so the arm has
    to pass `${command}` through. An arm that dropped it would send every
    invocation to the same default verb — `adp capabilities` would silently run
    `doctor`.
    """
    arm = re.search(r'\n\s{8}capabilities\|doctor\)\s*\n\s*exec_python_helper "([^"]+)" "\$\{command\}"', ADP.read_text())
    assert arm, "expected a `capabilities|doctor)` arm forwarding ${command}"
    assert arm.group(1) == HELPER_NAME


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
    """Drift here means `adp update` and a fresh install deliver different CLIs."""
    assert cli_files(ADP) == cli_files(INSTALL)


def test_help_advertises_both_verbs() -> None:
    result = subprocess.run(["bash", str(ADP), "help"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0
    for verb in VERBS:
        assert verb in result.stdout, f"help does not mention {verb}"


def test_help_states_that_doctor_only_reads() -> None:
    """The read-only promise belongs where a worried user looks first.

    Somebody runs `doctor` against a deployment that is already misbehaving. If
    the help does not say the command changes nothing, the safe move is to not
    run it — and a diagnostic nobody dares run has no value.
    """
    result = subprocess.run(["bash", str(ADP), "help"], capture_output=True, text=True, timeout=60)
    assert "changes nothing" in result.stdout


@pytest.mark.parametrize("verb", VERBS)
def test_unknown_flag_is_a_usage_error_not_a_request(verb: str, tmp_path: Path) -> None:
    """Exit 1 for usage, and it must fail before touching the network."""
    result = subprocess.run(
        ["python3", str(CLI_DIR / HELPER_NAME), verb, "--not-a-flag"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert result.returncode == 1


def test_unknown_verb_is_a_usage_error(tmp_path: Path) -> None:
    result = subprocess.run(
        ["python3", str(CLI_DIR / HELPER_NAME), "not-a-command"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert result.returncode == 1
