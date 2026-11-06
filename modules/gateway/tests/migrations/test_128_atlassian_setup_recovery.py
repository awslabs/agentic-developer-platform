"""Migrated PostgreSQL setup outbox and lease fencing, not SQLite approximations."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.shared.identity.oauth_setup_store import (
    claim_due_setup,
    enqueue_setup,
    read_tenant_policy_observation,
    record_policy_observation,
    record_tenant_policy_observation,
    retire_setup,
    retry_setup,
)
from src.shared.models.oauth_setup import OAuthRecoveryJob, OAuthSetupState
from tests.migrations.conftest_postgres import run_alembic, to_async_url, upgrade

DIGEST = "a" * 64
NOW = datetime(2026, 10, 22, tzinfo=UTC)


@pytest.mark.asyncio
async def test_atlassian_setup_transactional_enqueue_and_leased_recovery(pg_url):
    upgrade(pg_url, "128_atlassian_setup_recovery")
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            await enqueue_setup(db, setup_key="abandoned", config_digest=DIGEST, registration_mode="retained-deployment-client", now=NOW)
            await db.rollback()
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(OAuthSetupState)) == 0
            assert await db.scalar(select(func.count()).select_from(OAuthRecoveryJob)) == 0

        async with sessions() as db:
            async with db.begin():
                job_id = await enqueue_setup(db, setup_key="mcp", config_digest=DIGEST, registration_mode="retained-deployment-client", now=NOW)
                assert (
                    await enqueue_setup(db, setup_key="mcp", config_digest=DIGEST, registration_mode="retained-deployment-client", now=NOW) == job_id
                )
                with pytest.raises(ValueError, match="differs"):
                    await enqueue_setup(db, setup_key="mcp", config_digest="b" * 64, registration_mode="retained-deployment-client", now=NOW)
        async with sessions() as first, sessions() as contender:
            async with first.begin():
                assert await claim_due_setup(first, owner="first", now=NOW) == job_id
                async with contender.begin():
                    assert await claim_due_setup(contender, owner="other", now=NOW) is None
                assert not await retire_setup(first, job_id=job_id, owner="other", now=NOW)
                assert await retry_setup(first, job_id=job_id, owner="first", now=NOW, category="persistence")
        async with sessions() as db:
            async with db.begin():
                assert await claim_due_setup(db, owner="early", now=NOW + timedelta(seconds=59)) is None
                assert await claim_due_setup(db, owner="second", now=NOW + timedelta(seconds=60)) == job_id
                assert not await retire_setup(db, job_id=job_id, owner="first", now=NOW + timedelta(seconds=60))
        async with sessions() as db:
            async with db.begin():
                assert await claim_due_setup(db, owner="successor", now=NOW + timedelta(seconds=105)) == job_id
                assert not await retire_setup(db, job_id=job_id, owner="second", now=NOW + timedelta(seconds=105))
                assert await retire_setup(db, job_id=job_id, owner="successor", now=NOW + timedelta(seconds=105))
                state = await db.get(OAuthSetupState, "mcp")
                state.phase = "prepared"
                await record_policy_observation(db, setup_key="mcp", config_digest=DIGEST, result="blocked", evidence_digest="c" * 64, now=NOW)
                with pytest.raises(ValueError, match="does not match"):
                    await record_policy_observation(db, setup_key="mcp", config_digest="b" * 64, result="allowed", evidence_digest="d" * 64, now=NOW)
        async with sessions() as db:
            job = await db.get(OAuthRecoveryJob, job_id)
            assert (job.state, job.attempts, job.last_error_category, job.terminal_at) == ("done", 3, "persistence", NOW + timedelta(seconds=105))
            assert await db.scalar(select(func.count()).select_from(OAuthRecoveryJob)) == 1
            state = await db.get(OAuthSetupState, "mcp")
            assert (state.policy_result, state.policy_evidence_ref) == ("blocked", "c" * 64)
        async with sessions() as db:
            async with db.begin():
                await enqueue_setup(db, setup_key="unresolved", config_digest=DIGEST, registration_mode="retained-deployment-client", now=NOW)
    finally:
        await engine.dispose()
    downgrade = run_alembic(pg_url, "downgrade", "127_authoring_assignment")
    assert downgrade.returncode != 0
    assert "Atlassian setup and unresolved recovery evidence must survive rollback" in downgrade.stderr
    import psycopg2

    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('oauth_recovery_jobs'), to_regclass('oauth_setup_states')")
        assert cursor.fetchone() == ("oauth_recovery_jobs", "oauth_setup_states")
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchone() == ("128_atlassian_setup_recovery",)
        cursor.execute("SELECT state, count(*) FROM oauth_recovery_jobs GROUP BY state ORDER BY state")
        assert cursor.fetchall() == [("done", 1), ("pending", 1)]
        cursor.execute("SELECT deployment_key, config_digest FROM oauth_setup_states ORDER BY deployment_key")
        assert cursor.fetchall() == [("mcp", DIGEST), ("unresolved", DIGEST)]


@pytest.mark.asyncio
async def test_tenant_policy_readback_is_pinned_fresh_and_isolated(pg_url):
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            async with db.begin():
                await enqueue_setup(db, setup_key="atlassian-mcp", config_digest=DIGEST, registration_mode="retained-deployment-client", now=NOW)
                state = await db.get(OAuthSetupState, "atlassian-mcp")
                state.phase = "prepared"
                await record_tenant_policy_observation(
                    db,
                    setup_key="atlassian-mcp",
                    tenant_id="org-alpha",
                    config_digest=DIGEST,
                    result="blocked",
                    evidence_digest="c" * 64,
                    now=NOW,
                )
                await record_tenant_policy_observation(
                    db,
                    setup_key="atlassian-mcp",
                    tenant_id="org-beta",
                    config_digest=DIGEST,
                    result="allowed",
                    evidence_digest="d" * 64,
                    now=NOW,
                )
                with pytest.raises(ValueError, match="does not match"):
                    await record_tenant_policy_observation(
                        db,
                        setup_key="atlassian-mcp",
                        tenant_id="org-beta",
                        config_digest="b" * 64,
                        result="allowed",
                        evidence_digest="e" * 64,
                        now=NOW,
                    )
        async with sessions() as db:
            assert await read_tenant_policy_observation(db, setup_key="atlassian-mcp", tenant_id="org-alpha", now=NOW) == "blocked"
            assert await read_tenant_policy_observation(db, setup_key="atlassian-mcp", tenant_id="org-beta", now=NOW) == "allowed"
            assert await read_tenant_policy_observation(db, setup_key="atlassian-mcp", tenant_id="org-gamma", now=NOW) is None
            assert await read_tenant_policy_observation(db, setup_key="atlassian-mcp", tenant_id="org-beta", now=NOW + timedelta(minutes=6)) is None
        async with sessions() as db:
            async with db.begin():
                state = await db.get(OAuthSetupState, "atlassian-mcp")
                state.config_digest = "b" * 64
        async with sessions() as db:
            assert await read_tenant_policy_observation(db, setup_key="atlassian-mcp", tenant_id="org-beta", now=NOW) is None
    finally:
        await engine.dispose()
