"""Unit tests for the asset index-status endpoint.

Issue #1796 (Story G of E10 #1736).

Tests cover:
- GET /{id}/status: asset not found → 404
- GET /{id}/status: repo not found → 200 with repo_found=False
- GET /{id}/status: repo found but no runs → 200 with repo_found=True, no stages
- GET /{id}/status: full join → stages returned from latest run
- Scope isolation: other-tenant asset → 404
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

from agent_context.api.assets_router import (
    get_assets_db,
    get_current_user_from_state,
    router,
)


# ---------------------------------------------------------------------------
# Fixtures — fake TokenContext + fake DB session
# ---------------------------------------------------------------------------


@dataclass
class FakeTokenContext:
    """Minimal TokenContext stub for tests."""

    user_id: str = "user-alice"
    org_id: str = "acme-corp"
    is_admin: bool = False


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
    status: str = "indexed"
    last_error: str | None = None
    retry_count: int = 0
    registered_by: str | None = "user-alice"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class FakeRepoRow:
    """Simulates a repositories table row."""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    git_url: str = "https://github.com/acme/my-service"


@dataclass
class FakeRunRow:
    """Simulates an index_runs table row."""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    status: str = "completed"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class FakeStageRow:
    """Simulates an index_run_stages table row."""

    stage: str = "clone"
    status: str = "verified"
    artifact_ref: str | None = "sha256:abc123"
    error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


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

    async def execute(self, stmt: Any, params: dict | None = None) -> FakeResult:
        if self._call_index < len(self.execute_results):
            result = self.execute_results[self._call_index]
            self._call_index += 1
            return result
        return FakeResult()

    async def commit(self) -> None:
        self.committed = True


@pytest.fixture
def fake_user() -> FakeTokenContext:
    return FakeTokenContext()


@pytest.fixture
def app() -> FastAPI:
    """Create a FastAPI app with the assets router mounted."""
    test_app = FastAPI()
    test_app.include_router(router)
    return test_app


@pytest.fixture
def make_client(app: FastAPI):
    """Factory to create a test client with configured dependencies."""

    def _make(db_session: FakeAsyncSession, user: FakeTokenContext | None = None):
        if user is None:
            user = FakeTokenContext()

        async def override_db() -> AsyncGenerator:
            yield db_session

        async def override_user() -> FakeTokenContext:
            return user

        app.dependency_overrides[get_assets_db] = override_db
        app.dependency_overrides[get_current_user_from_state] = override_user
        return AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        )

    return _make


# ---------------------------------------------------------------------------
# GET /api/agent-context/assets/{id}/status
# ---------------------------------------------------------------------------


class TestGetAssetStatus:
    """Tests for GET /api/agent-context/assets/{id}/status."""

    @pytest.mark.anyio
    async def test_asset_not_found_returns_404(self, make_client, fake_user):
        """Request for non-existent asset returns 404."""
        db = FakeAsyncSession()
        # _fetch_asset_by_id returns None
        db.execute_results = [FakeResult(rows=[])]

        client = make_client(db, fake_user)
        async with client:
            resp = await client.get(f"/api/agent-context/assets/{uuid.uuid4()}/status")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_repo_not_found(self, make_client, fake_user):
        """Asset exists but no matching repository — returns repo_found=False."""
        asset_row = FakeRow()
        db = FakeAsyncSession()
        # 1) _fetch_asset_by_id, 2) repo lookup
        db.execute_results = [
            FakeResult(rows=[asset_row]),  # asset found
            FakeResult(rows=[]),  # no repo match
        ]

        client = make_client(db, fake_user)
        async with client:
            resp = await client.get(f"/api/agent-context/assets/{asset_row.id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["repo_found"] is False
        assert data["stages"] == []

    @pytest.mark.anyio
    async def test_repo_found_no_runs(self, make_client, fake_user):
        """Asset's repo exists but has no index runs — returns repo_found=True, no stages."""
        asset_row = FakeRow()
        repo_row = FakeRepoRow()
        db = FakeAsyncSession()
        # 1) _fetch_asset_by_id, 2) repo lookup, 3) latest run
        db.execute_results = [
            FakeResult(rows=[asset_row]),  # asset found
            FakeResult(rows=[repo_row]),  # repo found
            FakeResult(rows=[]),  # no runs
        ]

        client = make_client(db, fake_user)
        async with client:
            resp = await client.get(f"/api/agent-context/assets/{asset_row.id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["repo_found"] is True
        assert data["run_id"] is None
        assert data["stages"] == []

    @pytest.mark.anyio
    async def test_full_join_returns_stages(self, make_client, fake_user):
        """Full join path: asset → repo → run → stages all present."""
        asset_row = FakeRow()
        repo_row = FakeRepoRow()
        run_row = FakeRunRow()
        stage_rows = [
            FakeStageRow(stage="clone", status="verified"),
            FakeStageRow(stage="embed_vectors", status="running", artifact_ref=None, error=None),
            FakeStageRow(stage="zoekt_index", status="failed", artifact_ref=None, error="Timeout"),
        ]

        db = FakeAsyncSession()
        # 1) _fetch_asset_by_id, 2) repo lookup, 3) latest run, 4) stages
        db.execute_results = [
            FakeResult(rows=[asset_row]),  # asset found
            FakeResult(rows=[repo_row]),  # repo found
            FakeResult(rows=[run_row]),  # latest run
            FakeResult(rows=stage_rows),  # stages
        ]

        client = make_client(db, fake_user)
        async with client:
            resp = await client.get(f"/api/agent-context/assets/{asset_row.id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["repo_found"] is True
        assert data["run_id"] == str(run_row.id)
        assert data["run_status"] == "completed"
        assert len(data["stages"]) == 3
        assert data["stages"][0]["stage"] == "clone"
        assert data["stages"][0]["status"] == "verified"
        assert data["stages"][2]["stage"] == "zoekt_index"
        assert data["stages"][2]["status"] == "failed"
        assert data["stages"][2]["error"] == "Timeout"

    @pytest.mark.anyio
    async def test_scope_isolation_other_tenant(self, make_client):
        """Asset owned by different tenant → 404."""
        asset_row = FakeRow(tenant_id="other-corp")
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[asset_row])]

        user = FakeTokenContext(org_id="acme-corp")
        client = make_client(db, user)
        async with client:
            resp = await client.get(f"/api/agent-context/assets/{asset_row.id}/status")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_personal_asset_other_user(self, make_client):
        """Personal asset owned by another user (non-admin) → 404."""
        asset_row = FakeRow(owner_sub="user-bob", tenant_id="acme-corp")
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[asset_row])]

        user = FakeTokenContext(user_id="user-alice", org_id="acme-corp", is_admin=False)
        client = make_client(db, user)
        async with client:
            resp = await client.get(f"/api/agent-context/assets/{asset_row.id}/status")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_admin_can_see_other_personal_asset(self, make_client):
        """Admin can view personal asset owned by another user."""
        asset_row = FakeRow(owner_sub="user-bob", tenant_id="acme-corp")
        db = FakeAsyncSession()
        # 1) _fetch_asset_by_id, 2) repo lookup (no match — simplest success case)
        db.execute_results = [
            FakeResult(rows=[asset_row]),
            FakeResult(rows=[]),  # no repo match
        ]

        admin = FakeTokenContext(user_id="admin-alice", org_id="acme-corp", is_admin=True)
        client = make_client(db, admin)
        async with client:
            resp = await client.get(f"/api/agent-context/assets/{asset_row.id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["repo_found"] is False
