"""Unit tests for the three-value liveness verdict (issue #4176).

The load-bearing test in this file is `test_stale_active_is_unverifiable_not_exited`.
Everything else guards the edges around it. ORCA's documented failure mode is
exactly the collapse of `unverifiable` into `exited`: doing so "orphans live work
and can cold-start a duplicate over the same worktree" — in ADP terms, two agents
racing on one issue. These tests pin the invariant so a future refactor cannot
reintroduce that collapse silently.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.activity.liveness import (
    ACTIVE_STATUSES,
    OBSERVED_TERMINAL_STATUSES,
    compute_liveness,
)
from src.activity.stats_service import _ACTIVE_STALENESS_HOURS

# A fixed "now" so the boundary cases are deterministic.
NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    """Format as the `arrived_at` shape written by webhook-ingress."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**kwargs) -> str:
    """An `arrived_at` timestamp the given delta before NOW."""
    return iso(NOW - timedelta(**kwargs))


class TestObservedExits:
    """A positively-observed terminal status must never be downgraded."""

    @pytest.mark.parametrize(
        "status",
        ["complete", "failed", "rejected", "rate_limited", "no_op", "blocked", "skipped"],
    )
    def test_each_terminal_status_is_exited(self, status):
        """Every observed terminal status returns `exited`."""
        assert compute_liveness(status, ago(minutes=5), NOW) == "exited"

    @pytest.mark.parametrize(
        "status",
        ["complete", "failed", "rejected", "rate_limited", "no_op", "blocked", "skipped"],
    )
    def test_terminal_status_is_exited_regardless_of_age(self, status):
        """Age is irrelevant once an exit was observed.

        A run that completed a year ago is still `exited`. The staleness window
        governs only the *absence* of a signal, never the presence of one.
        """
        assert compute_liveness(status, ago(days=400), NOW) == "exited"

    def test_terminal_set_matches_the_documented_vocabulary(self):
        """The hoisted set is exactly the #4020-era terminal vocabulary.

        Pinned because `service.py` derives `completed_at` from this same set —
        adding a status here silently changes that behaviour too.
        """
        assert OBSERVED_TERMINAL_STATUSES == frozenset({"complete", "failed", "rejected", "rate_limited", "no_op", "blocked", "skipped"})


class TestActiveRuns:
    """An active run is `live` only while its signal is recent."""

    @pytest.mark.parametrize("status", sorted(ACTIVE_STATUSES))
    def test_recent_active_is_live(self, status):
        """An active run inside the cutoff returns `live`."""
        assert compute_liveness(status, ago(hours=1), NOW) == "live"

    def test_stale_active_is_unverifiable_not_exited(self):
        """THE core correctness assertion.

        An `in_progress` run older than the cutoff is `unverifiable` — we have
        lost contact, which is not evidence that it ended. Asserting `exited`
        here is the bug this whole issue exists to prevent.
        """
        verdict = compute_liveness("in_progress", ago(hours=_ACTIVE_STALENESS_HOURS + 1), NOW)
        assert verdict == "unverifiable"
        assert verdict != "exited"

    def test_stale_webhook_received_is_unverifiable(self):
        """A delivery accepted but never advanced past the cutoff is indeterminate."""
        assert compute_liveness("webhook_received", ago(hours=_ACTIVE_STALENESS_HOURS + 1), NOW) == "unverifiable"

    def test_very_old_active_run_never_becomes_exited(self):
        """Time alone can never manufacture evidence of an exit."""
        assert compute_liveness("in_progress", ago(days=400), NOW) == "unverifiable"


