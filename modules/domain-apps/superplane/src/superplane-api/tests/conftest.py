"""Test fixtures — uses httpx async client with the FastAPI test app.

Overrides the database session with an in-memory SQLite backend so that
tests do not require a running PostgreSQL instance.
"""

import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# The application creates its production-shaped engine at import time. Give that
# engine an explicit credential-free local target; requests use the isolated SQLite
# engine below and never connect to this URL.
APPLICATION_TEST_DATABASE_URL = "postgresql+asyncpg://localhost/superplane_offline_test"
os.environ["DATABASE_URL"] = APPLICATION_TEST_DATABASE_URL

from app.database import Base, get_session  # noqa: E402
from app.main import app  # noqa: E402

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


# ---------------------------------------------------------------------------
# The offline suite's token signing key — issue #5683 (A04).
#
# `app/config.py` no longer defaults `jwt_secret_key`, because the value it used to
# default to was a committed placeholder that a deployment could unknowingly run
# on. Removing it means the tests that create org-scoped tokens have to state the
# key they use, which is the point: a fixture that silently inherits the
# production default is how such a placeholder survives review, and it makes the
# suite pass in exactly the misconfiguration the fix exists to catch.
#
# Isolated by construction, not by convention:
#   * it is set on the imported `settings` object, so it exists only inside this
#     process and is never written to the environment a deployment reads;
#   * it is autouse and session-scoped, so no test can accidentally depend on the
#     value having leaked in from outside;
#   * the string says what it is, so it cannot be mistaken for a real key if it
#     ever appears in output.
#
# Tests asserting the REFUSAL (that a missing key fails closed) deliberately undo
# this with monkeypatch — see tests/test_jwt_secret_required.py.
TEST_JWT_SECRET_KEY = "offline-test-only-jwt-signing-key-not-a-real-secret"


@pytest.fixture(autouse=True, scope="session")
def _offline_jwt_signing_key():
    """Give the offline suite an explicit signing key for the whole session.

    Set directly on `settings` rather than via the environment because `Settings`
    reads env vars once at import, and this module imports `app.main` above — so an
    `os.environ` write here would already be too late to be seen.
    """
    from app.config import settings

    previous = settings.jwt_secret_key
    settings.jwt_secret_key = TEST_JWT_SECRET_KEY
    yield
    settings.jwt_secret_key = previous


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
