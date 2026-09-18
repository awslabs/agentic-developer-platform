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


@pytest.fixture(autouse=True)
def _reset_rate_limit_buckets():
    """Clear the rate limiter's counters between tests.

    The middleware instance lives for the lifetime of the app, so its buckets are
    shared by every test in the session and requests accumulate across them.
    Without this, a test's result depends on how many requests the tests before
    it happened to make: a suite that passes individually starts returning 429
    once any test exercises many routes, and the failure surfaces in an unrelated
    file (`assert 429 in (401, 403)`), which points at the wrong code.

    Autouse and unconditional, because the leak is not specific to the tests that
    reveal it.
    """
    from app.middleware.rate_limit import RateLimitMiddleware

    def _clear():
        for middleware in getattr(app, "user_middleware", []):
            if middleware.cls is RateLimitMiddleware:
                # Starlette builds the instance lazily on first request, so the
                # object may not exist yet; clearing the built stack is what
                # actually reaches it.
                stack = getattr(app, "middleware_stack", None)
                while stack is not None:
                    if isinstance(stack, RateLimitMiddleware):
                        stack._buckets.clear()
                        return
                    stack = getattr(stack, "app", None)

    _clear()
    yield
    _clear()


@pytest.fixture
async def client():
    """Async HTTP client for testing FastAPI endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# Shared internal token for the machine-to-machine `/internal/*` routes.
# Issue #5055 (U14) put this check on the two routes that lacked it, so tests
# exercising them need a credential. Set on `settings` for the duration of the
# test rather than in the environment, because `Settings` reads env vars once at
# import and a later os.environ write would not be seen.
TEST_INTERNAL_TOKEN = "test-internal-token-not-a-real-secret"


@pytest.fixture
def internal_token_header(monkeypatch):
    """Authorization header carrying the internal shared token."""
    from app.config import settings

    monkeypatch.setattr(settings, "internal_api_token", TEST_INTERNAL_TOKEN)
    return {"Authorization": f"Bearer {TEST_INTERNAL_TOKEN}"}
