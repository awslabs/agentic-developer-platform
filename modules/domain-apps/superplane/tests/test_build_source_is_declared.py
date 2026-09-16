"""The build lanes declare their source mechanism — Issue #5041 (U2), EPIC #4910.

Two forbidden ways to obtain the pinned source, each with its own consequence:

  * ``actions/checkout`` of ``aws-innovate/AISuperPlane`` — the hosted agent token is scoped
    to *this* repository, so the workflow fails for everyone. Worse than failing: it fails
    with a checkout error that reads like a transient CI fault, so the real cause (a missing
    access grant) stays hidden.
  * reading the reference snapshot at ``modules/domain-apps/ai-super-plane/reference/`` —
    that is read-only evidence at upstream ``5d543c95``. Building from it would ship
    evidence as a product and silently pin a revision that is already stale.

So the lanes must instead **state** which mechanism they assume and record it as unresolved.
That choice is an access grant, not a planning decision, and this story does not make it.

## Why these tests check usage rather than mentions

The lanes and the lock legitimately *discuss* both forbidden mechanisms — that is how a
reader learns why the obvious approach is ruled out, and deleting the explanation to satisfy
a naive substring check would make the codebase worse. A test that failed on any mention
would therefore punish the documentation and reward silence.

So each check targets the mechanism as an *executed step*: a `uses: actions/checkout` whose
`with.repository` is upstream, or a non-comment line that reads the snapshot path. Comments
are excluded deliberately, and there is a positive test asserting the explanation is
present, so the two pressures balance.
"""

from __future__ import annotations

import _release_path  # noqa: F401

import re
from pathlib import Path

import pytest
import yaml

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]

WORKFLOWS = sorted(
    [
        REPO_ROOT / ".github/workflows/superplane-api-build.yml",
        REPO_ROOT / ".github/workflows/superplane-controller-build.yml",
        REPO_ROOT / ".github/workflows/superplane-monitor-build.yml",
    ]
)

UPSTREAM_REPO_SLUG = "aws-innovate/AISuperPlane"
SNAPSHOT_PREFIX = "modules/domain-apps/ai-super-plane/reference/"


def _non_comment_lines(path: Path) -> list[str]:
    """Lines with full-line comments removed.

    Trailing comments are kept attached to their line: a step that reads the snapshot and
    then explains itself in a trailing comment is still a step that reads the snapshot.
    """
    return [
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if not ln.strip().startswith("#")
    ]


