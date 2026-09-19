"""PMM-07 audited posture mutation: compare-and-set, audit atomicity, rollback.

Canonical §4.2/§9 require the posture to change only through a platform-admin,
versioned, audited operation, and require operational rollback to *be* that
operation.  A test that edits an ORM row directly can prove the live read works,
but it cannot establish the audited operation exists — so everything here goes
through :mod:`src.admin.persona_models.posture_service`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin.persona_models.posture_service import (
    PLATFORM_AUDIT_ORG,
    POSTURE_CHANGED_EVENT,
    POSTURE_REJECTED_EVENT,
    PostureConflictError,
    PostureMutationError,
    finalize_posture_commit,
    get_posture_setting,
    set_runtime_posture,
    write_posture_refusal_audit,
)
from src.agentauth.runtime_posture import (
    DEFAULT_POSTURE_CACHE_TTL_SECONDS,
    read_live_posture,
    reset_posture_cache,
)
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base
from src.shared.models.persona_models import PersonaModelPolicySetting

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
CLASS = "claude-agent-sdk"
ACTOR = "user-platform-admin"


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_posture_cache()
    yield
    reset_posture_cache()


@pytest.fixture
async def concurrent_engine(tmp_path):
    """A file-backed engine whose sessions get genuinely separate connections.

    The shared ``db_session`` fixtures use ``StaticPool`` over ``:memory:``, so
    two "concurrent" sessions there are one connection and one transaction and
    no interleaving can be observed.  Any test asserting a real compare-and-set
    race must use this fixture.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'posture.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def test_engine(concurrent_engine):
    # Posture reads require another physical connection even for sequential tests.
    yield concurrent_engine


@pytest.fixture
async def db_engine(concurrent_engine):
    # The admin test hierarchy uses db_engine for its request sessions.
    yield concurrent_engine


async def _seed(session, *, posture: str = "report_only", revision: int = 1):
    session.add(
        PersonaModelPolicySetting(
            compatibility_class=CLASS,
            revision=1,
            posture_revision=revision,
            enforcement_posture=posture,
        )
    )
    await session.commit()


async def _audit_rows(session, event_type: str) -> list[AuditLog]:
    return list(await session.scalars(select(AuditLog).where(AuditLog.event_type == event_type)))


class TestSupportedMutationPath:
    async def test_changes_posture_and_bumps_the_revision(self, db_session):
        await _seed(db_session, posture="report_only", revision=1)
        row = await set_runtime_posture(
            db_session,
            compatibility_class=CLASS,
            posture="enforcing",
            expected_revision=1,
            actor_id=ACTOR,
        )
        await db_session.commit()
        finalize_posture_commit(db_session)

        assert row.enforcement_posture == "enforcing"
        assert row.posture_revision == 2
        assert row.updated_by == ACTOR

    async def test_revision_is_monotonic_across_successive_changes(self, db_session):
        await _seed(db_session, posture="report_only", revision=1)
        revisions = []
        for target, expected in (("enforcing", 1), ("report_only", 2), ("disabled", 3)):
            row = await set_runtime_posture(
                db_session,
                compatibility_class=CLASS,
                posture=target,
                expected_revision=expected,
                actor_id=ACTOR,
            )
            await db_session.commit()
            finalize_posture_commit(db_session)
            revisions.append(row.posture_revision)
        assert revisions == [2, 3, 4]

    async def test_the_change_is_what_the_live_read_then_observes(self, db_session):
        """The mutation and the resolver's read path must agree."""
        await _seed(db_session, posture="report_only", revision=1)
        await set_runtime_posture(
            db_session,
            compatibility_class=CLASS,
            posture="enforcing",
            expected_revision=1,
            actor_id=ACTOR,
        )
        await db_session.commit()
        finalize_posture_commit(db_session)

        observed = await read_live_posture(db_session, compatibility_class=CLASS, now=NOW)
        assert (observed.posture, observed.posture_revision) == ("enforcing", 2)


