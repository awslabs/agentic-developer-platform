"""The build lanes declare their source mechanism — Issue #5041 (U2), EPIC #4910.
Updated for the source-ownership transfer — Issue #5326 (U22).

Two forbidden ways to obtain the source, each with its own consequence:

  * ``actions/checkout`` of ``aws-innovate/AISuperPlane`` — the hosted agent token is scoped
    to *this* repository, so the workflow fails for everyone. Worse than failing: it fails
    with a checkout error that reads like a transient CI fault, so the real cause (a missing
    access grant) stays hidden.
  * reading the reference snapshot at ``modules/domain-apps/ai-super-plane/reference/`` —
    that is read-only evidence at upstream ``5d543c95``. Building from it would ship
    evidence as a product and silently pin a revision that is already stale.

Both remain forbidden after U22, and for a sharper reason than before: the transfer put the
source in this repository, so a lane reaching for either one is not merely unauthorized, it
is reaching past the copy ADP maintains to a copy nobody maintains. The reference snapshot in
particular must not become a second writable runtime tree.

What changed is the positive obligation. Before the transfer, the lanes had to **state** an
assumed mechanism and record it as unresolved, because choosing one was an access grant this
repo could not make. Now the lock records the resolved mechanism — a transferred source
location inside ADP's control — and the lanes must resolve their build context from it rather
than hardcoding a path. So the tests below check the same two prohibitions, plus that the
declared source is the maintained in-repository tree.

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

# Where U22 (#5326) placed the maintained source. Build contexts must be under this.
MAINTAINED_SRC_ROOT = "modules/domain-apps/superplane/src"
COMPONENTS = (
    "superplane-api",
    "superplane-controller",
    "superplane-platform-monitor",
)


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


def _step_bodies(path: Path) -> list[str]:
    """Every step's executable content: `run` scripts plus `with:` inputs.

    Deliberately excludes `on.push.paths` and step names, so a check for "does this lane
    hardcode a path" tests what the lane *executes* rather than what it documents or watches.
    """
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    bodies: list[str] = []
    for job in (parsed.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if step.get("run"):
                bodies.append(str(step["run"]))
            for value in (step.get("with") or {}).values():
                bodies.append(str(value))
    return bodies


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
    """The positive obligation: say where the source comes from, and get it from the lock."""

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

    def test_workflow_has_an_explicit_source_step(self, workflow: Path) -> None:
        """The one place source is established is named, so it cannot be added ad hoc.

        Before U22 this step was the (unwritten) upstream fetch. After the transfer there is
        nothing to fetch — ``actions/checkout`` of this repository already brought the source
        — so the named step *verifies* that the directory the lock points at is really here.
        The obligation is unchanged: exactly one step in the lane is responsible for source,
        so a second one cannot appear quietly further down.
        """
        parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        names = [
            str(s.get("name", ""))
            for job in parsed["jobs"].values()
            for s in job.get("steps") or []
        ]
        assert any("maintained source" in n.lower() for n in names), (
            f"{workflow.name} has no declared maintained-source step"
        )

    def test_no_step_hardcodes_a_component_source_path(self, workflow: Path) -> None:
        """The build context must come from the resolver, not be spelled out in a step.

        A hardcoded path would build correctly today and silently keep building the old
        directory the day the lock moves a component — the same "pinning is a document, not a
        mechanism" failure the resolver exists to prevent, one level down.

        Scoped to step bodies on purpose. The `on.push.paths` filter *must* name the
        directory: that is how a change to the maintained source triggers a rebuild at all,
        and GitHub does not evaluate expressions there, so it cannot be resolved from the
        lock. Blanket-matching the whole file would force that filter to be deleted, which
        would trade a cosmetic win for a lane that no longer notices its own source changing.
        """
        for body in _step_bodies(workflow):
            for component in COMPONENTS:
                assert f"{MAINTAINED_SRC_ROOT}/{component}" not in body, (
                    f"{workflow.name} hardcodes a source path in a step instead of "
                    f"resolving it: {body.strip()[:120]!r}"
                )

    def test_workflow_reads_the_resolved_build_context(self, workflow: Path) -> None:
        """Positive counterpart: the lane must actually use what the resolver handed back."""
        text = workflow.read_text(encoding="utf-8")
        assert "SUPERPLANE_SOURCE_DIR" in text, (
            f"{workflow.name} never reads the resolved build context"
        )


class TestLockRecordsTheTransferredMechanism:
    """After U22 the mechanism is resolved — and resolved to a location ADP maintains."""

    @staticmethod
    def _lock() -> dict:
        return yaml.safe_load(
            (MODULE_ROOT / "releases" / "superplane.lock.yaml").read_text(
                encoding="utf-8"
            )
        )

    def test_lock_states_the_mechanism_is_resolved(self) -> None:
        assert self._lock()["source_access"]["status"] == "resolved"

    def test_lock_names_the_resolved_mechanism_and_who_resolved_it(self) -> None:
        """A resolved status with no named mechanism would be a claim without a subject."""
        access = self._lock()["source_access"]
        assert access["mechanism"], "the lock does not say which mechanism was adopted"
        assert access["resolved_by"]["issue"] == 5326

    def test_lock_still_records_what_was_ruled_out(self) -> None:
        """Kept after resolution: these are still the wrong ways to obtain this source.

        Deleting them once the grant question went away would lose the reason a future
        reader should not "simplify" the lanes by checking out upstream.
        """
        ruled_out = self._lock()["source_access"]["ruled_out"]
        mechanisms = " ".join(str(entry["mechanism"]) for entry in ruled_out).lower()
        assert "checkout" in mechanisms
        assert "snapshot" in mechanisms

    def test_lock_records_the_maintained_source_location(self) -> None:
        maintained = self._lock()["maintained_source"]
        assert maintained["root"] == MAINTAINED_SRC_ROOT
        assert maintained["transferred_by"]["issue"] == 5326

    def test_every_component_source_path_is_under_the_maintained_root(self) -> None:
        """The lock must not point a build anywhere except the tree ADP maintains."""
        lock = self._lock()
        for component, entry in (lock["pending_images"] or {}).items():
            expected = (
                "executor" if component == "superplane-executor" else f"src/{component}"
            )
            assert entry["source_path"] == expected, (
                f"{component} does not resolve to its maintained directory: {entry!r}"
            )
            assert (MODULE_ROOT / entry["source_path"]).is_dir(), (
                f"{component} names {entry['source_path']!r}, which does not exist"
            )

    def test_origin_provenance_is_recorded_separately_from_maintained_source(
        self,
    ) -> None:
        """The two facts the issue requires be kept apart.

        One "revision" field could only carry one of them, and whichever it carried, a reader
        would lose the ability to tell whether the maintained files have changed since the
        transfer.
        """
        lock = self._lock()
        assert lock["upstream"]["repository"].endswith("AISuperPlane")
        assert re.fullmatch(r"[0-9a-f]{40}", str(lock["upstream"]["revision"]))
        assert "role" in lock["upstream"], (
            "upstream must say it is provenance, not a build input"
        )
        assert "not a build input" in lock["upstream"]["role"]
        assert lock["maintained_source"]["repository"] != lock["upstream"]["repository"]

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
