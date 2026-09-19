"""A reviewer's review documents never become a PR of their own (#5350 AC3, #5348).

When the engine's reviewer could not record a verdict (GitHub 422s a self-review),
it fell back to whatever remained available. One of those fallbacks was committing
the review as files: PR #5240 carried 10 review documents totalling +1991 lines, all
about a DIFFERENT PR, on a different branch — and `data/code-review/` on main holds
131 such files. Because that branch then had a diff, it opened its own PR, which was
itself reviewable, which spawned another reviewer (#5348). The verdict-with-nowhere-
to-go became branch noise that consumed reviewer capacity.

`_branch_changes_are_transcript_only` is the guard: a branch whose every change is a
review transcript is pushed for archival but opens no PR. The guard already existed
and was CORRECT — but it had no test of its own (the only reference to it in the
suite stubs it out), so nothing stopped a refactor from removing it and bringing the
junk-PR pattern back. These tests pin it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _with_changed_files(monkeypatch, entrypoint, files: list[str]):
    """Stub `git diff --name-only origin/main...HEAD` to report `files`."""

    def run(cmd, **_kwargs):
        if cmd[:2] == ["git", "diff"]:
            return MagicMock(stdout="\n".join(files) + ("\n" if files else ""), returncode=0)
        return MagicMock(stdout="", returncode=0)

    monkeypatch.setattr(entrypoint, "run_cmd", run)


@pytest.mark.parametrize(
    "files",
    [
        ["data/code-review/review-20260917-pr-5347.md"],
        # The #5240 shape: many transcripts at once, still not a code change.
        [f"data/code-review/review-2026091{n}-pr-523{n}.md" for n in range(5)],
    ],
)
def test_transcript_only_branch_opens_no_pr(monkeypatch, files):
    import entrypoint

    _with_changed_files(monkeypatch, entrypoint, files)
    assert entrypoint._branch_changes_are_transcript_only("agent/issue-5350") is True


@pytest.mark.parametrize(
    "files",
    [
        # A reviewer that repaired code during review MUST still get its PR — that
        # is real delivery, and suppressing it would lose the fix.
        ["modules/gateway/src/orchestration/results.py"],
        # Mixed: one real change alongside transcripts is still a real change.
        ["data/code-review/review-20260917-pr-5347.md", "modules/gateway/src/internal/routes.py"],
        # A path that merely starts with a similar prefix is not a transcript.
        ["data/code-review-notes/summary.md"],
    ],
)
def test_branch_with_real_code_changes_still_opens_a_pr(monkeypatch, files):
    import entrypoint

    _with_changed_files(monkeypatch, entrypoint, files)
    assert entrypoint._branch_changes_are_transcript_only("agent/issue-5350") is False


def test_empty_diff_is_not_treated_as_transcript_only(monkeypatch):
    """An empty branch is handled earlier in Step 11; conflating them hides bugs."""
    import entrypoint

    _with_changed_files(monkeypatch, entrypoint, [])
    assert entrypoint._branch_changes_are_transcript_only("agent/issue-5350") is False


@pytest.mark.parametrize("failure", [subprocess.CalledProcessError(1, "git"), OSError("git missing")])
def test_git_failure_fails_soft_towards_opening_a_pr(monkeypatch, failure):
    """Fail-soft direction matters: a spurious PR is recoverable, lost work is not."""
    import entrypoint

    def run(cmd, **_kwargs):
        raise failure

    monkeypatch.setattr(entrypoint, "run_cmd", run)
    assert entrypoint._branch_changes_are_transcript_only("agent/issue-5350") is False