class TestAuditIsAtomicAndDurable:
    async def test_success_writes_one_audit_row_with_before_and_after(self, db_session):
        await _seed(db_session, posture="report_only", revision=1)
        await set_runtime_posture(
            db_session,
            compatibility_class=CLASS,
            posture="enforcing",
            expected_revision=1,
            actor_id=ACTOR,
            reason="PMM-09 staged flip rehearsal",
        )
        await db_session.commit()

        rows = await _audit_rows(db_session, POSTURE_CHANGED_EVENT)
        assert len(rows) == 1
        details = rows[0].details
        assert details["before_posture"] == "report_only"
        assert details["after_posture"] == "enforcing"
        assert details["before_posture_revision"] == 1
        assert details["after_posture_revision"] == 2
        assert details["actor_kind"] == "platform_admin"
        assert details["subject_key"] == CLASS
        assert details["change_reason"] == "PMM-09 staged flip rehearsal"
        assert rows[0].actor_id == ACTOR
        assert rows[0].org_id == PLATFORM_AUDIT_ORG

    async def test_audit_row_is_committed_not_merely_flushed(self, db_session_factory):
        """Verified through a separate session, so it proves durability."""
        async with db_session_factory() as setup:
            await _seed(setup, posture="report_only", revision=1)
        async with db_session_factory() as writer:
            await set_runtime_posture(
                writer,
                compatibility_class=CLASS,
                posture="enforcing",
                expected_revision=1,
                actor_id=ACTOR,
            )
            await writer.commit()
            finalize_posture_commit(writer)
        async with db_session_factory() as reader:
            rows = await _audit_rows(reader, POSTURE_CHANGED_EVENT)
            assert len(rows) == 1
            setting = await reader.get(PersonaModelPolicySetting, CLASS)
            assert setting.enforcement_posture == "enforcing"

    async def test_an_unaudited_change_does_not_happen(self, db_session_factory):
        """Rolling back discards the posture change and its audit row together."""
        async with db_session_factory() as setup:
            await _seed(setup, posture="report_only", revision=1)
        async with db_session_factory() as writer:
            await set_runtime_posture(
                writer,
                compatibility_class=CLASS,
                posture="enforcing",
                expected_revision=1,
                actor_id=ACTOR,
            )
            await writer.rollback()
        async with db_session_factory() as reader:
            assert await _audit_rows(reader, POSTURE_CHANGED_EVENT) == []
            setting = await reader.get(PersonaModelPolicySetting, CLASS)
            assert setting.enforcement_posture == "report_only"
            assert setting.posture_revision == 1

    async def test_refusal_is_audited_on_its_own_transaction(self, db_session_factory):
        async with db_session_factory() as setup:
            await _seed(setup, posture="report_only", revision=1)
        async with db_session_factory() as writer:
            await write_posture_refusal_audit(
                writer,
                compatibility_class=CLASS,
                requested_posture="enforcing",
                reason="posture_revision_conflict",
                actor_id=ACTOR,
            )
        async with db_session_factory() as reader:
            rows = await _audit_rows(reader, POSTURE_REJECTED_EVENT)
            assert len(rows) == 1
            assert rows[0].details["reason"] == "posture_revision_conflict"
            assert rows[0].details["requested_posture"] == "enforcing"
            # The refusal changed nothing.
            setting = await reader.get(PersonaModelPolicySetting, CLASS)
            assert setting.enforcement_posture == "report_only"


