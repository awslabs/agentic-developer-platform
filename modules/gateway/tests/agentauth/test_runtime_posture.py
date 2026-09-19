"""PMM-07 live versioned runtime posture: bounded staleness and fail-closed reads.

The posture is the audited operational control.  These tests pin the two
properties the rollback guarantee rests on: the value is re-read per hop (never
inherited from a frozen snapshot), and a stale observation expires by elapsed
time so the bound holds across separate gateway instances rather than depending
on a local invalidation call one replica would never see.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError

from src.agentauth.runtime_posture import (
    DEFAULT_POSTURE_CACHE_TTL_SECONDS,
    MAX_POSTURE_CACHE_TTL_SECONDS,
    POSTURE_CACHE_TTL_ENV,
    RuntimePostureError,
    coerce_posture,
    coerce_posture_revision,
    measured_cache_ttl_seconds,
    read_live_posture,
    reset_posture_cache,
)
from src.shared.models.persona_models import PersonaModelPolicySetting

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
CLASS = "claude-agent-sdk"


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_posture_cache()
    yield
    reset_posture_cache()


async def _seed(session, *, posture: str = "report_only", revision: int = 2, compatibility_class: str = CLASS):
    row = PersonaModelPolicySetting(
        compatibility_class=compatibility_class,
        harness_contract_revision="0.3.220",
        active_default_model_id="global.anthropic.claude-sonnet-4-6",
        revision=7,
        posture_revision=revision,
        enforcement_posture=posture,
    )
    session.add(row)
    await session.commit()
    return row


async def _set_posture(session, *, posture: str, revision: int, compatibility_class: str = CLASS):
    row = await session.get(PersonaModelPolicySetting, compatibility_class)
    row.enforcement_posture = posture
    row.posture_revision = revision
    await session.commit()


class TestClosedVocabulary:
    @pytest.mark.parametrize("value", ["disabled", "report_only", "enforcing"])
    def test_accepts_only_the_three_known_postures(self, value):
        assert coerce_posture(value) == value

    @pytest.mark.parametrize(
        "value",
        ["", "REPORT_ONLY", "report-only", "enforce", "enforcing ", None, 1, True, ["enforcing"]],
    )
    def test_unknown_posture_is_refused_not_guessed(self, value):
        """An unrecognised posture must not be coerced to the permissive value."""
        with pytest.raises(RuntimePostureError) as err:
            coerce_posture(value)
        assert err.value.reason == "runtime_posture_unsupported"

    @pytest.mark.parametrize("value", [1, 2, 99])
    def test_accepts_monotonic_revisions(self, value):
        assert coerce_posture_revision(value) == value

    @pytest.mark.parametrize("value", [0, -1, None, "2", 2.0, True, False])
    def test_malformed_revision_is_refused(self, value):
        """``True`` is an int subclass; accepting it would let junk look valid."""
        with pytest.raises(RuntimePostureError) as err:
            coerce_posture_revision(value)
        assert err.value.reason == "posture_revision_unsupported"


class TestMeasuredBound:
    def test_default_is_bounded(self):
        assert measured_cache_ttl_seconds() == DEFAULT_POSTURE_CACHE_TTL_SECONDS
        assert DEFAULT_POSTURE_CACHE_TTL_SECONDS <= MAX_POSTURE_CACHE_TTL_SECONDS

    def test_configured_value_cannot_exceed_the_ceiling(self, monkeypatch):
        monkeypatch.setenv(POSTURE_CACHE_TTL_ENV, "100000")
        assert measured_cache_ttl_seconds() == MAX_POSTURE_CACHE_TTL_SECONDS

    @pytest.mark.parametrize("raw", ["not-a-number", "", "12.5"])
    def test_malformed_configuration_falls_back_to_the_default_bound(self, monkeypatch, raw):
        monkeypatch.setenv(POSTURE_CACHE_TTL_ENV, raw)
        assert measured_cache_ttl_seconds() == DEFAULT_POSTURE_CACHE_TTL_SECONDS

    def test_negative_configuration_disables_caching_rather_than_extending_it(self, monkeypatch):
        monkeypatch.setenv(POSTURE_CACHE_TTL_ENV, "-5")
        assert measured_cache_ttl_seconds() == 0


class TestLiveRead:
    async def test_reads_the_committed_posture_and_revision(self, db_session):
        await _seed(db_session, posture="report_only", revision=3)
        observed = await read_live_posture(db_session, compatibility_class=CLASS, now=NOW)
        assert (observed.posture, observed.posture_revision) == ("report_only", 3)
        assert observed.source == "live"
        assert observed.enforcing is False
        assert observed.expires_at == NOW + timedelta(seconds=DEFAULT_POSTURE_CACHE_TTL_SECONDS)

    async def test_missing_row_is_unavailable_not_report_only(self, db_session):
        """A missing setting is a platform-readiness failure, not a posture."""
        with pytest.raises(RuntimePostureError) as err:
            await read_live_posture(db_session, compatibility_class=CLASS, now=NOW)
        assert err.value.reason == "runtime_posture_unavailable"

    async def test_database_rejects_an_unknown_posture_outright(self, db_session):
        """First layer: the CHECK constraint refuses to store an unknown value."""
        await _seed(db_session)
        with pytest.raises(Exception) as err:
            await db_session.execute(
                PersonaModelPolicySetting.__table__.update()
                .where(PersonaModelPolicySetting.compatibility_class == CLASS)
                .values(enforcement_posture="quarantined")
            )
            await db_session.commit()
        assert "ck_pmps_enforcement_posture" in str(err.value)
        await db_session.rollback()

    async def test_unknown_posture_from_the_read_path_fails_closed(self, db_session, monkeypatch):
        """Second layer: a value that reached the row anyway is still refused.

        The CHECK constraint is the first defence, but the resolver must not
        depend on it — a future schema revision, a replica lagging a constraint
        change, or a non-PostgreSQL path could surface an unrecognised posture.
        It must fail closed rather than be relabelled as the permissive value.
        """
        await _seed(db_session)
        reset_posture_cache()
        rogue = SimpleNamespace(enforcement_posture="quarantined", posture_revision=3)
        monkeypatch.setattr(db_session, "scalar", AsyncMock(return_value=rogue))
        with pytest.raises(RuntimePostureError) as err:
            await read_live_posture(db_session, compatibility_class=CLASS, now=NOW)
        assert err.value.reason == "runtime_posture_unsupported"

    async def test_malformed_stored_revision_fails_closed(self, db_session, monkeypatch):
        await _seed(db_session)
        reset_posture_cache()
        rogue = SimpleNamespace(enforcement_posture="enforcing", posture_revision=0)
        monkeypatch.setattr(db_session, "scalar", AsyncMock(return_value=rogue))
        with pytest.raises(RuntimePostureError) as err:
            await read_live_posture(db_session, compatibility_class=CLASS, now=NOW)
        assert err.value.reason == "posture_revision_unsupported"

    async def test_an_unreadable_setting_is_never_relabelled_report_only(self, db_session, monkeypatch):
        """A database outage on the posture read must propagate, not soften."""
        await _seed(db_session)
        reset_posture_cache()
        monkeypatch.setattr(db_session, "scalar", AsyncMock(side_effect=OperationalError("SELECT", {}, Exception("down"))))
        with pytest.raises(OperationalError):
            await read_live_posture(db_session, compatibility_class=CLASS, now=NOW)

    async def test_unknown_compatibility_class_is_refused(self, db_session):
        with pytest.raises(RuntimePostureError) as err:
            await read_live_posture(db_session, compatibility_class="", now=NOW)
        assert err.value.reason == "compatibility_class_unknown"

    async def test_each_class_is_tracked_separately(self, db_session):
        await _seed(db_session, posture="report_only", revision=2, compatibility_class=CLASS)
        await _seed(db_session, posture="disabled", revision=5, compatibility_class="codex-sdk")
        claude = await read_live_posture(db_session, compatibility_class=CLASS, now=NOW)
        codex = await read_live_posture(db_session, compatibility_class="codex-sdk", now=NOW)
        assert claude.posture == "report_only"
        assert codex.posture == "disabled"
        assert codex.posture_revision == 5


class TestBoundedStaleness:
    async def test_a_change_is_observed_once_the_measured_bound_elapses(self, db_session):
        await _seed(db_session, posture="report_only", revision=2)
        first = await read_live_posture(db_session, compatibility_class=CLASS, now=NOW)
        assert first.posture == "report_only"

        await _set_posture(db_session, posture="enforcing", revision=3)

        # Inside the bound the previous observation is still served, and it
        # honestly reports that it came from the cache.
        within = await read_live_posture(
            db_session,
            compatibility_class=CLASS,
            now=NOW + timedelta(seconds=DEFAULT_POSTURE_CACHE_TTL_SECONDS - 1),
        )
        assert (within.posture, within.source) == ("report_only", "cache")

        # At the bound the new value must be visible.
        after = await read_live_posture(
            db_session,
            compatibility_class=CLASS,
            now=NOW + timedelta(seconds=DEFAULT_POSTURE_CACHE_TTL_SECONDS),
        )
        assert (after.posture, after.posture_revision, after.source) == ("enforcing", 3, "live")

    async def test_rollback_to_report_only_takes_effect_within_the_bound(self, db_session):
        """report_only -> enforcing -> report_only, on a controlled clock."""
        await _seed(db_session, posture="report_only", revision=2)
        step = timedelta(seconds=DEFAULT_POSTURE_CACHE_TTL_SECONDS)

        assert (await read_live_posture(db_session, compatibility_class=CLASS, now=NOW)).posture == "report_only"

        await _set_posture(db_session, posture="enforcing", revision=3)
        enforcing = await read_live_posture(db_session, compatibility_class=CLASS, now=NOW + step)
        assert (enforcing.posture, enforcing.posture_revision) == ("enforcing", 3)

        # Audited operational rollback.
        await _set_posture(db_session, posture="report_only", revision=4)
        reverted = await read_live_posture(db_session, compatibility_class=CLASS, now=NOW + 2 * step)
        assert (reverted.posture, reverted.posture_revision) == ("report_only", 4)
        assert reverted.enforcing is False

    async def test_expiry_is_time_based_so_a_second_instance_also_reverts(self, db_session_factory):
        """No local invalidation call: a separate session must still revert.

        This is the cross-instance property.  The mutating session never calls
        into the reading session's process, so if expiry depended on
        invalidation the reader would enforce indefinitely.
        """
        async with db_session_factory() as writer:
            await _seed(writer, posture="enforcing", revision=3)

        async with db_session_factory() as reader:
            observed = await read_live_posture(reader, compatibility_class=CLASS, now=NOW)
            assert observed.posture == "enforcing"

        # A different instance performs the audited rollback.
        async with db_session_factory() as writer:
            await _set_posture(writer, posture="report_only", revision=4)

        # The reading instance gets no notification, only elapsed time.
        async with db_session_factory() as reader:
            still_cached = await read_live_posture(reader, compatibility_class=CLASS, now=NOW + timedelta(seconds=1))
            assert still_cached.posture == "enforcing"

            reverted = await read_live_posture(
                reader,
                compatibility_class=CLASS,
                now=NOW + timedelta(seconds=DEFAULT_POSTURE_CACHE_TTL_SECONDS),
            )
            assert (reverted.posture, reverted.posture_revision) == ("report_only", 4)

    async def test_zero_ttl_reads_through_every_time(self, db_session, monkeypatch):
        monkeypatch.setenv(POSTURE_CACHE_TTL_ENV, "0")
        await _seed(db_session, posture="report_only", revision=2)
        assert (await read_live_posture(db_session, compatibility_class=CLASS, now=NOW)).source == "live"
        await _set_posture(db_session, posture="enforcing", revision=3)
        immediate = await read_live_posture(db_session, compatibility_class=CLASS, now=NOW)
        assert (immediate.posture, immediate.source) == ("enforcing", "live")


class TestUncommittedChangesAreNeverCached:
    async def test_a_pending_posture_write_refuses_rather_than_reporting_itself(self, db_session):
        """A change that may still roll back is not a live posture at all.

        Stricter than merely keeping it out of the cache, and deliberately so: the
        posture describes committed platform state, so a session holding an
        uncommitted write to it cannot be told what the posture is — not even for
        one uncached hop.  Returning its own pending value, correctly labelled
        uncacheable, still let that value govern a signed decision.
        """
        await _seed(db_session, posture="report_only", revision=2)
        reset_posture_cache()

        row = await db_session.get(PersonaModelPolicySetting, CLASS)
        row.enforcement_posture = "enforcing"
        row.posture_revision = 3
        await db_session.flush()  # visible in-session, NOT committed

        with pytest.raises(RuntimePostureError, match="runtime_posture_unavailable"):
            await read_live_posture(db_session, compatibility_class=CLASS, now=NOW)

        await db_session.rollback()

        # And the committed value is readable again once the write is gone.
        after = await read_live_posture(db_session, compatibility_class=CLASS, now=NOW)
        assert (after.posture, after.posture_revision) == ("report_only", 2)

    async def test_rolled_back_change_is_invisible_to_other_sessions(self, db_session_factory):
        async with db_session_factory() as setup:
            await _seed(setup, posture="report_only", revision=2)
        reset_posture_cache()

        async with db_session_factory() as writer:
            row = await writer.get(PersonaModelPolicySetting, CLASS)
            row.enforcement_posture = "enforcing"
            row.posture_revision = 3
            await writer.flush()
            with pytest.raises(RuntimePostureError):
                await read_live_posture(writer, compatibility_class=CLASS, now=NOW)
            await writer.rollback()

        async with db_session_factory() as reader:
            observed = await read_live_posture(reader, compatibility_class=CLASS, now=NOW)
            assert observed.posture == "report_only"
            assert observed.posture_revision == 2

    async def test_an_uncommitted_insert_is_not_live_policy_when_no_row_is_committed(self, db_session):
        """The operator's remaining case: absent committed row, caller inserts one.

        Avoiding the cache was not enough — the reader still *returned* the
        caller's own uncommitted ``report_only`` as live policy.  A permissive
        value that the caller could still roll back is the worst possible thing to
        substitute here, because it is indistinguishable from an audited
        report-only posture at the point of consumption.
        """
        reset_posture_cache()
        db_session.add(
            PersonaModelPolicySetting(
                compatibility_class=CLASS,
                enforcement_posture="report_only",
                posture_revision=1,
                revision=1,
            )
        )
        await db_session.flush()

        with pytest.raises(RuntimePostureError, match="runtime_posture_unavailable"):
            await read_live_posture(db_session, compatibility_class=CLASS, now=NOW)
