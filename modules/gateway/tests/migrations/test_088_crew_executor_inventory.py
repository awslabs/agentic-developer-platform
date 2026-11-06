"""Crew inventory migration stays additive and its live-owner index is reversible."""

import asyncio
import re

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex

from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from src.crew.inventory import CrewExecutorInventory
from src.shared.models.crew_executor import CrewExecutor
from tests.migrations.conftest_postgres import GATEWAY_ROOT, downgrade, to_async_url, upgrade


def inventory_revision():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    assert "088_crew_executor_inventory" in {revision.revision for revision in script.walk_revisions("base", heads[0])}
    revision = script.get_revision("088_crew_executor_inventory")
    assert revision.down_revision == "087_run_report_spool_document"
    return revision.module


def test_upgrade_downgrade_and_partial_index_on_local_fixture():
    migration = inventory_revision()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert "crew_executors" in inspect(connection).get_table_names()
            columns = {column["name"]: column for column in inspect(connection).get_columns("crew_executors")}
            assert not columns["tenant_id"]["nullable"] and not columns["user_id"]["nullable"]
            indexes = inspect(connection).get_indexes("crew_executors")
            assert len(indexes) == 1
            assert indexes[0]["name"] == "uq_crew_executors_active_owner"
            assert indexes[0]["unique"]
            assert "state <> 'deleted'" in str(indexes[0]["dialect_options"]["sqlite_where"])
            statement = text("INSERT INTO crew_executors (id, tenant_id, user_id, generation) VALUES (:id, :tenant, :user, :generation)")
            connection.execute(statement, {"id": "a", "tenant": "tenant-a", "user": "same", "generation": 1})
            with pytest.raises(IntegrityError):
                connection.execute(statement, {"id": "duplicate", "tenant": "tenant-a", "user": "same", "generation": 1})
            with pytest.raises(IntegrityError):
                connection.execute(statement, {"id": "blank-tenant", "tenant": "", "user": "same", "generation": 1})
            with pytest.raises(IntegrityError):
                connection.execute(statement, {"id": "blank-user", "tenant": "tenant-a", "user": "", "generation": 1})
            connection.execute(statement, {"id": "b", "tenant": "tenant-b", "user": "same", "generation": 1})
            connection.execute(text("UPDATE crew_executors SET state='deleted' WHERE id='a'"))
            connection.execute(statement, {"id": "replacement", "tenant": "tenant-a", "user": "same", "generation": 2})
            migration.downgrade()
            assert "crew_executors" not in inspect(connection).get_table_names()
    engine.dispose()


def test_postgres_index_uses_owner_and_live_predicate():
    index = next(index for index in CrewExecutor.__table__.indexes if index.name == "uq_crew_executors_active_owner")
    statement = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert "UNIQUE INDEX uq_crew_executors_active_owner" in statement
    assert "(tenant_id, user_id)" in statement
    assert "WHERE state <> 'deleted'" in statement


def test_real_postgres_alembic_upgrade_downgrade_and_index(pg_url):
    upgrade(pg_url, "087_run_report_spool_document")
    engine = create_engine(pg_url)
    try:
        upgrade(pg_url, "088_crew_executor_inventory")
        with engine.connect() as connection:
            definition = connection.scalar(text("SELECT pg_get_indexdef(to_regclass('uq_crew_executors_active_owner'))"))
            assert "UNIQUE INDEX" in definition
            assert "(tenant_id, user_id)" in definition
            assert " WHERE " in definition
            assert re.search(r"\bstate\b.*<>.*'deleted'", definition)
            assert connection.scalar(text("SELECT COUNT(*) FROM crew_executors")) == 0
        downgrade(pg_url, "087_run_report_spool_document")
        with engine.connect() as connection:
            assert "crew_executors" not in inspect(connection).get_table_names()
    finally:
        engine.dispose()


async def test_real_postgres_concurrent_registration_keeps_one_owner_generation(pg_url):
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    inventory = CrewExecutorInventory(factory)
    try:
        rows = await asyncio.gather(*(inventory.ensure(tenant_id="tenant-a", user_id="owner-a") for _ in range(8)))
        assert len({row.id for row in rows}) == 1
        assert {row.generation for row in rows} == {1}
        assert len({row.owner_key for row in rows}) == 1
        assert len(rows[0].owner_key) == 52
        assert {row.latest_verified_checkpoint_id for row in rows} == {None}
        other = await inventory.ensure(tenant_id="tenant-b", user_id="owner-a")
        assert other.id != rows[0].id
        async with factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CrewExecutor)
                    .where(CrewExecutor.tenant_id == "tenant-a", CrewExecutor.user_id == "owner-a", CrewExecutor.state != "deleted")
                )
                == 1
            )
    finally:
        await engine.dispose()
