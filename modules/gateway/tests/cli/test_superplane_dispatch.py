"""`adp superplane <verb>` must actually reach the extension (Issue #5039).

`adp`'s main() is a closed hardcoded `case`, and the extension is resolved as an
INSTALLED SIBLING and served by the gateway's download route. So "the verb works"
has three independent failure points, and each gets a test here:

1. the `case` routes `superplane` to the helper at all;
2. the helper is in `ALLOWED_SCRIPTS`, or the gateway never serves it and every
   user's install lands a CLI whose new verb is missing;
3. the helper is in both `CLI_FILES` lists, or install/update never place it.

Point 2 is the one worth stating plainly: a verb present in the `case` but absent
from the allowlist is not a partial feature, it is a CLI that looks broken for
everyone — the download 404s, so `adp superplane` reports a missing helper.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).parents[2] / "cli"
ADP = CLI_DIR / "adp"
INSTALL = CLI_DIR / "install.sh"
HELPER_NAME = "adp-superplane.py"


def case_verbs() -> set[str]:
    """Every verb main()'s `case` claims, as `adp` itself defines them."""
    body = ADP.read_text().split("main() {", 1)[1]
    verbs: set[str] = set()
    for line in body.splitlines():
        match = re.match(r"\s{8}([a-z|\-]+)\)", line)
        if match:
            verbs.update(match.group(1).split("|"))
    return verbs


def cli_files(script: Path) -> list[str]:
    match = re.search(r'^CLI_FILES="([^"]+)"', script.read_text(), re.MULTILINE)
    assert match, f"no CLI_FILES in {script.name}"
    return match.group(1).split()


@pytest.fixture(scope="module")
def allowlist() -> dict:
    from src.cli_download.routes import ALLOWED_SCRIPTS

    return ALLOWED_SCRIPTS


def test_superplane_is_a_verb_in_the_dispatch_case() -> None:
    assert "superplane" in case_verbs()


def test_the_verb_dispatches_to_the_superplane_helper() -> None:
    """The `case` arm must exec THIS helper, not some other area's."""
    arm = re.search(r"\n\s+superplane\)\s*exec_python_helper \"([^\"]+)\"", ADP.read_text())
    assert arm, "superplane) does not delegate via exec_python_helper"
    assert arm.group(1) == HELPER_NAME


def test_the_helper_exists_and_is_python() -> None:
    helper = CLI_DIR / HELPER_NAME
    assert helper.is_file()
    assert helper.read_text().startswith("#!/usr/bin/env python3")


def test_the_helper_is_served_by_the_download_route(allowlist) -> None:
    """Absent here, the gateway 404s it and no install can ever place it."""
    assert HELPER_NAME in allowlist
    assert allowlist[HELPER_NAME] == (CLI_DIR / HELPER_NAME).resolve()


def test_the_helper_has_a_media_type(allowlist) -> None:
    from src.cli_download.routes import PYTHON_SCRIPT_MEDIA_TYPE, SCRIPT_MEDIA_TYPES

    assert SCRIPT_MEDIA_TYPES.get(HELPER_NAME) == PYTHON_SCRIPT_MEDIA_TYPE


@pytest.mark.parametrize("script", [ADP, INSTALL], ids=["adp", "install.sh"])
def test_install_and_update_carry_the_helper(script: Path) -> None:
    """Both lists, or `adp update` silently drops the file it just started needing."""
    assert HELPER_NAME in cli_files(script)


def test_both_cli_files_lists_agree() -> None:
    """install.sh stages what `adp update --rollback` restores; a drift orphans a file."""
    assert cli_files(ADP) == cli_files(INSTALL)


def test_every_python_helper_the_case_dispatches_is_in_the_allowlist(allowlist) -> None:
    """The general invariant behind this story's headline bug class.

    Any verb wired to a Python helper must be serveable. Kept general on purpose:
    it fails for the NEXT area added to the `case` without an allowlist entry,
    not just for superplane.
    """
    dispatched = set(re.findall(r"exec_python_helper \"([^\"]+)\"", ADP.read_text()))
    assert dispatched, "no python helpers dispatched — the regex needs updating"
    assert dispatched <= set(allowlist), f"dispatched but not serveable: {sorted(dispatched - set(allowlist))}"


def test_help_mentions_the_verb() -> None:
    """A verb users cannot discover is a verb they do not have."""
    helped = subprocess.run(["bash", str(ADP), "help"], capture_output=True, text=True, timeout=60)
    assert "superplane" in helped.stdout


def test_unknown_superplane_subcommand_is_a_usage_error(tmp_path: Path) -> None:
    """Dispatch reaches the extension's OWN parser, so its errors surface as usage errors."""
    result = subprocess.run(
        ["python3", str(CLI_DIR / HELPER_NAME), "not-a-verb"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert result.returncode == 1
