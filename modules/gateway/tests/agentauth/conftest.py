"""Authority reads must be able to observe committed state independently."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from src.shared.models.base import Base


@pytest.fixture
async def test_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'authority.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()
