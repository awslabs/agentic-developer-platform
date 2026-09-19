"""Operator regressions against real PostgreSQL sessions, with no model calls."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.agentauth import runtime_posture as runtime
from src.shared.models.persona_models import PersonaModelPolicySetting
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

CLASS = "claude-agent-sdk"
NOW = datetime(2026, 9, 19, 17, 10, tzinfo=UTC)

@pytest.fixture
async def posture_sessions(pg_url):
    runtime.reset_posture_cache()
    engine = create_async_engine(to_async_url(pg_url))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(PersonaModelPolicySetting.__table__.create)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as seed:
            seed.add(PersonaModelPolicySetting(compatibility_class=CLASS, enforcement_posture="enforcing", posture_revision=10, revision=1))
            await seed.commit()
        yield factory
    finally:
        runtime.reset_posture_cache()
        await engine.dispose()

@pytest.mark.integration
async def test_committed_rollback_refreshes_retained_orm_row_after_cache_expiry(posture_sessions):
    async with posture_sessions() as reader:
        retained = await reader.scalar(select(PersonaModelPolicySetting))
        assert retained.enforcement_posture == "enforcing"
        first = await runtime.read_live_posture(reader, compatibility_class=CLASS, now=NOW)
        assert first.posture == "enforcing"
        async with posture_sessions() as operator:
            await operator.execute(update(PersonaModelPolicySetting).values(enforcement_posture="report_only", posture_revision=11))
            await operator.commit()
        after = await runtime.read_live_posture(reader, compatibility_class=CLASS, now=NOW + timedelta(seconds=runtime.MAX_POSTURE_CACHE_TTL_SECONDS + 1))
        async with posture_sessions() as independent:
            database = await independent.scalar(select(PersonaModelPolicySetting))
            assert (database.enforcement_posture, database.posture_revision) == ("report_only", 11)
            cached = await runtime.read_live_posture(independent, compatibility_class=CLASS, now=NOW + timedelta(seconds=runtime.MAX_POSTURE_CACHE_TTL_SECONDS + 2))
            assert (after.posture, after.posture_revision, cached.posture, cached.posture_revision) == ("report_only", 11, "report_only", 11)

@pytest.mark.integration
async def test_savepoint_release_cannot_publish_rolled_back_outer_posture(posture_sessions):
    async with posture_sessions() as writer:
        row = await writer.scalar(select(PersonaModelPolicySetting))
        row.enforcement_posture = "report_only"
        row.posture_revision = 11
        await writer.flush()
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
async def test_uncommitted_insert_is_not_a_live_posture_when_committed_setting_is_absent(posture_sessions):
    async with posture_sessions() as operator:
        await operator.execute(delete(PersonaModelPolicySetting))
        await operator.commit()
    async with posture_sessions() as writer:
        writer.add(PersonaModelPolicySetting(compatibility_class=CLASS, enforcement_posture="report_only", posture_revision=1, revision=1))
        await writer.flush()
        # A row visible only inside this pending transaction is not live policy.
        # Not caching it is insufficient: callers use the returned posture to sign.
        with pytest.raises(runtime.RuntimePostureError):
            await runtime.read_live_posture(writer, compatibility_class=CLASS, now=NOW)
        await writer.rollback()


@pytest.mark.integration
async def test_connection_bound_reader_does_not_treat_outer_transaction_as_committed(posture_sessions):
    engine = posture_sessions.kw["bind"]
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(update(PersonaModelPolicySetting).values(enforcement_posture="report_only", posture_revision=11))
        async with AsyncSession(bind=connection) as reader:
            try:
                observation = await runtime.read_live_posture(reader, compatibility_class=CLASS, now=NOW)
            except runtime.RuntimePostureError:
                pass  # Refusing an unprovable read is safe.
            else:
                assert (observation.posture, observation.posture_revision) == ("enforcing", 10)
        if transaction.is_active:
            await transaction.rollback()
    async with posture_sessions() as independent:
        observed = await runtime.read_live_posture(independent, compatibility_class=CLASS, now=NOW)
        assert (observed.posture, observed.posture_revision) == ("enforcing", 10)
