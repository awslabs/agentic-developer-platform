"""Migration contract for PMM-08 nullable usage evidence (#5426)."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.persona_models import PersonaModelRetirementAlert
from src.shared.models.usage import UsageLog

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"
COLUMNS = {
    "pricing_confidence",
    "pricing_estimate_reasons",
    "pricing_decision",
    "model_decision",
    "model_decision_id",
    "approving_human_id",
    "destination_region",
    "provider_request_id",
    "persona_key",
    "compatibility_class",
    "harness_contract_revision",
    "root_invocation_id",
    "chain_id",
    "preference_owner_kind",
    "preference_owner_id",
    "model_policy_snapshot_digest",
    "model_policy_revision",
    "model_catalogue_revision",
    "requested_model_id",
    "resolved_model_id",
    "resolution_source",
    "runtime_posture",
    "posture_revision",
    "pricing_source_kind",
    "pricing_generation_id",
    "pricing_pointer_revision",
    "pricing_snapshot_version",
    "pricing_policy_version",
}
OUTBOX_COLUMNS = {
    "id",
    "org_id",
    "preference_id",
    "persona_key",
    "preference_owner_kind",
    "preference_owner_id",
    "canonical_model_id",
    "lifecycle_revision",
    "state",
    "claim_token",
    "lease_expires_at",
    "attempt_count",
    "last_error",
    "claimed_at",
    "delivered_at",
    "created_at",
    "updated_at",
}


def _load(filename="061_persona_usage_evidence.py"):
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location("migration_061", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


MIGRATION = _load()
INDEX_MIGRATION = _load("062_persona_usage_indexes.py")

_PRE_059 = """
CREATE TABLE usage_logs (
 id VARCHAR(255) PRIMARY KEY, org_id VARCHAR(255) NOT NULL, timestamp DATETIME,
 department_id VARCHAR(255) NOT NULL, team_id VARCHAR(255) NOT NULL,
 user_id VARCHAR(255) NOT NULL, account_type VARCHAR(20) NOT NULL,
 model VARCHAR(255) NOT NULL, input_tokens INTEGER NOT NULL,
 output_tokens INTEGER NOT NULL, cost_usd NUMERIC(10,6) NOT NULL,
 latency_ms INTEGER NOT NULL, status_code INTEGER NOT NULL,
 request_id VARCHAR(255), bedrock_account_id VARCHAR(12),
 agent_run_id VARCHAR(255), chat_log_s3_key VARCHAR(1024),
 cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,
 graph_address VARCHAR(512), client_tool VARCHAR(32)
)
"""


def _run(sync_conn, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with Operations.context(MigrationContext.configure(sync_conn)):
        fn()


async def _engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(sa.text(_PRE_059))
    return engine


async def _upgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run, MIGRATION.upgrade)
        await conn.run_sync(_run, INDEX_MIGRATION.upgrade)


@pytest.mark.asyncio
async def test_columns_are_nullable_without_defaults_and_match_the_model():
    engine = await _engine()
    try:
        await _upgrade(engine)
        async with engine.connect() as conn:
            columns = {row["name"]: row for row in await conn.run_sync(lambda c: sa_inspect(c).get_columns("usage_logs"))}
        assert COLUMNS <= columns.keys()
        assert all(columns[name]["nullable"] is True for name in COLUMNS)
        assert all(columns[name].get("default") is None for name in COLUMNS)
        assert COLUMNS <= set(UsageLog.__table__.columns.keys())
        assert {"ix_usage_persona_owner", "ix_usage_chain_id"} <= {index.name for index in UsageLog.__table__.indexes}
        async with engine.connect() as conn:
            outbox = {row["name"]: row for row in await conn.run_sync(lambda c: sa_inspect(c).get_columns("persona_model_retirement_alerts"))}
        assert set(outbox) == OUTBOX_COLUMNS
        assert set(PersonaModelRetirementAlert.__table__.columns.keys()) == OUTBOX_COLUMNS
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_never_backfills_or_mutates_existing_usage():
    engine = await _engine()
    try:
        before = {
            "id": "old",
            "org_id": "tenant",
            "department_id": "dept",
            "team_id": "team",
            "user_id": "user",
            "account_type": "human",
            "model": "model",
            "input_tokens": 2,
            "output_tokens": 3,
            "cost_usd": 0.25,
            "latency_ms": 5,
            "status_code": 200,
        }
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO usage_logs "
                    "(id, org_id, department_id, team_id, user_id, account_type, model, "
                    "input_tokens, output_tokens, cost_usd, latency_ms, status_code) "
                    "VALUES (:id, :org_id, :department_id, :team_id, :user_id, :account_type, "
                    ":model, :input_tokens, :output_tokens, :cost_usd, :latency_ms, :status_code)"
                ),
                before,
            )
        await _upgrade(engine)
        async with engine.connect() as conn:
            row = (await conn.execute(sa.text("SELECT * FROM usage_logs WHERE id='old'"))).mappings().one()
        assert all(row[name] is None for name in COLUMNS)
        assert {name: row[name] for name in before} == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_partial_query_indexes_and_downgrade():
    engine = await _engine()
    try:
        await _upgrade(engine)
        async with engine.connect() as conn:
            indexes = {row["name"]: row for row in await conn.run_sync(lambda c: sa_inspect(c).get_indexes("usage_logs"))}
        assert indexes["ix_usage_persona_owner"]["column_names"] == [
            "org_id",
            "preference_owner_kind",
            "preference_owner_id",
            "persona_key",
        ]
        assert indexes["ix_usage_chain_id"]["column_names"] == ["org_id", "chain_id"]
        async with engine.begin() as conn:
            await conn.run_sync(_run, INDEX_MIGRATION.downgrade)
            await conn.run_sync(_run, MIGRATION.downgrade)
        async with engine.connect() as conn:
            columns = {row["name"] for row in await conn.run_sync(lambda c: sa_inspect(c).get_columns("usage_logs"))}
            tables = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        assert COLUMNS.isdisjoint(columns)
        assert "persona_model_retirement_alerts" not in tables
    finally:
        await engine.dispose()


def test_revision_chains_to_live_pmm03_head():
    assert MIGRATION.revision == "061_persona_usage_evidence"
    assert MIGRATION.down_revision == "060_orch_pending_amend"


def test_real_postgres_upgrade_downgrade_and_reupgrade(pg_url):
    """Exercise the actual linear chain and rollback on PostgreSQL 16."""
    import psycopg2

    from tests.migrations.conftest_postgres import downgrade, upgrade

    upgrade(pg_url, INDEX_MIGRATION.revision)
    with psycopg2.connect(pg_url) as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT column_name, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='usage_logs' "
            "AND column_name = ANY(%s)",
            (list(COLUMNS),),
        )
        rows = cursor.fetchall()
        assert {row[0] for row in rows} == COLUMNS
        assert all(row[1] == "YES" and row[2] is None for row in rows)
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='persona_model_retirement_alerts'"
        )
        assert {row[0] for row in cursor.fetchall()} == OUTBOX_COLUMNS

    downgrade(pg_url, MIGRATION.down_revision)
    with psycopg2.connect(pg_url) as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='usage_logs' AND column_name = ANY(%s)",
            (list(COLUMNS),),
        )
        assert cursor.fetchall() == []
        cursor.execute("SELECT to_regclass('public.persona_model_retirement_alerts')")
        assert cursor.fetchone()[0] is None

    upgrade(pg_url, INDEX_MIGRATION.revision)
    with psycopg2.connect(pg_url) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchone()[0] == INDEX_MIGRATION.revision