class TestBoundary:
    """The cutoff boundary is deterministic and pinned to a side."""

    def test_exactly_at_cutoff_is_live(self):
        """A signal exactly at the cutoff counts as fresh (inclusive, `>=`).

        Pinned explicitly so a future refactor cannot flip the comparison from
        `>=` to `>` unnoticed. Matches `stats_service._aggregate`, which uses
        `arrived_at >= staleness_cutoff` — the two must agree, or the dashboard's
        `stale_count` and this per-run verdict would contradict each other on the
        same run.
        """
        assert compute_liveness("in_progress", ago(hours=_ACTIVE_STALENESS_HOURS), NOW) == "live"

    def test_one_second_past_cutoff_is_unverifiable(self):
        """One second beyond the cutoff flips to `unverifiable`."""
        stale = ago(hours=_ACTIVE_STALENESS_HOURS, seconds=1)
        assert compute_liveness("in_progress", stale, NOW) == "unverifiable"

    def test_reuses_the_stats_cutoff_constant(self):
        """The cutoff is #3696's, not a second one invented here."""
        assert _ACTIVE_STALENESS_HOURS == 24


class TestUnknownAndMissing:
    """The default must be indeterminate, never exit."""

    def test_unrecognised_status_is_unverifiable(self):
        """A status this build has never heard of is indeterminate.

        Producers add statuses on their own cadence (#4020 added two). A new
        status must not have its live runs reported as finished.
        """
        assert compute_liveness("some_future_status", ago(minutes=1), NOW) == "unverifiable"

    def test_absent_status_is_unverifiable(self):
        """A row with no status at all is indeterminate."""
        assert compute_liveness(None, ago(minutes=1), NOW) == "unverifiable"

    def test_empty_status_is_unverifiable(self):
        """An empty-string status is indeterminate."""
        assert compute_liveness("", ago(minutes=1), NOW) == "unverifiable"

    def test_missing_timestamp_on_active_run_is_unverifiable(self):
        """We cannot date the signal, so we do not claim it is fresh."""
        assert compute_liveness("in_progress", "", NOW) == "unverifiable"

    def test_unparseable_timestamp_on_active_run_is_unverifiable(self):
        """A malformed timestamp degrades to indeterminate, it does not raise.

        `arrived_at` is an untyped DDB string attribute; a garbage value must
        produce a verdict, not a 500 on the Agent Activity list.
        """
        assert compute_liveness("in_progress", "not-a-timestamp", NOW) == "unverifiable"

    def test_case_sensitivity_does_not_leak_an_exit(self):
        """Status matching is exact — a near-miss is indeterminate, not exited."""
        assert compute_liveness("COMPLETE", ago(minutes=1), NOW) == "unverifiable"


class TestNeverAssertExitWithoutEvidence:
    """The invariant, stated as a test (property-style over a wide input space)."""

    @pytest.mark.parametrize(
        "status",
        [
            None,
            "",
            "in_progress",
            "webhook_received",
            "unknown",
            "some_future_status",
            "Complete",
            "exited",
            "live",
            "unverifiable",
            "queued",
            "running",
            "terminated",
            "evicted",
            "oom_killed",
            "deadline_exceeded",
        ],
    )
    @pytest.mark.parametrize(
        "arrived_at",
        ["", "not-a-timestamp", iso(NOW), iso(NOW - timedelta(days=400)), iso(NOW + timedelta(days=1))],
    )
    def test_no_non_terminal_input_produces_exited(self, status, arrived_at):
        """No input outside OBSERVED_TERMINAL_STATUSES may return `exited`."""
        assert compute_liveness(status, arrived_at, NOW) != "exited"

    @pytest.mark.parametrize(
        "status",
        [None, "", "in_progress", "webhook_received", "unknown", "evicted", "oom_killed"],
    )
    @pytest.mark.parametrize("arrived_at", ["", "garbage", iso(NOW), iso(NOW - timedelta(days=400))])
    def test_verdict_is_always_one_of_three_values(self, status, arrived_at):
        """The function is total: every input maps to a valid verdict."""
        assert compute_liveness(status, arrived_at, NOW) in {"live", "unverifiable", "exited"}

    def test_active_and_terminal_vocabularies_are_disjoint(self):
        """A status cannot be both under way and finished.

        If these sets ever overlap, the ordering of the checks in
        `compute_liveness` would silently decide the answer.
        """
        assert not (ACTIVE_STATUSES & OBSERVED_TERMINAL_STATUSES)
