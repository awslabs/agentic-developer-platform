"""Actual DDL permits a historical binding without inventing a worker ID."""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from alembic.migration import MigrationContext
from alembic.operations import Operations
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401
from tests.migrations.test_051_orchestration_pr_bindings import _load_migration

migration = _load_migration("063_pr_binding_adoption.py")


def apply(connection, fn):
    with Operations.context(MigrationContext.configure(connection)):
        fn()


async def test_actual_migration_preserves_old_run_and_accepts_no_run(pg_url):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url))
    async with engine.begin() as conn:
        await conn.execute(sa.text("CREATE TABLE orchestration_pr_bindings (id text PRIMARY KEY, run_id varchar(255) NOT NULL)"))
        await conn.execute(sa.text("INSERT INTO orchestration_pr_bindings VALUES ('existing', 'worker-1')"))
        await conn.run_sync(lambda c: apply(c, migration.upgrade))
        await conn.execute(sa.text("INSERT INTO orchestration_pr_bindings VALUES ('historical', NULL)"))
        assert (await conn.execute(sa.text("SELECT id, run_id FROM orchestration_pr_bindings ORDER BY id"))).all() == [
            ("existing", "worker-1"),
            ("historical", None),
        ]
        await conn.execute(sa.text("DELETE FROM orchestration_pr_bindings WHERE id = 'historical'"))
        await conn.run_sync(lambda c: apply(c, migration.downgrade))
        columns = await conn.run_sync(lambda c: sa.inspect(c).get_columns("orchestration_pr_bindings"))
        assert next(column for column in columns if column["name"] == "run_id")["nullable"] is False
    await engine.dispose()
