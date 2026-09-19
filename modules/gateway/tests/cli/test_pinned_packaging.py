"""Install and update must be version-explicit (Issue #5039).

`cmd_update` originally re-downloaded install.sh with no version, no tag and no
checksum, and `--rollback` moved back exactly one anonymous generation. So
"update" meant "install whatever the gateway serves right now" and "rollback"
meant "whatever was here before" — neither is a version anyone chose.

What is pinned here:

* a pinned install verifies the version that would land BEFORE committing any
  file, so a mismatch leaves the working CLI untouched rather than installing
  something unasked-for and reporting it afterwards;
* an unpinned target (a range, a tag, `latest`) is REJECTED, not resolved —
  resolving it would reintroduce the behaviour the pin exists to prevent;
* a pinned rollback is checked against what the `.prev` copy actually declares,
  not against a filename or a manifest that could disagree with the code;
* bare `adp update` and bare `--rollback` keep working (R16 acc. 5).

These run the real scripts against a real temp prefix, using the local checkout
as the file source, so no network is involved.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).parents[2] / "cli"
ADP = CLI_DIR / "adp"
INSTALL = CLI_DIR / "install.sh"

CURRENT_VERSION = re.search(r'^readonly ADP_VERSION="([^"]+)"', ADP.read_text(), re.MULTILINE).group(1)
MANIFEST = ".adp-manifest.json"


def run(args, home: Path, cwd: Path = CLI_DIR):
    return subprocess.run(
        ["bash", *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(cwd),
        env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )


@pytest.fixture
def installed(tmp_path):
    """A real install into a temp prefix, sourced from the checkout."""
    home = tmp_path / "home"
    home.mkdir()
    prefix = tmp_path / "bin"
    result = run([str(INSTALL), "--prefix", str(prefix), "--gateway-url", "https://gw.test", "--no-path-edit"], home)
    assert result.returncode == 0, result.stderr
    return home, prefix


# --- install pins a version --------------------------------------------------


def test_install_records_the_version_it_landed(installed) -> None:
    _, prefix = installed
    manifest = json.loads((prefix / MANIFEST).read_text())

    assert manifest["version"] == CURRENT_VERSION


def test_a_matching_pin_installs(installed) -> None:
    home, prefix = installed
    result = run(
        [str(INSTALL), "--prefix", str(prefix), "--gateway-url", "https://gw.test", "--no-path-edit", "--version-pin", CURRENT_VERSION],
        home,
    )

    assert result.returncode == 0, result.stderr


def test_a_mismatched_pin_installs_nothing(installed) -> None:
    """The headline property: a wrong pin must not change the working CLI."""
    home, prefix = installed
    before = (prefix / "adp").read_bytes()

    result = run(
        [str(INSTALL), "--prefix", str(prefix), "--gateway-url", "https://gw.test", "--no-path-edit", "--version-pin", "99.99.99"],
        home,
    )

    assert result.returncode == 1
    assert "99.99.99" in result.stderr
    assert (prefix / "adp").read_bytes() == before


def test_a_mismatched_pin_leaves_no_staged_temp_files(installed) -> None:
    """A failed pin must not litter the prefix with half-downloaded files."""
    home, prefix = installed
    run(
        [str(INSTALL), "--prefix", str(prefix), "--gateway-url", "https://gw.test", "--no-path-edit", "--version-pin", "99.99.99"],
        home,
    )

    assert [path.name for path in prefix.iterdir() if ".tmp." in path.name] == []


def test_the_pin_flag_requires_a_value(installed) -> None:
    home, prefix = installed
    result = run([str(INSTALL), "--prefix", str(prefix), "--no-path-edit", "--version-pin"], home)

    assert result.returncode == 1


# --- update / rollback are version-explicit ----------------------------------


@pytest.mark.parametrize("target", ["latest", "^1.0", "1.0", "v1.0.0", "1.0.x"])
def test_update_rejects_an_unpinned_target(installed, target) -> None:
    """A range or tag pins nothing; only an exact version is accepted."""
    home, prefix = installed
    result = run([str(prefix / "adp"), "update", "--to", target], home)

    assert result.returncode == 1
    assert "exact version" in result.stderr


def test_an_empty_target_is_a_missing_value_not_a_fuzzy_one(installed) -> None:
    """Both are refused, but `--to ''` is a typo — say so instead of 'not exact'."""
    home, prefix = installed
    result = run([str(prefix / "adp"), "update", "--to", ""], home)

    assert result.returncode == 1
    assert "requires a version" in result.stderr


@pytest.mark.parametrize("target", ["latest", "^1.0"])
def test_rollback_rejects_an_unpinned_target(installed, target) -> None:
    home, prefix = installed
    result = run([str(prefix / "adp"), "update", "--rollback", "--to", target], home)

    assert result.returncode == 1
    assert "exact version" in result.stderr


def test_rollback_to_a_version_that_is_not_there_fails_and_changes_nothing(installed) -> None:
    home, prefix = installed
    previous = prefix / "adp.prev"
    previous.write_text((prefix / "adp").read_text().replace(f'ADP_VERSION="{CURRENT_VERSION}"', 'ADP_VERSION="0.9.0"'))
    current = (prefix / "adp").read_bytes()

    result = run([str(prefix / "adp"), "update", "--rollback", "--to", "5.5.5"], home)

    assert result.returncode == 1
    assert "0.9.0" in result.stderr, "the error should name what IS available"
    assert (prefix / "adp").read_bytes() == current
    assert previous.exists(), "a refused rollback must not consume the .prev copy"


def test_rollback_to_the_available_version_succeeds(installed) -> None:
    home, prefix = installed
    (prefix / "adp.prev").write_text((prefix / "adp").read_text().replace(f'ADP_VERSION="{CURRENT_VERSION}"', 'ADP_VERSION="0.9.0"'))

    result = run([str(prefix / "adp"), "update", "--rollback", "--to", "0.9.0"], home)

    assert result.returncode == 0, result.stderr
    assert run([str(prefix / "adp"), "version"], home).stdout.strip() == "adp 0.9.0"


def test_the_rollback_version_is_read_from_the_code_not_a_manifest(installed) -> None:
    """A manifest that disagrees with the code must not decide the outcome."""
    home, prefix = installed
    (prefix / "adp.prev").write_text((prefix / "adp").read_text().replace(f'ADP_VERSION="{CURRENT_VERSION}"', 'ADP_VERSION="0.9.0"'))
    (prefix / MANIFEST).write_text(json.dumps({"version": CURRENT_VERSION, "previous_version": "7.7.7"}))

    result = run([str(prefix / "adp"), "update", "--rollback", "--to", "7.7.7"], home)

    assert result.returncode == 1, "the manifest's claim must not override the actual code"


# --- the extension is carried by the pinned package -------------------------


def test_the_install_places_the_superplane_extension(installed) -> None:
    _, prefix = installed

    assert (prefix / "adp-superplane.py").is_file()


def test_the_installed_extension_is_dispatchable(installed) -> None:
    """End to end: the installed `adp` reaches the installed extension."""
    home, prefix = installed
    result = run([str(prefix / "adp"), "superplane", "org"], home)

    assert result.returncode == 4, result.stdout + result.stderr


def test_rollback_restores_the_extension_too(installed) -> None:
    """Every CLI_FILES entry rolls back together, or the set is inconsistent."""
    home, prefix = installed
    extension = prefix / "adp-superplane.py"
    (prefix / "adp-superplane.py.prev").write_text("#!/usr/bin/env python3\n# previous generation\n")
    (prefix / "adp.prev").write_text((prefix / "adp").read_text())

    result = run([str(prefix / "adp"), "update", "--rollback"], home)

    assert result.returncode == 0, result.stderr
    assert "previous generation" in extension.read_text()


# --- regression: the unpinned paths keep their meaning (R16 acc. 5) ---------


def test_bare_rollback_without_a_previous_version_still_fails_clearly(installed) -> None:
    home, prefix = installed
    result = run([str(prefix / "adp"), "update", "--rollback"], home)

    assert result.returncode == 1
    assert "No previous version" in result.stderr


def test_bare_update_without_a_known_gateway_still_says_how_to_reinstall(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    prefix = tmp_path / "bin"
    run([str(INSTALL), "--prefix", str(prefix), "--gateway-url", "https://gw.test", "--no-path-edit"], home)
    (home / ".bedrock-gateway/config.json").write_text("{}")

    result = run([str(prefix / "adp"), "update"], home)

    assert result.returncode == 1
    assert "install.sh" in result.stderr


def test_an_unknown_update_option_is_still_rejected(installed) -> None:
    home, prefix = installed
    result = run([str(prefix / "adp"), "update", "--nope"], home)

    assert result.returncode == 1
