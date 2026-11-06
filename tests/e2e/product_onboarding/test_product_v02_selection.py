"""Exercise the maintained workflow's B02 selection without a deployed target."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.e2e.product_onboarding.product_v02_evidence import CASES

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/e2e-new-ui-playwright.yml"
REVIEWED = tuple(spec[2] for spec in CASES.values())


def workflow_selection(tmp_path, *, revision="a" * 40, v11="false", preview="on"):
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(item for item in workflow["jobs"]["e2e-new-ui"]["steps"] if item.get("id") == "browser-run")
    executable = tmp_path / "python"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:3] == ['-m', 'pytest']:\n"
        "    Path('pytest-arguments.json').write_text(json.dumps(sys.argv[3:]))\n"
        "else:\n"
        "    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n"
    )
    executable.chmod(0o700)
    result = subprocess.run(
        ["bash", "-c", step["run"]], cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "PYTHONPATH": str(ROOT),
             "PRODUCT_EXPECTED_REVISION": revision, "PRODUCT_V11_MODE": v11,
             "E2E_NEW_UI_EXPECTED_FLAG": preview},
    )
    arguments = tmp_path / "pytest-arguments.json"
    selected = json.loads(arguments.read_text()) if arguments.exists() else []
    return result, selected[:selected.index("-p")] if selected else []


def test_b02_workflow_selects_only_three_core_profile_cases(tmp_path):
    result, selected = workflow_selection(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert selected == list(REVIEWED)


@pytest.mark.parametrize("revision,preview", [("wrong-revision", "on"), ("a" * 40, "off")])
def test_b02_invalid_release_or_disabled_preview_never_starts_pytest(tmp_path, revision, preview):
    result, selected = workflow_selection(tmp_path, revision=revision, preview=preview)
    assert result.returncode != 0
    assert selected == []


def test_default_ui_workflow_keeps_its_full_suite(tmp_path):
    result, selected = workflow_selection(tmp_path, revision="")
    assert result.returncode == 0, result.stderr
    assert selected == ["tests/e2e/new_ui/"]


def test_v11_workflow_keeps_its_five_integrated_cases(tmp_path):
    from tests.e2e.product_onboarding.product_v11_selection import selectors

    result, selected = workflow_selection(tmp_path, v11="true")
    assert result.returncode == 0, result.stderr
    assert selected == list(selectors())


def test_b02_collects_every_reviewed_case_and_checks_actual_inventory(tmp_path):
    from tests.e2e.product_onboarding.product_v02_selection import main, require_v02_inventory, selectors

    inventory = tmp_path / "inventory.json"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *selectors(), "-p", "regression_pytest",
         "--collect-only", "-q", "--disable-warnings"],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "E2E_NEW_UI_ENABLED": "0", "REGRESSION_INVENTORY": str(inventory),
             "PYTHONPATH": f"{ROOT / '.github/scripts'}:{ROOT}"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert require_v02_inventory(json.loads(inventory.read_text())) == REVIEWED
    assert main(["--inventory", str(inventory)]) == 0
    assert "3 tests collected" in result.stdout


@pytest.mark.parametrize("inventory", [
    [], list(REVIEWED[:-1]), list(REVIEWED) + ["unrelated::test_onboarding"],
    list(REVIEWED[:-1]) + [REVIEWED[0]], list(reversed(REVIEWED)), "all UI tests", None,
])
def test_b02_inventory_refuses_missing_duplicate_or_unrelated_cases(inventory):
    from tests.e2e.product_onboarding.product_v02_selection import require_v02_inventory

    with pytest.raises(ValueError, match="B02 browser inventory"):
        require_v02_inventory(inventory)


def test_b02_command_outputs_selectors_and_rejects_absent_inventory(tmp_path, capsys):
    from tests.e2e.product_onboarding.product_v02_selection import main

    assert main(["--selectors"]) == 0
    assert capsys.readouterr().out.splitlines() == list(REVIEWED)
    with pytest.raises(FileNotFoundError):
        main(["--inventory", str(tmp_path / "missing.json")])


def test_browser_harness_can_advance_without_changing_the_deployed_release(tmp_path):
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["e2e-new-ui"]["steps"]
    checkout = next(step for step in steps if step.get("name") == "Checkout")
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert checkout["with"]["fetch-depth"] == 0
    script = next(step["run"] for step in steps if step.get("name") == "Require selected Product source on reviewed main")

    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=Browser test", "-c", "user.email=browser@example.test",
             "-c", "commit.gpgsign=false", *args], cwd=tmp_path, check=True, capture_output=True, text=True,
        ).stdout.strip()

    git("init", "-b", "main")
    (tmp_path / "application").write_text("deployed application")
    git("add", "application")
    git("commit", "-m", "application release")
    released = git("rev-parse", "HEAD")
    (tmp_path / "browser").write_text("reviewed browser repair")
    git("add", "browser")
    git("commit", "-m", "browser repair")
    harness = git("rev-parse", "HEAD")
    git("checkout", "-b", "unmerged", released)
    (tmp_path / "unmerged").write_text("not a released ancestor")
    git("add", "unmerged")
    git("commit", "-m", "unmerged source")
    unmerged = git("rev-parse", "HEAD")
    git("checkout", "main")

    def guard(source):
        return subprocess.run(
            ["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True,
            env={**os.environ, "PRODUCT_EXPECTED_REVISION": source, "GITHUB_SHA": harness},
        ).returncode

    assert guard(released) == 0
    assert guard(harness) == 0
    assert guard(unmerged) != 0
    assert guard("0" * 40) != 0
    assert guard("HEAD") != 0
    git("checkout", released)
    assert guard(released) != 0, "A stale checkout cannot claim the new browser-code revision"
