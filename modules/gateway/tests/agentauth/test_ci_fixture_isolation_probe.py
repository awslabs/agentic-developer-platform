"""Authority fixtures isolate databases while exposing commits to new connections."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.parametrize("copy_number", [1, 2, 3])
async def test_fresh_database_and_independent_connection_visibility(test_engine, copy_number):
    async with test_engine.begin() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM sqlite_master WHERE name='ci_isolation_probe'")) == 0
        await connection.execute(text("CREATE TABLE ci_isolation_probe(value INTEGER)"))
        await connection.execute(text("INSERT INTO ci_isolation_probe VALUES (:value)"), {"value": copy_number})
    independent_engine = create_async_engine(test_engine.url)
    try:
        async with independent_engine.connect() as connection:
            assert await connection.scalar(text("SELECT value FROM ci_isolation_probe")) == copy_number
    finally:
        await independent_engine.dispose()
