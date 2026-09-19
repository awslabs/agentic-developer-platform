"""A CLI-only change must reach the served artifact (Issue #5039).

`gateway-deploy.yml` filters twice, and BOTH must include `modules/gateway/cli/`:

1. `on.push.paths` decides whether the workflow runs at all;
2. the `changes` job's BACKEND regex decides whether `deploy-backend` runs.

Either one missing the directory produces the same silent failure: a CLI change
merges to main, CI reports green, and the gateway keeps serving the OLD helper —
so `adp superplane` exists in the repo and 404s for every user who installs. The
two-filter structure is what makes it silent: a workflow that never triggers and
a workflow that triggers but skips its only deploy job are indistinguishable from
a passing build unless you go looking.

#5037 (U1) closed this gap. These tests are the fence that keeps it closed: the
filters are long, hand-maintained lists that get reorganized, and nothing else in
the suite would notice `modules/gateway/cli/` being dropped from one of them.

The BACKEND test EXECUTES the filter script against a synthetic diff rather than
grepping for the pattern. A regex can be present and still not match — anchors,
alternation and escaping all fail quietly — so the only assertion worth making is
on the decision the shell actually writes to `$GITHUB_OUTPUT`.
"""

from __future__ import annotations

import os
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).parents[4]
WORKFLOW = REPO / ".github/workflows/gateway-deploy.yml"

# Files whose change must deliver a new CLI artifact. The extension is the point
# of this story; `adp` and `install.sh` are what carry it to a user's machine.
CLI_FILES = [
    "modules/gateway/cli/adp-superplane.py",
    "modules/gateway/cli/adp",
    "modules/gateway/cli/install.sh",
    "modules/gateway/cli/adp_common.py",
]


@pytest.fixture(scope="module")
def workflow() -> dict:
    # BaseLoader keeps `on` a string key: YAML 1.1 would read it as the boolean True.
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


@pytest.fixture(scope="module")
def filter_script(workflow) -> str:
    step = next(step for step in workflow["jobs"]["changes"]["steps"] if step.get("id") == "filter")
    return step["run"].replace("${{ github.event_name }}", "push")


def decisions_for(changed: str, script: str, tmp_path: Path) -> dict[str, str]:
    """Run the real filter script over a synthetic one-file diff."""
    output = tmp_path / "github_output"
    subprocess.run(
        ["bash", "-eu", "-c", 'git() { printf "%s\\n" "$CLI_TEST_CHANGED"; }\n' + script],
        env={**os.environ, "CLI_TEST_CHANGED": changed, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


# --- filter 1: the workflow triggers at all ----------------------------------


@pytest.mark.parametrize("changed", CLI_FILES)
def test_a_cli_change_triggers_the_workflow(changed, workflow) -> None:
    patterns = workflow["on"]["push"]["paths"]

    assert any(fnmatchcase(changed, pattern) for pattern in patterns), f"a push touching only {changed} would not run gateway-deploy at all"


def test_the_cli_directory_is_matched_by_a_wildcard_not_a_file_list(workflow) -> None:
    """A new helper must be delivered without also editing the workflow."""
    patterns = workflow["on"]["push"]["paths"]

    assert any(fnmatchcase("modules/gateway/cli/some-future-helper.py", pattern) for pattern in patterns)


# --- filter 2: deploy-backend actually runs ----------------------------------


@pytest.mark.parametrize("changed", CLI_FILES)
def test_a_cli_change_selects_the_backend_deploy(changed, filter_script, tmp_path) -> None:
    """The gap that shipped a CLI to main and served the old one."""
    decisions = decisions_for(changed, filter_script, tmp_path)

    assert decisions["backend"] == "true", f"{changed} changed but deploy-backend would be skipped, so nothing new is served"


def test_a_cli_change_does_not_force_an_unrelated_frontend_build(filter_script, tmp_path) -> None:
    """Closing the gap must not make every CLI edit a full-stack redeploy."""
    decisions = decisions_for("modules/gateway/cli/adp-superplane.py", filter_script, tmp_path)

    assert decisions["frontend"] == "false"
    assert decisions["budget_lambdas"] == "false"


def test_an_unrelated_change_still_skips_the_backend(filter_script, tmp_path) -> None:
    """Guards against a filter so broad that `backend=true` is meaningless."""
    decisions = decisions_for("docs/adp-platform-deployment/deploy-quickstart.md", filter_script, tmp_path)

    assert decisions["backend"] == "false"


# --- the two filters must agree ----------------------------------------------


def test_both_filters_cover_the_cli_directory(workflow, filter_script, tmp_path) -> None:
    """Passing one and failing the other is the silent case — assert together."""
    changed = "modules/gateway/cli/adp-superplane.py"
    triggers = any(fnmatchcase(changed, pattern) for pattern in workflow["on"]["push"]["paths"])
    deploys = decisions_for(changed, filter_script, tmp_path)["backend"] == "true"

    assert (triggers, deploys) == (True, True), "on.push.paths and the BACKEND regex disagree about modules/gateway/cli/"


def test_the_dockerfile_bakes_the_cli_into_the_image(workflow) -> None:
    """A triggered deploy delivers nothing if the image does not carry the files."""
    dockerfile = (REPO / "modules/gateway/Dockerfile").read_text()

    assert "COPY cli/ cli/" in dockerfile
