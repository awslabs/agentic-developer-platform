"""Knowledge-registry test fixtures.

Issue #2045: Provides shared fakes/mocks for the knowledge module tests,
reusing the gateway's test harness patterns.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Fake TokenContext
# ---------------------------------------------------------------------------


@dataclass
class FakeTokenContext:
    """Minimal TokenContext stub for tests."""

    user_id: str = "user-alice"
    org_id: str = "acme-corp"
    is_admin: bool = False


# ---------------------------------------------------------------------------
# Fake DB row
# ---------------------------------------------------------------------------


@dataclass
class FakeRow:
    """Simulates a DB row returned by fetchone/fetchall."""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    asset_type: str = "repo"
    source_ref: str = "https://github.com/acme/my-service"
    display_name: str | None = None
    tags: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    tenant_id: str | None = "acme-corp"
    owner_sub: str | None = "user-alice"
    project_id: uuid.UUID | None = None
    status: str = "registered"
    status_detail: dict[str, Any] | None = None
    last_error: str | None = None
    retry_count: int = 0
    registered_by: str | None = "user-alice"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Fake SQLAlchemy result + session
# ---------------------------------------------------------------------------


class FakeResult:
    """Simulates SQLAlchemy result from execute()."""

    def __init__(self, rows: list[Any] | None = None, scalar_val: Any = None):
        self._rows = rows or []
        self._scalar_val = scalar_val

    def fetchone(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return self._rows

    def scalar(self) -> Any:
        return self._scalar_val


class FakeAsyncSession:
    """Async mock for SQLAlchemy AsyncSession."""

    def __init__(self):
        self.execute_results: list[FakeResult] = []
        self._call_index = 0
        self.committed = False
        self.executed_statements: list[Any] = []
        # Optional: set to control resolve_canonical_user_id fallback behavior.
        # When None (default), the resolver falls back to the raw cognito_sub.
        self.scalar_result: Any = None

    async def execute(self, stmt: Any, params: dict | None = None) -> FakeResult:
        self.executed_statements.append((stmt, params))
        if self._call_index < len(self.execute_results):
            result = self.execute_results[self._call_index]
            self._call_index += 1
            return result
        return FakeResult()

    async def scalar(self, stmt: Any) -> Any:
        """Support for db.scalar() used by resolve_canonical_user_id."""
        return self.scalar_result

    async def commit(self) -> None:
        self.committed = True


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_queue_url(monkeypatch) -> None:
    """Ensure INGESTION_QUEUE_URL is always set for route tests (prevents 503)."""
    monkeypatch.setenv(
        "INGESTION_QUEUE_URL",
        "https://sqs.us-east-1.amazonaws.com/123456789012/test-ingestion",
    )


@pytest.fixture
def fake_user() -> FakeTokenContext:
    return FakeTokenContext()


@pytest.fixture
def admin_user() -> FakeTokenContext:
    return FakeTokenContext(user_id="admin-bob", is_admin=True)


@pytest.fixture
def regular_user() -> FakeTokenContext:
    return FakeTokenContext(user_id="user-alice", is_admin=False)


@pytest.fixture
def knowledge_app(monkeypatch) -> FastAPI:
    """Create a FastAPI app with the knowledge routes mounted.

    Sets AGENT_CONTEXT_ENABLED=true so the router is exported, then imports
    the routes module fresh.
    """
    monkeypatch.setenv("AGENT_CONTEXT_ENABLED", "true")

    # Import the internal _router directly (bypasses the conditional export
    # so tests work regardless of env state at module import time)
    from src.knowledge.routes import _router

    test_app = FastAPI()
    test_app.include_router(_router)
    return test_app


@pytest.fixture
def make_client(knowledge_app: FastAPI):
    """Factory to create a test client with configured dependencies."""
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db

    def _make(db_session: FakeAsyncSession, user: FakeTokenContext | None = None):
        if user is None:
            user = FakeTokenContext()

        async def override_db() -> AsyncGenerator:
            yield db_session

        async def override_user() -> FakeTokenContext:
            return user

        knowledge_app.dependency_overrides[get_db] = override_db
        knowledge_app.dependency_overrides[get_current_user] = override_user
        return AsyncClient(
            transport=ASGITransport(app=knowledge_app),
            base_url="http://test",
        )

    return _make