def _checkout_steps(path: Path) -> list[dict]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps: list[dict] = []
    for job in (parsed.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and str(step.get("uses", "")).startswith(
                "actions/checkout"
            ):
                steps.append(step)
    return steps


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
class TestNoUpstreamCheckout:
    def test_no_checkout_step_targets_the_upstream_repository(
        self, workflow: Path
    ) -> None:
        for step in _checkout_steps(workflow):
            repository = str((step.get("with") or {}).get("repository", ""))
            assert UPSTREAM_REPO_SLUG.lower() not in repository.lower(), (
                f"{workflow.name} checks out {repository!r}"
            )

    def test_checkout_steps_take_no_repository_override_at_all(
        self, workflow: Path
    ) -> None:
        """Belt and braces: any cross-repo checkout here is out of scope for this lane."""
        for step in _checkout_steps(workflow):
            assert "repository" not in (step.get("with") or {}), (
                f"{workflow.name} has a cross-repo checkout"
            )

    def test_no_executed_line_clones_the_upstream_repository(
        self, workflow: Path
    ) -> None:
        """Covers `git clone`/`gh repo clone` in a run block, which the YAML check misses."""
        for line in _non_comment_lines(workflow):
            if (
                UPSTREAM_REPO_SLUG.lower() in line.lower()
                or "aisuperplane" in line.lower()
            ):
                assert not re.search(
                    r"\b(git\s+clone|gh\s+repo\s+clone|git\s+fetch|submodule)\b", line
                ), f"{workflow.name} clones upstream: {line.strip()!r}"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
class TestSnapshotIsNotABuildInput:
    def test_no_executed_line_reads_the_reference_snapshot(
        self, workflow: Path
    ) -> None:
        for line in _non_comment_lines(workflow):
            assert SNAPSHOT_PREFIX not in line, (
                f"{workflow.name} reads the reference snapshot: {line.strip()!r}"
            )

    def test_no_executed_line_fetches_the_planning_branch(self, workflow: Path) -> None:
        """The snapshot lives on `agent/issue-4910`; fetching it is the other way in."""
        for line in _non_comment_lines(workflow):
            assert "agent/issue-4910" not in line, (
                f"{workflow.name} fetches the planning branch: {line.strip()!r}"
            )

    def test_path_filters_do_not_match_the_snapshot(self, workflow: Path) -> None:
        """The snapshot is verified inert — no path filter may make it trigger a build."""
        parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        triggers = parsed.get("on") or parsed.get(True)
        for path_glob in (triggers.get("push") or {}).get("paths") or []:
            assert "ai-super-plane" not in path_glob, (
                f"{workflow.name} path filter matches the snapshot: {path_glob!r}"
            )


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
class TestSourceMechanismIsStated:
    """The positive obligation: say which mechanism is assumed, and that it is unresolved."""

    def test_workflow_explains_why_the_upstream_checkout_is_ruled_out(
        self, workflow: Path
    ) -> None:
        """Guards the explanation itself, so the checks above cannot be satisfied by silence."""
        text = workflow.read_text(encoding="utf-8").lower()
        assert "scoped to" in text, (
            f"{workflow.name} does not explain why the upstream checkout is ruled out"
        )

    def test_workflow_defers_to_the_lock_for_the_mechanism(
        self, workflow: Path
    ) -> None:
        text = workflow.read_text(encoding="utf-8")
        assert "source_access" in text, (
            f"{workflow.name} does not reference the lock's source_access"
        )

    def test_workflow_has_an_explicit_source_acquisition_step(
        self, workflow: Path
    ) -> None:
        """The one place source would be obtained is named, so it cannot be added ad hoc."""
        parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        names = [
            str(s.get("name", ""))
            for job in parsed["jobs"].values()
            for s in job.get("steps") or []
        ]
        assert any("pinned upstream source" in n.lower() for n in names), (
            f"{workflow.name} has no declared source-acquisition step"
        )


class TestLockRecordsTheMechanismAsUnresolved:
    def test_lock_states_the_mechanism_is_unresolved(self) -> None:
        lock = yaml.safe_load(
            (MODULE_ROOT / "releases" / "superplane.lock.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert lock["source_access"]["status"] == "unresolved"

    def test_lock_does_not_choose_a_mechanism(self) -> None:
        """Recording candidates is required; picking one would be making the access grant."""
        lock = yaml.safe_load(
            (MODULE_ROOT / "releases" / "superplane.lock.yaml").read_text(
                encoding="utf-8"
            )
        )
        access = lock["source_access"]
        assert "chosen_mechanism" not in access
        assert len(access["candidate_mechanisms"]) >= 2

    def test_lock_names_what_the_unresolved_grant_blocks(self) -> None:
        lock = yaml.safe_load(
            (MODULE_ROOT / "releases" / "superplane.lock.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert lock["source_access"]["blocks"], (
            "the lock does not say what the missing grant blocks"
        )

    def test_snapshot_is_not_referenced_as_a_build_input_in_the_lock(self) -> None:
        """The lock may explain the snapshot is excluded; it may not point a build at it."""
        text = (MODULE_ROOT / "releases" / "superplane.lock.yaml").read_text(
            encoding="utf-8"
        )
        for line in text.splitlines():
            if line.strip().startswith("#"):
                continue
            assert SNAPSHOT_PREFIX not in line, (
                f"the lock references the snapshot in a value: {line.strip()!r}"
            )
