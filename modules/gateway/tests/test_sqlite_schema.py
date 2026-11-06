"""Fixture acceleration must preserve database isolation and schema semantics."""

import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.sqlite_schema import SQLiteSchemas


@pytest.fixture
def schemas(tmp_path):
    return SQLiteSchemas(tmp_path)


@pytest.fixture
def metadata():
    result = MetaData()
    Table("parent", result, Column("id", Integer, primary_key=True), Column("name", String, unique=True))
    Table("child", result, Column("id", Integer, primary_key=True), Column("parent_id", ForeignKey("parent.id")))
    return result


@pytest.fixture(params=["memory", "file"])
async def engines(request, tmp_path):
    created = []

    def make():
        location = ":memory:" if request.param == "memory" else str(tmp_path / f"test-{len(created)}.sqlite")
        engine = create_async_engine(f"sqlite+aiosqlite:///{location}", connect_args={"check_same_thread": False})
        created.append(engine)
        return engine

    yield make
    for engine in created:
        await engine.dispose()


async def test_copies_isolate_committed_rows_and_schema_mutations(schemas, metadata, engines):
    first, second = engines(), engines()
    await schemas.create_all(first, metadata)
    async with first.begin() as connection:
        await connection.execute(text("INSERT INTO parent VALUES (1, 'first')"))
        await connection.execute(text("CREATE TABLE leaked_table(id INTEGER)"))
    await schemas.create_all(second, metadata)
    async with second.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM parent")) == 0
        assert await connection.scalar(text("SELECT count(*) FROM sqlite_master WHERE name='leaked_table'")) == 0
    assert len(schemas.templates) == 1


async def test_existing_data_is_preserved(schemas, metadata, engines):
    engine = engines()
    await schemas.create_all(engine, metadata)
    async with engine.begin() as connection:
        await connection.execute(text("INSERT INTO parent VALUES (1, 'keep')"))
    await schemas.create_all(engine, metadata)
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT name FROM parent")) == "keep"


async def test_changed_metadata_and_new_indexes_get_new_templates(schemas, metadata, engines):
    await schemas.create_all(engines(), metadata)
    metadata.tables["parent"].append_column(Column("extra", String, index=True))
    engine = engines()
    await schemas.create_all(engine, metadata)
    async with engine.connect() as connection:
        columns = (await connection.execute(text("PRAGMA table_info(parent)"))).all()
        assert "extra" in {column[1] for column in columns}
        assert await connection.scalar(text("SELECT count(*) FROM sqlite_master WHERE name='ix_parent_extra'")) == 1
    assert len(schemas.templates) == 2


async def test_ddl_listeners_still_run_for_each_database(schemas, metadata, engines):
    calls = []
    event.listen(metadata, "after_create", lambda *args, **kwargs: calls.append("created"))
    await schemas.create_all(engines(), metadata)
    await schemas.create_all(engines(), metadata)
    assert calls == ["created", "created"]
    assert schemas.templates == {}


async def test_unique_and_foreign_key_constraints_survive_copy(schemas, metadata, engines):
    engine = engines()

    @event.listens_for(engine.sync_engine, "connect")
    def enforce_foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    await schemas.create_all(engine, metadata)
    async with engine.begin() as connection:
        await connection.execute(text("INSERT INTO parent VALUES (1, 'unique')"))
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(text("INSERT INTO parent VALUES (2, 'unique')"))
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(text("INSERT INTO child VALUES (1, 999)"))
