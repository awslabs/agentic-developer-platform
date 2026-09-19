"""Migration evidence for the #5142 tenant-safe ledger correction."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

MIGRATIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, MIGRATIONS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_052 = _load("052_orchestration_executions.py")
MIG_054 = _load("054_execution_tenant_guards.py")


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
    metadata = sa.MetaData()
    sa.Table(
        "orchestration_flows",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
    )
    sa.Table(
        "orchestration_nodes",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("flow_id", sa.String(36), nullable=False),
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.run_sync(_run, MIG_052.upgrade)
        await conn.run_sync(_run, MIG_054.upgrade)
    return engine


class TestTenantGuardMigration:
    async def test_upgrade_adds_all_composite_foreign_keys(self):
        engine = await _engine()
        async with engine.connect() as conn:
            execution_fks = await conn.run_sync(lambda c: sa_inspect(c).get_foreign_keys("orchestration_executions"))
            action_fks = await conn.run_sync(lambda c: sa_inspect(c).get_foreign_keys("orchestration_actions"))
        await engine.dispose()

        execution_pairs = {(tuple(fk["constrained_columns"]), tuple(fk["referred_columns"])) for fk in execution_fks}
        action_pairs = {(tuple(fk["constrained_columns"]), tuple(fk["referred_columns"])) for fk in action_fks}
        assert (("org_id", "flow_id"), ("org_id", "id")) in execution_pairs
        assert (("org_id", "node_id"), ("org_id", "id")) in execution_pairs
        assert (("org_id", "execution_id"), ("org_id", "id")) in action_pairs

    async def test_upgrade_downgrade_upgrade_round_trip(self):
        engine = await _engine()
        async with engine.begin() as conn:
            await conn.run_sync(_run, MIG_054.downgrade)
            await conn.run_sync(_run, MIG_054.upgrade)
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes("orchestration_executions"))
        await engine.dispose()
        assert MIG_054.EXECUTION_IDENTITY in {index["name"] for index in indexes}

    async def test_database_rejects_cross_tenant_bindings(self):
        engine = await _engine()
        async with engine.connect() as conn:
            await conn.execute(sa.text("PRAGMA foreign_keys=ON"))
            await conn.commit()
            await conn.execute(sa.text("INSERT INTO orchestration_flows (id, org_id) VALUES ('flow-a','a'),('flow-b','b')"))
            await conn.execute(
                sa.text("INSERT INTO orchestration_nodes (id, org_id, flow_id) VALUES ('node-a','a','flow-a'),('node-b','b','flow-b')")
            )
            await conn.commit()

            with pytest.raises(IntegrityError):
                await conn.execute(
                    sa.text(
                        "INSERT INTO orchestration_executions "
                        "(id,org_id,flow_id,node_id,cycle,phase,status,revision,accepted_plan_version,claim_id,claim_generation,attempts,created_at) "
                        "VALUES ('bad','a','flow-b','node-a',1,'admitted','runnable',1,0,'claim',1,0,CURRENT_TIMESTAMP)"
                    )
                )
            await conn.rollback()

            await conn.execute(
                sa.text(
                    "INSERT INTO orchestration_executions "
                    "(id,org_id,flow_id,node_id,cycle,phase,status,revision,accepted_plan_version,claim_id,claim_generation,attempts,created_at) "
                    "VALUES ('good','a','flow-a','node-a',1,'admitted','runnable',1,0,'claim',1,0,CURRENT_TIMESTAMP)"
                )
            )
            await conn.commit()
            with pytest.raises(IntegrityError):
                await conn.execute(
                    sa.text(
                        "INSERT INTO orchestration_actions "
                        "(id,org_id,execution_id,operation_key,kind,status,attempt,created_at) "
                        "VALUES ('bad-action','b','good','op','k','prepared',0,CURRENT_TIMESTAMP)"
                    )
                )
            await conn.rollback()
        await engine.dispose()


class TestRevision:
    def test_revision_is_serialized_after_053(self):
        assert MIG_054.revision == "054_execution_tenant_guards"
        assert MIG_054.down_revision == "053_flow_slug_unique"
        assert len(MIG_054.revision) <= 32
