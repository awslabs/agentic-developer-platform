"""Run the real PostgreSQL migration against a populated pre-pause flow table."""

import importlib.util
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic.migration import MigrationContext
from alembic.operations import Operations
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401


async def test_upgrade_defaults_existing_and_new_flows_to_paused_without_changing_progress(pg_url):  # noqa: F811
    spec = importlib.util.spec_from_file_location("flow_pause_migration", Path(__file__).parents[2] / "alembic/versions/067_flow_execution_pause.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "066_cred_evidence_delegation"
    engine = create_async_engine(to_async_url(pg_url))
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE orchestration_flows (id text primary key, state text, attempts integer)"))
        await conn.execute(text("INSERT INTO orchestration_flows VALUES ('existing', 'running', 3)"))

        def upgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.upgrade()

        await conn.run_sync(upgrade)
        await conn.execute(text("INSERT INTO orchestration_flows (id, state, attempts) VALUES ('new', 'pending', 0)"))
        rows = (await conn.execute(text("SELECT id, state, attempts, execution_paused FROM orchestration_flows ORDER BY id"))).all()
        assert rows == [("existing", "running", 3, True), ("new", "pending", 0, True)]

        def downgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.downgrade()

        await conn.run_sync(downgrade)
        assert (await conn.execute(text("SELECT state, attempts FROM orchestration_flows WHERE id='existing'"))).one() == ("running", 3)
    await engine.dispose()
