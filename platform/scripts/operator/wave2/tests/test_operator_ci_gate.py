"""The operator fixture tooling must be gated by a real CI check.

Root's finding 5808491910: PR #5839's `statusCheckRollup` was EMPTY. No workflow in
the branch referenced `platform/scripts/operator/wave2` at all, so every change to
the executable lifecycle -- the scripts that create real AWS and Kubernetes objects,
and the teardown that removes them -- merged with no automated check of any kind. The
local suite passing is not a merge gate; nothing required it to have been run.

These tests assert the gate exists and is wired to the paths it claims to cover. They
are deliberately about the WORKFLOW FILE rather than about any script's behaviour,
because the failure being prevented is not a bug in a script -- it is a check that
isn't there. A gate that stops triggering fails silently and looks identical to a gate
that passes, so the wiring itself needs a test.

`yaml` is a real dependency of this file, and the CI job installs it for exactly this
reason. If it is missing the tests below FAIL rather than skip: a skipped assertion
that a required check exists is indistinguishable from a satisfied one, which is the
same false-green this file exists to close.
"""
from __future__ import annotations

from pathlib import Path

import pytest

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised by its own absence
    # NOT importorskip. A skip here would mean "the required check might not exist"
    # rendered as a green tick, which is the exact false-green this file closes. The
    # CI job installs pyyaml, so an ImportError is a broken gate, not a soft absence.
    raise RuntimeError(
        "pyyaml is required to verify the CI gate's wiring and is missing. Install it "
        "(`pip install pytest pyyaml`) -- these assertions must not be skipped, because "
        "a skipped check that a required check exists looks identical to a satisfied one."
    ) from exc

# tests/ -> wave2 -> operator -> scripts -> platform -> repo root
REPO_ROOT = Path(__file__).resolve().parents[5]
WORKFLOW = REPO_ROOT / ".github/workflows/agent-control-ci.yml"
TREE = "platform/scripts/operator/wave2"
JOB = "operator-fixture-tests"
# The rendered check name. Renaming a job silently removes a required check rather
# than failing the build, so the string is part of the contract, not a label.
CHECK_NAME = "Operator fixture tests"


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert WORKFLOW.is_file(), (
        f"{WORKFLOW} does not exist. The operator fixture tooling would have no merge "
        "check at all, which is the condition root found on PR #5839."
    )
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def triggers_of(workflow: dict) -> dict:
    """The `on:` block, under whichever key the YAML loader produced.

    GitHub Actions spells the trigger key `on`, which YAML 1.1 -- and therefore
    `yaml.safe_load` -- reads as the BOOLEAN True, not the string "on". Looking up
    only `workflow["on"]` raises KeyError on a perfectly valid workflow, so both
    spellings are accepted here. This is a quirk of the parser, not of the file: the
    workflow itself is correct and GitHub reads it correctly.
    """
    for key in ("on", True):
        if key in workflow:
            return workflow[key]
    raise AssertionError(f"{WORKFLOW.name} declares no triggers at all")


def test_a_job_gates_the_operator_fixture_tree(workflow: dict) -> None:
    jobs = workflow["jobs"]
    assert JOB in jobs, (
        f"no {JOB!r} job in {WORKFLOW.name}. Without it this tree has no CI gate: "
        f"the local suite passing proves nothing about what merged."
    )
    assert jobs[JOB]["name"] == CHECK_NAME, (
        f"the job's rendered check name must stay {CHECK_NAME!r}; a rename removes the "
        "required check silently instead of failing."
    )


def test_the_trigger_covers_this_tree_and_the_workflow_itself(workflow: dict) -> None:
    """Both halves matter, and the second is the one usually forgotten.

    A PR editing a script under this tree must run the gate -- otherwise the gate
    only protects against changes that were never going to break it. And a PR
    editing the WORKFLOW must also run it, or the gate could be weakened or
    disconnected by a change that the gate itself never sees.
    """
    paths = triggers_of(workflow)["pull_request"]["paths"]

    assert any(p == f"{TREE}/**" or p.startswith(f"{TREE}/") for p in paths), (
        f"no path trigger matches {TREE}. A change to the fixture lifecycle would not "
        f"run its own tests. Triggers present: {paths!r}"
    )
    assert ".github/workflows/agent-control-ci.yml" in paths, (
        "the workflow does not trigger on edits to itself, so a change disabling this "
        "gate would not be checked by it."
    )


def test_the_job_runs_the_whole_suite_not_a_chosen_subset(workflow: dict) -> None:
    """A gate that runs only the newest tests cannot catch a regression in the oldest.

    Root asked for the full operator suite specifically: the older tests cover teardown
    and ownership, and those are the paths whose silent failure leaves real resources
    running in the account.
    """
    steps = workflow["jobs"][JOB]["steps"]
    runs = "\n".join(step.get("run", "") for step in steps)

    assert "pytest tests/" in runs, (
        "the job must invoke the whole tests/ directory. Naming individual files means "
        "a new suite is gated and every existing one is not."
    )
    for narrowing in (" -k ", "--ignore=", "--deselect"):
        assert narrowing not in runs, (
            f"the suite invocation uses {narrowing!r}. Every test in this tree is "
            "hermetic, so there is nothing legitimate to exclude from the gate."
        )
    assert "--passWithNoTests" not in runs and "--no-header" not in runs


def test_the_job_installs_its_dependencies_and_fails_if_they_are_absent(
        workflow: dict) -> None:
    """Honest failure on a missing prerequisite, never a skip.

    A suite that skips when its dependencies are absent reports the same green tick as
    one that ran every test. That is the false-green root explicitly ruled out.
    """
    steps = workflow["jobs"][JOB]["steps"]
    runs = "\n".join(step.get("run", "") for step in steps)

    assert "pip install" in runs and "pytest" in runs and "pyyaml" in runs, (
        "the job must install pytest and pyyaml; this file needs yaml to verify the "
        "gate's own wiring."
    )
    assert "exit 1" in runs, (
        "the prerequisite check must exit nonzero when a tool is missing, rather than "
        "continuing and reporting a pass over an unrun suite."
    )


def test_the_gate_needs_no_cloud_credential(workflow: dict) -> None:
    """Hermetic by construction: no AWS role, no cluster, no paid SDK.

    The suite stubs `aws` and `kubectl` on PATH itself (conftest.py). A gate that
    needed a deploy credential to check teardown logic would carry a far larger blast
    radius than the thing it verifies, and live acceptance belongs to the credentialed
    operator run, not to CI.
    """
    job = workflow["jobs"][JOB]
    rendered = yaml.safe_dump(job)

    for forbidden in ("aws-actions/configure-aws-credentials", "role-to-assume",
                      "ANTHROPIC_API_KEY", "aws sts", "secrets."):
        assert forbidden not in rendered, (
            f"the operator gate references {forbidden!r}. It must run with no cloud "
            "credential and no paid SDK access."
        )
    assert "permissions" not in job or job.get("permissions") in (None, {}, "read-all"), (
        "the job should inherit the workflow's read-only permissions rather than "
        "widening them."
    )


def test_the_job_verifies_scripts_parse_and_modules_compile(workflow: dict) -> None:
    """The strongest check available without a cluster.

    These scripts cannot be executed in CI -- they mutate a real account. Syntax
    checking catches the class of error that would otherwise appear for the first time
    mid-run against a live fixture, after some resources already exist and others do
    not.
    """
    runs = "\n".join(
        step.get("run", "") for step in workflow["jobs"][JOB]["steps"])
    assert "bash -n" in runs, "shell scripts must be parse-checked"
    assert "py_compile" in runs, "Python modules must be compile-checked"