class TestStaleAndMalformedRefusal:
    async def test_stale_expected_revision_is_refused(self, db_session):
        await _seed(db_session, posture="report_only", revision=4)
        with pytest.raises(PostureConflictError) as err:
            await set_runtime_posture(
                db_session,
                compatibility_class=CLASS,
                posture="enforcing",
                expected_revision=2,
                actor_id=ACTOR,
            )
        assert err.value.reason == "posture_revision_conflict"
        assert err.value.current_revision == 4
        assert err.value.current_posture == "report_only"

    async def test_a_refused_change_leaves_no_trace(self, db_session):
        await _seed(db_session, posture="report_only", revision=4)
        with pytest.raises(PostureConflictError):
            await set_runtime_posture(
                db_session,
                compatibility_class=CLASS,
                posture="enforcing",
                expected_revision=2,
                actor_id=ACTOR,
            )
        setting = await db_session.get(PersonaModelPolicySetting, CLASS)
        assert (setting.enforcement_posture, setting.posture_revision) == ("report_only", 4)
        assert await _audit_rows(db_session, POSTURE_CHANGED_EVENT) == []

    @pytest.mark.parametrize("bad", ["", "enforce", "REPORT_ONLY", "report-only", "mandatory", None, 1, True])
    async def test_unsupported_posture_is_refused(self, db_session, bad):
        await _seed(db_session, posture="report_only", revision=1)
        with pytest.raises(PostureMutationError) as err:
            await set_runtime_posture(
                db_session,
                compatibility_class=CLASS,
                posture=bad,
                expected_revision=1,
                actor_id=ACTOR,
            )
        assert err.value.reason == "runtime_posture_unsupported"

    @pytest.mark.parametrize("bad", [0, -1, None, "1", 1.0, True])
    async def test_malformed_expected_revision_is_refused(self, db_session, bad):
        await _seed(db_session, posture="report_only", revision=1)
        with pytest.raises(PostureMutationError) as err:
            await set_runtime_posture(
                db_session,
                compatibility_class=CLASS,
                posture="enforcing",
                expected_revision=bad,
                actor_id=ACTOR,
            )
        assert err.value.reason == "posture_revision_unsupported"

    async def test_unknown_compatibility_class_is_refused(self, db_session):
        with pytest.raises(PostureMutationError) as err:
            await set_runtime_posture(
                db_session,
                compatibility_class="not-a-class",
                posture="enforcing",
                expected_revision=1,
                actor_id=ACTOR,
            )
        assert err.value.reason == "compatibility_class_unknown"

    async def test_unprovisioned_class_is_a_readiness_failure_not_an_insert(self, db_session):
        """A posture change must not invent a class migrations never provisioned."""
        with pytest.raises(PostureMutationError) as err:
            await set_runtime_posture(
                db_session,
                compatibility_class="codex-sdk",
                posture="enforcing",
                expected_revision=1,
                actor_id=ACTOR,
            )
        assert err.value.reason == "runtime_posture_unavailable"
        assert await db_session.get(PersonaModelPolicySetting, "codex-sdk") is None

    async def test_get_setting_refuses_unknown_class(self, db_session):
        with pytest.raises(PostureMutationError) as err:
            await get_posture_setting(db_session, compatibility_class="../claude-agent-sdk")
        assert err.value.reason == "compatibility_class_unknown"


class TestPendingChangeIsNeverLive:
    async def test_uncommitted_change_is_not_cached_for_other_sessions(self, db_session_factory):
        """A change that may still roll back must not become a live decision."""
        async with db_session_factory() as setup:
            await _seed(setup, posture="report_only", revision=1)
        reset_posture_cache()

        async with db_session_factory() as writer:
            await set_runtime_posture(
                writer,
                compatibility_class=CLASS,
                posture="enforcing",
                expected_revision=1,
                actor_id=ACTOR,
            )
            # The independent connection sees only the committed report-only
            # setting, never this writer's pending enforcing change.
            observed = await read_live_posture(writer, compatibility_class=CLASS, now=NOW)
            assert (observed.posture, observed.posture_revision) == ("report_only", 1)
            await writer.rollback()

        async with db_session_factory() as reader:
            observed = await read_live_posture(reader, compatibility_class=CLASS, now=NOW)
            assert observed.posture == "report_only"
            assert observed.posture_revision == 1

    async def test_rolled_back_enforcing_never_enforces(self, db_session_factory):
        async with db_session_factory() as setup:
            await _seed(setup, posture="report_only", revision=1)
        reset_posture_cache()
        async with db_session_factory() as writer:
            await set_runtime_posture(
                writer,
                compatibility_class=CLASS,
                posture="enforcing",
                expected_revision=1,
                actor_id=ACTOR,
            )
            await writer.rollback()
        async with db_session_factory() as reader:
            assert (await read_live_posture(reader, compatibility_class=CLASS, now=NOW)).enforcing is False


