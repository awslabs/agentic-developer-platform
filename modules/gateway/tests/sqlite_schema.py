"""Clone empty SQLite schemas while retaining a fresh database for every test.

Only fixture initialization uses this helper. Migration tests still execute
their real DDL. Templates contain no test data and no shared live connections.
"""

from pathlib import Path

import aiosqlite
from sqlalchemy import MetaData, create_engine
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.schema import CreateIndex, CreateTable


class SQLiteSchemas:
    def __init__(self, directory: Path):
        self.directory = directory
        self.templates: dict[tuple[str, ...], Path] = {}

    async def create_all(self, engine: AsyncEngine, metadata: MetaData) -> None:
        """Initialize a fresh engine, falling back for non-template use cases.

        SQLite connections must allow cross-thread access: aiosqlite's public
        backup API runs on the source connection's thread. No connection is used
        concurrently while its schema is copied.
        """
        async with engine.begin() as connection:
            # DDL listeners can depend on per-test state or seed rows. They must
            # run normally, as must PostgreSQL and already-populated databases.
            listeners = any(
                target.dispatch.before_create or target.dispatch.after_create
                for target in (metadata, *metadata.tables.values(), *(index for table in metadata.tables.values() for index in table.indexes))
            )
            if engine.dialect.name != "sqlite" or listeners or any(table.schema for table in metadata.tables.values()):
                await connection.run_sync(metadata.create_all)
                return
            existing = await connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1")
            if existing.first() is not None or not metadata.tables:
                await connection.run_sync(metadata.create_all)
                return

            # Compile each time so imported tables, changed columns/indexes and
            # custom type compilation cannot reuse a stale schema. Compiling is
            # much cheaper than hundreds of asynchronous DDL/checkfirst calls.
            signature = tuple(
                statement
                for table in sorted(metadata.tables.values(), key=lambda table: table.key)
                for statement in (
                    str(CreateTable(table).compile(dialect=engine.dialect)),
                    *sorted(str(CreateIndex(index).compile(dialect=engine.dialect)) for index in table.indexes),
                )
            )
            template = self.templates.get(signature)
            if template is None:
                template = self.directory / f"schema-{len(self.templates)}.sqlite"
                builder = create_engine("sqlite:///:memory:")
                try:
                    with builder.begin() as source:
                        metadata.create_all(source)
                        template.write_bytes(source.connection.driver_connection.serialize())
                finally:
                    builder.dispose()
                self.templates[signature] = template

            raw = await connection.get_raw_connection()
            async with aiosqlite.connect(f"{template.as_uri()}?mode=ro&immutable=1", uri=True) as source:
                await source.backup(raw.driver_connection)
