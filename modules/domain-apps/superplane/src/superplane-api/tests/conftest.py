"""Test fixtures — uses httpx async client with the FastAPI test app.

Overrides the database session with an in-memory SQLite backend so that
tests do not require a running PostgreSQL instance.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_session
from app.main import app

# In-memory SQLite for tests (aiosqlite driver)
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine_test = create_async_engine(TEST_DATABASE_URL, echo=False)
async_session_test = async_sessionmaker(
    engine_test, class_=AsyncSession, expire_on_commit=False
)


# SQLite doesn't support JSONB — map it to JSON for test purposes
@event.listens_for(engine_test.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# Monkey-patch JSONB to render as JSON on SQLite
# This is safe because we only use this for the test engine
_original_compile = None


def _patch_jsonb_for_sqlite():
    """Register JSONB → JSON adapter for SQLite dialect."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    original_method = (
        SQLiteTypeCompiler.visit_JSONB
        if hasattr(SQLiteTypeCompiler, "visit_JSONB")
        else None
    )

    if original_method is None:

        def visit_JSONB(self, type_, **kw):
            return self.visit_JSON(type_, **kw)

        SQLiteTypeCompiler.visit_JSONB = visit_JSONB


_patch_jsonb_for_sqlite()


async def _override_get_session():
    async with async_session_test() as session:
        yield session


# Override the dependency globally
app.dependency_overrides[get_session] = _override_get_session


@pytest.fixture(autouse=True)
async def _setup_db():
    """Create all tables before each test and drop after."""
    async with engine_test.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine_test.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    """Async HTTP client for testing FastAPI endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
