"""Operator regressions against real PostgreSQL sessions, with no model calls.

Retained verbatim in intent from the operator review of the first pushed
checkpoint, which found two defects the SQLite in-memory harness could not
surface:

* an expired cache entry refilled from a **retained ORM identity-map row**, so a
  committed rollback stayed invisible past the hard TTL ceiling; and
* **releasing a nested savepoint** clearing the uncommitted marker for a still
  open outer transaction, letting a rolled-back ``report_only`` escape into the
  shared cache — which could turn enforcing into permissive execution even
  though the rollback never committed.

These need real PostgreSQL because transaction visibility is the property under
test.  A skip is not a pass: see ``tests/migrations/README-postgres.md``.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agentauth import runtime_posture as runtime
from src.shared.models.persona_models import PersonaModelPolicySetting

# ``pg_server``/``pg_url`` are fixtures, re-exported so this module can request
# them; ruff sees the parameter of the same name below as a redefinition.
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

CLASS = "claude-agent-sdk"
NOW = datetime(2026, 9, 19, 17, 10, tzinfo=UTC)


@pytest.fixture
async def posture_sessions(pg_url):  # noqa: F811
    runtime.reset_posture_cache()
    engine = create_async_engine(to_async_url(pg_url))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(PersonaModelPolicySetting.__table__.create)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as seed:
            seed.add(
                PersonaModelPolicySetting(
                    compatibility_class=CLASS,
                    enforcement_posture="enforcing",
                    posture_revision=10,
                    revision=1,
                )
            )
            await seed.commit()
        yield factory
    finally:
        runtime.reset_posture_cache()
        await engine.dispose()


@pytest.mark.integration
async def test_committed_rollback_refreshes_retained_orm_row_after_cache_expiry(posture_sessions):
    """An expired entry must be refilled from fresh committed state, not a stale row."""
    async with posture_sessions() as reader:
        retained = await reader.scalar(select(PersonaModelPolicySetting))
        assert retained.enforcement_posture == "enforcing"
        first = await runtime.read_live_posture(reader, compatibility_class=CLASS, now=NOW)
        assert first.posture == "enforcing"
        async with posture_sessions() as operator:
            await operator.execute(update(PersonaModelPolicySetting).values(enforcement_posture="report_only", posture_revision=11))
            await operator.commit()
        after = await runtime.read_live_posture(
            reader,
            compatibility_class=CLASS,
            now=NOW + timedelta(seconds=runtime.MAX_POSTURE_CACHE_TTL_SECONDS + 1),
        )
        async with posture_sessions() as independent:
            database = await independent.scalar(select(PersonaModelPolicySetting))
            assert (database.enforcement_posture, database.posture_revision) == ("report_only", 11)
            cached = await runtime.read_live_posture(
                independent,
                compatibility_class=CLASS,
                now=NOW + timedelta(seconds=runtime.MAX_POSTURE_CACHE_TTL_SECONDS + 2),
            )
            assert (after.posture, after.posture_revision, cached.posture, cached.posture_revision) == (
                "report_only",
                11,
                "report_only",
                11,
            )


@pytest.mark.integration
async def test_savepoint_release_cannot_publish_rolled_back_outer_posture(posture_sessions):
    """A released savepoint must not publish a value the outer transaction discards."""
    async with posture_sessions() as writer:
        row = await writer.scalar(select(PersonaModelPolicySetting))
        row.enforcement_posture = "report_only"
        row.posture_revision = 11
        await writer.flush()
        assert writer.info.get(runtime.UNCOMMITTED_SESSION_FLAG)
        async with writer.begin_nested():
            pass
        await runtime.read_live_posture(writer, compatibility_class=CLASS, now=NOW)
        await writer.rollback()
    async with posture_sessions() as independent:
        database = await independent.scalar(select(PersonaModelPolicySetting))
        assert (database.enforcement_posture, database.posture_revision) == ("enforcing", 10)
        cached = await runtime.read_live_posture(independent, compatibility_class=CLASS, now=NOW)
        assert (cached.posture, cached.posture_revision) == ("enforcing", 10)


@pytest.mark.integration
async def test_uncommitted_change_is_never_published_to_another_session(posture_sessions):
    """The same property without a savepoint, as a direct control.

    The writer's *own* flushed-but-uncommitted change must not become the live
    posture even for the writer itself: the decision describes the platform's
    committed state, and a value that can still roll back is not that.  Because
    the read happens on an independent connection, the writer sees the committed
    ``enforcing`` value here — which is why the observation is legitimately
    cacheable, unlike the single-shared-connection harness case.
    """
    async with posture_sessions() as writer:
        row = await writer.scalar(select(PersonaModelPolicySetting))
        row.enforcement_posture = "report_only"
        row.posture_revision = 11
        await writer.flush()
        observed = await runtime.read_live_posture(writer, compatibility_class=CLASS, now=NOW)
        assert (observed.posture, observed.posture_revision) == ("enforcing", 10)
        await writer.rollback()
    async with posture_sessions() as independent:
        cached = await runtime.read_live_posture(independent, compatibility_class=CLASS, now=NOW)
        assert (cached.posture, cached.posture_revision) == ("enforcing", 10)


@pytest.mark.integration
async def test_an_uncommitted_insert_is_refused_when_no_row_is_committed(posture_sessions):
    """Absent committed authority is a refusal, not the caller's own pending insert.

    The operator's remaining defect at the previous checkpoint: with the committed
    row deleted, a session that inserts its own ``report_only`` row and flushes got
    that value back as live policy.  Keeping it out of the shared cache was not
    sufficient — the return value itself was treated as authority by the caller.

    Real PostgreSQL matters here because the property is transaction visibility:
    the reader has to distinguish "no committed row exists" from "I cannot check",
    and on a single-shared-connection harness those are the same observation.
    """
    async with posture_sessions() as administrator:
        await administrator.execute(delete(PersonaModelPolicySetting))
        await administrator.commit()
    runtime.reset_posture_cache()

    async with posture_sessions() as caller:
        caller.add(
            PersonaModelPolicySetting(
                compatibility_class=CLASS,
                enforcement_posture="report_only",
                posture_revision=1,
                revision=1,
            )
        )
        await caller.flush()
        with pytest.raises(runtime.RuntimePostureError, match="runtime_posture_unavailable"):
            await runtime.read_live_posture(caller, compatibility_class=CLASS, now=NOW)
        await caller.rollback()

    # And nothing was published: a later reader still finds no posture.
    async with posture_sessions() as reader:
        with pytest.raises(runtime.RuntimePostureError, match="runtime_posture_unavailable"):
            await runtime.read_live_posture(reader, compatibility_class=CLASS, now=NOW)


@pytest.mark.integration
async def test_audited_change_is_visible_to_a_new_session_within_the_bound(posture_sessions):
    """The positive case: a committed change does propagate, and is cacheable."""
    async with posture_sessions() as operator:
        await operator.execute(update(PersonaModelPolicySetting).values(enforcement_posture="report_only", posture_revision=11))
        await operator.commit()
    async with posture_sessions() as reader:
        observed = await runtime.read_live_posture(reader, compatibility_class=CLASS, now=NOW)
        assert (observed.posture, observed.posture_revision) == ("report_only", 11)
        assert observed.expires_at > observed.observed_at, "committed state must be cacheable"
