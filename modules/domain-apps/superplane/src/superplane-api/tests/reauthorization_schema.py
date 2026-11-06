"""Use the maintained reauthorization DDL, including checks absent from the ORM."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations

VERSIONS = Path(__file__).parents[1] / "alembic/versions"


async def migration(engine, number, direction="upgrade"):
    (path,) = VERSIONS.glob(f"{number:03}_*.py")
    spec = importlib.util.spec_from_file_location(f"reauthorization_{number}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def run(connection):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(module, direction)()

    async with engine.begin() as connection:
        await connection.run_sync(run)


async def create_reauthorization_schema(engine):
    # 055 and 056 change other tables; this owns the complete DDL history of the
    # reauthorization table, including its immutable-history trigger.
    for number in (*range(46, 55), 57, 58):
        await migration(engine, number)