class TestAuditedRollbackRoundTrip:
    async def test_report_only_to_enforcing_to_report_only_through_the_operation(self, db_session_factory):
        """§9 operational rollback, on a fake clock, via the supported path only.

        One unchanged snapshot is not involved here: this proves the *setting*
        round-trips through the audited operation and that each step is visible
        to a separate reader within the measured bound.
        """
        step = timedelta(seconds=DEFAULT_POSTURE_CACHE_TTL_SECONDS)
        async with db_session_factory() as setup:
            await _seed(setup, posture="report_only", revision=1)
        reset_posture_cache()

        async with db_session_factory() as reader:
            assert (await read_live_posture(reader, compatibility_class=CLASS, now=NOW)).posture == "report_only"

        async with db_session_factory() as writer:
            await set_runtime_posture(
                writer,
                compatibility_class=CLASS,
                posture="enforcing",
                expected_revision=1,
                actor_id=ACTOR,
                reason="staged flip",
            )
            await writer.commit()

        async with db_session_factory() as reader:
            observed = await read_live_posture(reader, compatibility_class=CLASS, now=NOW + step)
            assert (observed.posture, observed.posture_revision) == ("enforcing", 2)

        # Audited operational rollback — same operation, not an ad-hoc edit.
        async with db_session_factory() as writer:
            await set_runtime_posture(
                writer,
                compatibility_class=CLASS,
                posture="report_only",
                expected_revision=2,
                actor_id=ACTOR,
                reason="operational rollback",
            )
            await writer.commit()

        async with db_session_factory() as reader:
            observed = await read_live_posture(reader, compatibility_class=CLASS, now=NOW + 2 * step)
            assert (observed.posture, observed.posture_revision) == ("report_only", 3)
            assert observed.enforcing is False

        # Both transitions are on the durable audit trail, in order.
        async with db_session_factory() as reader:
            rows = sorted(
                await _audit_rows(reader, POSTURE_CHANGED_EVENT),
                key=lambda row: row.details["after_posture_revision"],
            )
            assert [row.details["after_posture"] for row in rows] == ["enforcing", "report_only"]
            assert [row.details["change_reason"] for row in rows] == ["staged flip", "operational rollback"]


class TestConcurrentWriters:
    async def test_two_administrators_one_winner(self, concurrent_engine):
        """Real interleaving on a file-backed DB: the loser is refused, not lost."""
        factory = async_sessionmaker(concurrent_engine, expire_on_commit=False)
        async with factory() as setup:
            await _seed(setup, posture="report_only", revision=1)
        reset_posture_cache()

        async with factory() as first, factory() as second:
            # Both read revision 1, then both attempt the compare-and-set.
            await set_runtime_posture(
                first,
                compatibility_class=CLASS,
                posture="enforcing",
                expected_revision=1,
                actor_id="admin-one",
            )
            await first.commit()

            with pytest.raises(PostureConflictError) as err:
                await set_runtime_posture(
                    second,
                    compatibility_class=CLASS,
                    posture="disabled",
                    expected_revision=1,
                    actor_id="admin-two",
                )
            assert err.value.reason == "posture_revision_conflict"
            assert err.value.current_revision == 2

        async with factory() as reader:
            setting = await reader.get(PersonaModelPolicySetting, CLASS)
            assert setting.enforcement_posture == "enforcing"
            assert setting.posture_revision == 2
            assert setting.updated_by == "admin-one"
            rows = await _audit_rows(reader, POSTURE_CHANGED_EVENT)
            assert len(rows) == 1
