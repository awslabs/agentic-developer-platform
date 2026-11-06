"""Load the gateway's isolated API fixtures with independent SQLite connections."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from src.shared.models.base import Base
from tests.conftest import app as app
from tests.conftest import db_session as db_session
from tests.conftest import db_session_factory as db_session_factory
from tests.conftest import test_client as test_client
from tests.conftest import valid_token_context as valid_token_context


@pytest.fixture
async def test_engine(tmp_path):
    database = tmp_path / "configurations.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}", connect_args={"timeout": 30})
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()
        database.unlink()
