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
    ACTIVE_STALENESS_HOURS,
    ACTIVE_STATUSES,
    OBSERVED_TERMINAL_STATUSES,
    compute_liveness,
    last_signal_at,
)

# Issue #4235: the cutoff now lives in `liveness.py` (see TestSharedActiveVocabulary
# for why the arrow flipped). Imported under the old name so the existing boundary
# tests below keep reading as they did.
_ACTIVE_STALENESS_HOURS = ACTIVE_STALENESS_HOURS

# A fixed "now" so the boundary cases are deterministic.
NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    """Format as the `arrived_at` shape written by webhook-ingress."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**kwargs) -> str:
    """An `arrived_at` timestamp the given delta before NOW."""
    return iso(NOW - timedelta(**kwargs))


class TestLastSignalFreshness:
    """Issue #4235: freshness is dated from the LAST SIGNAL, not the start time.

    `arrived_at` is the DDB sort key — when the delivery landed. Using it as the
    freshness input made run AGE a proxy for liveness, so a perfectly healthy run
    that had simply been going for more than a day read `unverifiable`. That is
    precisely the multi-day workload EPIC #4210 exists to support, which made the
    badge wrong for the runs it matters most for.
    """

    def test_multi_day_healthy_run_is_live(self):
        """THE case this issue exists for.

        A run that started four days ago but transitioned five minutes ago is
        `live`. Before #4235 this read `unverifiable` purely because of its age,
        inviting an operator to kill a healthy long-running agent.
        """
        assert (
            compute_liveness(
                "in_progress",
                ago(days=4),
                NOW,
                status_updated_at=ago(minutes=5),
            )
            == "live"
        )

    def test_multi_day_run_with_stale_last_signal_is_unverifiable(self):
        """The other side of the same coin — no over-correction.

        A long-running run whose last transition is ALSO past the cutoff stays
        `unverifiable`. If #4235 had simply treated every `in_progress` row as
        `live`, a genuinely wedged run would read healthy forever and the badge
        would stop flagging real hangs.
        """
        assert (
            compute_liveness(
                "in_progress",
                ago(days=4),
                NOW,
                status_updated_at=ago(days=2),
            )
            == "unverifiable"
        )

    def test_stale_last_signal_beats_a_recent_start(self):
        """`status_updated_at` is preferred even when it is the OLDER timestamp.

        Pins "prefer the authoritative last-transition attribute" over "take the
        max of the two". A row whose status has not moved since well before the
        cutoff is not fresh, regardless of what `arrived_at` says — otherwise a
        clock-skewed or backfilled `arrived_at` could manufacture a `live`.
        """
        assert (
            compute_liveness(
                "in_progress",
                ago(minutes=5),
                NOW,
                status_updated_at=ago(hours=_ACTIVE_STALENESS_HOURS, seconds=1),
            )
            == "unverifiable"
        )

    @pytest.mark.parametrize("absent", [None, ""])
    def test_falls_back_to_arrived_at_when_last_signal_absent(self, absent):
        """Rows with no `status_updated_at` keep their only timestamp.

        The attribute is written by every current producer, but a row predating
        it must not lose its date and silently degrade to `unverifiable`.
        """
        assert compute_liveness("in_progress", ago(hours=1), NOW, status_updated_at=absent) == "live"
        assert compute_liveness("in_progress", ago(days=4), NOW, status_updated_at=absent) == "unverifiable"

    def test_omitting_the_argument_preserves_pre_4235_behaviour(self):
        """The three-argument call shape still dates from `arrived_at`.

        The parameter is optional so no caller is forced to change; a caller that
        does not pass it gets exactly the old semantics.
        """
        assert compute_liveness("in_progress", ago(hours=1), NOW) == "live"
        assert compute_liveness("in_progress", ago(days=4), NOW) == "unverifiable"

    def test_malformed_last_signal_does_not_manufacture_live(self):
        """A garbage `status_updated_at` degrades, it does not report `live`.

        The ISO guard matters most here: lexicographically "not-a-timestamp"
        sorts ABOVE any real timestamp, so without the guard a malformed value
        would assert a positive signal we never received.
        """
        assert compute_liveness("in_progress", ago(minutes=5), NOW, status_updated_at="not-a-timestamp") == "unverifiable"

    def test_terminal_status_ignores_last_signal_entirely(self):
        """An observed exit is `exited` no matter how the timestamps read."""
        assert compute_liveness("complete", ago(days=400), NOW, status_updated_at=ago(minutes=1)) == "exited"

    def test_last_signal_at_prefers_status_updated_at(self):
        """The helper itself: `status_updated_at` wins when present."""
        assert last_signal_at("2026-08-20T10:00:00Z", "2026-08-25T10:00:00Z") == "2026-08-25T10:00:00Z"

    @pytest.mark.parametrize("absent", [None, ""])
    def test_last_signal_at_falls_back_to_arrived_at(self, absent):
        """The helper itself: falls back when `status_updated_at` is unusable."""
        assert last_signal_at("2026-08-20T10:00:00Z", absent) == "2026-08-20T10:00:00Z"

    def test_last_signal_at_returns_empty_when_undatable(self):
        """Neither timestamp usable → "", which routes an active run to unverifiable."""
        assert last_signal_at(None, None) == ""
        assert compute_liveness("in_progress", "", NOW, status_updated_at=None) == "unverifiable"


class TestSharedActiveVocabulary:
    """Issue #4235: the badge and `stale_count` read ONE definition of "active".

    `liveness.ACTIVE_STATUSES` was `{"in_progress", "webhook_received"}` while
    `stats_service._ACTIVE_STATUSES` was `{"in_progress"}`. A stale
    `webhook_received` row therefore read `unverifiable` on its badge but
    contributed 0 to the operator-facing `stale_count` — two numbers on the same
    page contradicting each other about one row.
    """

    def test_stats_service_reuses_the_liveness_active_set(self):
        """Not equal-by-coincidence — the SAME object, so they cannot drift."""
        from src.activity.stats_service import _ACTIVE_STATUSES

        assert _ACTIVE_STATUSES is ACTIVE_STATUSES

    def test_stats_service_reuses_the_liveness_cutoff(self):
        """One cutoff constant, owned here."""
        assert _ACTIVE_STALENESS_HOURS is ACTIVE_STALENESS_HOURS

    def test_webhook_received_is_active_on_both_sides(self):
        """The specific row the two definitions used to disagree about."""
        from src.activity.stats_service import _ACTIVE_STATUSES

        assert "webhook_received" in ACTIVE_STATUSES
        assert "webhook_received" in _ACTIVE_STATUSES

    def test_liveness_does_not_import_from_stats_service(self):
        """The dependency arrow points at the module with no I/O.

        `liveness.py` is pure; `stats_service.py` carries the boto3 query layer.
        Pointing the arrow the other way (as it was before #4235) is what made a
        shared constant impossible without a cycle, which is why two definitions
        of "active" existed in the first place.
        """
        import inspect

        import src.activity.liveness as liveness_module

        source = inspect.getsource(liveness_module)
        assert "import" in source  # sanity: we are reading real source
        assert "from src.activity.stats_service import" not in source


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
        ["complete", "failed", "rejected", "rate_limited", "no_op", "blocked", "skipped", "budget_stopped"],
    )
    def test_terminal_status_is_exited_regardless_of_age(self, status):
        """Age is irrelevant once an exit was observed.

        A run that completed a year ago is still `exited`. The staleness window
        governs only the *absence* of a signal, never the presence of one.
        """
        assert compute_liveness(status, ago(days=400), NOW) == "exited"

    def test_terminal_set_matches_the_documented_vocabulary(self):
        """The hoisted set is exactly the current terminal vocabulary.

        Pinned because `service.py` derives `completed_at` from this same set, and
        `stats_service._TERMINAL_STATUSES` is now an alias for it — adding a status
        here silently changes both.

        Issue #4187 added `budget_stopped`: a run the gateway stopped on a spend
        cap is over, so it must read `exited` rather than sitting at `live` until
        the staleness window expires.
        """
        assert OBSERVED_TERMINAL_STATUSES == frozenset(
            {"complete", "failed", "rejected", "rate_limited", "no_op", "blocked", "skipped", "budget_stopped"}
        )


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
        `>=` to `>` unnoticed. Since #4235 `stats_service._aggregate` calls the
        very same `within_staleness_window`, so the two cannot disagree on a
        boundary run — previously they only matched by coincidence.
        """
        assert compute_liveness("in_progress", ago(hours=_ACTIVE_STALENESS_HOURS), NOW) == "live"

    def test_one_second_past_cutoff_is_unverifiable(self):
        """One second beyond the cutoff flips to `unverifiable`."""
        stale = ago(hours=_ACTIVE_STALENESS_HOURS, seconds=1)
        assert compute_liveness("in_progress", stale, NOW) == "unverifiable"

    def test_reuses_the_stats_cutoff_constant(self):
        """The cutoff is #3696's tuned value, not a second one invented here."""
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

    @pytest.mark.parametrize(
        "status",
        [None, "", "in_progress", "webhook_received", "unknown", "evicted", "oom_killed"],
    )
    @pytest.mark.parametrize("arrived_at", ["", "garbage", iso(NOW), iso(NOW - timedelta(days=400))])
    @pytest.mark.parametrize(
        "status_updated_at",
        [None, "", "garbage", iso(NOW), iso(NOW - timedelta(days=400)), iso(NOW + timedelta(days=1))],
    )
    def test_last_signal_input_cannot_manufacture_an_exited(self, status, arrived_at, status_updated_at):
        """Issue #4235 added an input; the invariant must hold across it too.

        No combination of the new `status_updated_at` argument with any
        non-terminal status may produce `exited`, and the function stays total.
        """
        verdict = compute_liveness(status, arrived_at, NOW, status_updated_at=status_updated_at)
        assert verdict != "exited"
        assert verdict in {"live", "unverifiable"}

    def test_active_and_terminal_vocabularies_are_disjoint(self):
        """A status cannot be both under way and finished.

        If these sets ever overlap, the ordering of the checks in
        `compute_liveness` would silently decide the answer.
        """
        assert not (ACTIVE_STATUSES & OBSERVED_TERMINAL_STATUSES)
