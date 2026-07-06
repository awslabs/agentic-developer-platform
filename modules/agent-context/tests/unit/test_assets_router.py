"""Unit tests for the knowledge-assets registry CRUD API.

Issue #1791 (Story B of E10 #1736).

Tests cover:
- POST register: happy path, scope from TokenContext, quota 429, duplicate 409
- GET list: pagination, scope filtering
- GET detail: found, not found, scope isolation
- DELETE: soft-delete, authorization (owner vs admin vs other)
- POST reindex: re-queue, authorization

Uses httpx AsyncClient + FastAPI TestClient with mocked DB (no live DB required).
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
    status: str = "registered"
    last_error: str | None = None
    retry_count: int = 0
    registered_by: str | None = "user-alice"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


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
def admin_user() -> FakeTokenContext:
    return FakeTokenContext(user_id="admin-bob", is_admin=True)


@pytest.fixture
def app(fake_user: FakeTokenContext) -> FastAPI:
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
# POST /api/agent-context/assets — register
# ---------------------------------------------------------------------------


class TestRegisterAsset:
    """Tests for POST /api/agent-context/assets."""

    @pytest.mark.anyio
    async def test_register_personal_scope(self, make_client, fake_user):
        """Happy path: personal scope registers with user_id as owner_sub."""
        asset_row = FakeRow()
        db = FakeAsyncSession()
        # execute calls: 1) quota count, 2) duplicate check, 3) insert, 4) fetch created
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota count
            FakeResult(rows=[]),  # no duplicate (fetchone returns None)
            FakeResult(),  # insert
            FakeResult(rows=[asset_row]),  # fetch created
        ]

        async with make_client(db, fake_user) as client:
            resp = await client.post(
                "/api/agent-context/assets",
                json={
                    "asset_type": "repo",
                    "source_ref": "https://github.com/acme/my-service",
                    "scope": "personal",
                },
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["asset_type"] == "repo"
        assert data["status"] == "registered"
        assert db.committed is True

    @pytest.mark.anyio
    async def test_register_tenant_scope_requires_admin(self, make_client, fake_user):
        """Non-admin cannot register at tenant scope."""
        db = FakeAsyncSession()
        async with make_client(db, fake_user) as client:
            resp = await client.post(
                "/api/agent-context/assets",
                json={
                    "asset_type": "repo",
                    "source_ref": "https://github.com/acme/my-service",
                    "scope": "tenant",
                },
            )

        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_register_tenant_scope_admin_allowed(self, make_client, admin_user):
        """Admin can register at tenant scope."""
        asset_row = FakeRow(owner_sub=None, registered_by="admin-bob")
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota
            FakeResult(rows=[]),  # no dup
            FakeResult(),  # insert
            FakeResult(rows=[asset_row]),  # fetch
        ]

        async with make_client(db, admin_user) as client:
            resp = await client.post(
                "/api/agent-context/assets",
                json={
                    "asset_type": "repo",
                    "source_ref": "https://github.com/acme/my-service",
                    "scope": "tenant",
                },
            )

        assert resp.status_code == 201

    @pytest.mark.anyio
    async def test_register_quota_exceeded_returns_429(self, make_client, fake_user):
        """Soft quota check returns 429 when at limit."""
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=20),  # quota count = limit (default personal repo = 20)
        ]

        async with make_client(db, fake_user) as client:
            resp = await client.post(
                "/api/agent-context/assets",
                json={
                    "asset_type": "repo",
                    "source_ref": "https://github.com/acme/new-repo",
                    "scope": "personal",
                },
            )

        assert resp.status_code == 429
        data = resp.json()
        assert "Quota exceeded" in data["detail"]["message"]
        assert data["detail"]["quota"]["repo"]["used"] == 20
        assert data["detail"]["quota"]["repo"]["limit"] == 20

    @pytest.mark.anyio
    async def test_register_full_url_normalizes_to_canonical(self, make_client, fake_user):
        """A full GitHub URL is persisted canonically (Issue #2864)."""
        captured: dict[str, object] = {}

        class CapturingSession(FakeAsyncSession):
            async def execute(self, stmt, params=None):
                # The INSERT is the 3rd execute; capture its source_ref param.
                if params and "asset_type" in params and "source_ref" in params:
                    captured["source_ref"] = params["source_ref"]
                return await super().execute(stmt, params)

        db = CapturingSession()
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota
            FakeResult(rows=[]),  # no dup
            FakeResult(),  # insert
            FakeResult(rows=[FakeRow()]),  # fetch created
        ]

        async with make_client(db, fake_user) as client:
            resp = await client.post(
                "/api/agent-context/assets",
                json={
                    "asset_type": "repo",
                    "source_ref": "https://github.com/octocat/Hello-World.git",
                    "scope": "personal",
                },
            )

        assert resp.status_code == 201
        assert captured["source_ref"] == "https://github.com/octocat/Hello-World"

    @pytest.mark.anyio
    async def test_register_bare_slug_normalizes(self, make_client, fake_user):
        """A bare org/repo slug is accepted and stored as the canonical URL."""
        captured: dict[str, object] = {}

        class CapturingSession(FakeAsyncSession):
            async def execute(self, stmt, params=None):
                if params and "asset_type" in params and "source_ref" in params:
                    captured["source_ref"] = params["source_ref"]
                return await super().execute(stmt, params)

        db = CapturingSession()
        db.execute_results = [
            FakeResult(scalar_val=0),
            FakeResult(rows=[]),
            FakeResult(),
            FakeResult(rows=[FakeRow()]),
        ]

        async with make_client(db, fake_user) as client:
            resp = await client.post(
                "/api/agent-context/assets",
                json={
                    "asset_type": "repo",
                    "source_ref": "octocat/Hello-World",
                    "scope": "personal",
                },
            )

        assert resp.status_code == 201
        assert captured["source_ref"] == "https://github.com/octocat/Hello-World"

    @pytest.mark.anyio
    async def test_register_double_prefixed_url_normalizes(self, make_client, fake_user):
        """The corrupt double-prefixed URL collapses to canonical (Issue #2864)."""
        captured: dict[str, object] = {}

        class CapturingSession(FakeAsyncSession):
            async def execute(self, stmt, params=None):
                if params and "asset_type" in params and "source_ref" in params:
                    captured["source_ref"] = params["source_ref"]
                return await super().execute(stmt, params)

        db = CapturingSession()
        db.execute_results = [
            FakeResult(scalar_val=0),
            FakeResult(rows=[]),
            FakeResult(),
            FakeResult(rows=[FakeRow()]),
        ]

        async with make_client(db, fake_user) as client:
            resp = await client.post(
                "/api/agent-context/assets",
                json={
                    "asset_type": "repo",
                    "source_ref": "https://github.com/https://github.com/octocat/Hello-World",
                    "scope": "personal",
                },
            )

        assert resp.status_code == 201
        assert captured["source_ref"] == "https://github.com/octocat/Hello-World"

    @pytest.mark.anyio
    async def test_register_invalid_slug_returns_422(self, make_client, fake_user):
        """A source_ref that doesn't reduce to org/repo is rejected with 422."""
        db = FakeAsyncSession()
        async with make_client(db, fake_user) as client:
            resp = await client.post(
                "/api/agent-context/assets",
                json={
                    "asset_type": "repo",
                    "source_ref": "https://github.com/octocat/Hello-World/tree/main",
                    "scope": "personal",
                },
            )

        assert resp.status_code == 422
        assert "org" in resp.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_register_duplicate_after_normalization_returns_409(self, make_client, fake_user):
        """Registering a URL whose canonical form already exists is idempotent (409)."""
        existing_id = uuid.uuid4()
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=5),  # under quota
            FakeResult(rows=[FakeRow(id=existing_id)]),  # canonical form already registered
        ]

        async with make_client(db, fake_user) as client:
            resp = await client.post(
                "/api/agent-context/assets",
                json={
                    # bare slug — normalizes to the same canonical URL as an existing row
                    "asset_type": "repo",
                    "source_ref": "octocat/Hello-World",
                    "scope": "personal",
                },
            )

        assert resp.status_code == 409
        assert resp.json()["detail"]["existing_id"] == str(existing_id)

    @pytest.mark.anyio
    async def test_register_duplicate_returns_409(self, make_client, fake_user):
        """Duplicate source_ref in same scope returns 409."""
        existing_id = uuid.uuid4()
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=5),  # under quota
            FakeResult(rows=[FakeRow(id=existing_id)]),  # duplicate found
        ]

        async with make_client(db, fake_user) as client:
            resp = await client.post(
                "/api/agent-context/assets",
                json={
                    "asset_type": "repo",
                    "source_ref": "https://github.com/acme/my-service",
                    "scope": "personal",
                },
            )

        assert resp.status_code == 409
        data = resp.json()
        assert "already registered" in data["detail"]["message"]
        assert data["detail"]["existing_id"] == str(existing_id)


# ---------------------------------------------------------------------------
# GET /api/agent-context/assets — list
# ---------------------------------------------------------------------------


class TestListAssets:
    """Tests for GET /api/agent-context/assets."""

    @pytest.mark.anyio
    async def test_list_returns_paginated(self, make_client, fake_user):
        """List returns items with pagination metadata."""
        rows = [FakeRow(), FakeRow(source_ref="https://github.com/acme/other")]
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=2),  # count
            FakeResult(rows=rows),  # page rows
            FakeResult(rows=[]),  # quota counts
        ]

        async with make_client(db, fake_user) as client:
            resp = await client.get("/api/agent-context/assets")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        assert data["page"] == 1
        assert data["has_more"] is False

    @pytest.mark.anyio
    async def test_list_with_scope_filter(self, make_client, fake_user):
        """Scope=personal filters by owner_sub."""
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=0),
            FakeResult(rows=[]),
            FakeResult(rows=[]),
        ]

        async with make_client(db, fake_user) as client:
            resp = await client.get("/api/agent-context/assets", params={"scope": "personal"})

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/agent-context/assets/{id} — detail
# ---------------------------------------------------------------------------


class TestGetAssetDetail:
    """Tests for GET /api/agent-context/assets/{id}."""

    @pytest.mark.anyio
    async def test_detail_found(self, make_client, fake_user):
        """Returns asset when found and user has access."""
        asset_row = FakeRow()
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[asset_row])]

        async with make_client(db, fake_user) as client:
            resp = await client.get(f"/api/agent-context/assets/{asset_row.id}")

        assert resp.status_code == 200
        assert resp.json()["id"] == str(asset_row.id)

    @pytest.mark.anyio
    async def test_detail_not_found(self, make_client, fake_user):
        """Returns 404 when asset doesn't exist."""
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[])]

        async with make_client(db, fake_user) as client:
            resp = await client.get(f"/api/agent-context/assets/{uuid.uuid4()}")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_detail_cross_tenant_hidden(self, make_client, fake_user):
        """Asset from a different tenant returns 404 (not 403, for security)."""
        asset_row = FakeRow(tenant_id="other-corp")
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[asset_row])]

        async with make_client(db, fake_user) as client:
            resp = await client.get(f"/api/agent-context/assets/{asset_row.id}")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_detail_personal_asset_hidden_from_other_user(self, make_client):
        """Personal asset hidden from non-owner, non-admin user."""
        asset_row = FakeRow(owner_sub="user-other", registered_by="user-other")
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[asset_row])]

        other_user = FakeTokenContext(user_id="user-eve")
        async with make_client(db, other_user) as client:
            resp = await client.get(f"/api/agent-context/assets/{asset_row.id}")

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/agent-context/assets/{id} — soft-delete
# ---------------------------------------------------------------------------


class TestDeleteAsset:
    """Tests for DELETE /api/agent-context/assets/{id}."""

    @pytest.mark.anyio
    async def test_delete_by_owner(self, make_client, fake_user):
        """Owner can soft-delete their own asset."""
        asset_row = FakeRow(registered_by="user-alice")
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(rows=[asset_row]),  # fetch
            FakeResult(),  # update
        ]

        async with make_client(db, fake_user) as client:
            resp = await client.delete(f"/api/agent-context/assets/{asset_row.id}")

        assert resp.status_code == 204
        assert db.committed is True

    @pytest.mark.anyio
    async def test_delete_by_admin(self, make_client, admin_user):
        """Tenant admin can soft-delete any tenant asset."""
        asset_row = FakeRow(registered_by="user-alice")
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(rows=[asset_row]),
            FakeResult(),
        ]

        async with make_client(db, admin_user) as client:
            resp = await client.delete(f"/api/agent-context/assets/{asset_row.id}")

        assert resp.status_code == 204

    @pytest.mark.anyio
    async def test_delete_by_other_user_forbidden(self, make_client):
        """Non-owner, non-admin cannot delete."""
        asset_row = FakeRow(registered_by="user-alice")
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[asset_row])]

        other_user = FakeTokenContext(user_id="user-eve")
        async with make_client(db, other_user) as client:
            resp = await client.delete(f"/api/agent-context/assets/{asset_row.id}")

        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_delete_not_found(self, make_client, fake_user):
        """Delete on non-existent asset returns 404."""
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[])]

        async with make_client(db, fake_user) as client:
            resp = await client.delete(f"/api/agent-context/assets/{uuid.uuid4()}")

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/agent-context/assets/{id}/reindex — re-queue
# ---------------------------------------------------------------------------


class TestReindexAsset:
    """Tests for POST /api/agent-context/assets/{id}/reindex."""

    @pytest.mark.anyio
    async def test_reindex_by_owner(self, make_client, fake_user):
        """Owner can reindex their asset."""
        asset_row = FakeRow(status="failed", registered_by="user-alice")
        updated_row = FakeRow(status="registered", registered_by="user-alice")
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(rows=[asset_row]),  # fetch
            FakeResult(),  # update
            FakeResult(rows=[updated_row]),  # re-fetch after update
        ]

        async with make_client(db, fake_user) as client:
            resp = await client.post(f"/api/agent-context/assets/{asset_row.id}/reindex")

        assert resp.status_code == 200
        assert resp.json()["status"] == "registered"
        assert db.committed is True

    @pytest.mark.anyio
    async def test_reindex_by_other_user_forbidden(self, make_client):
        """Non-owner, non-admin cannot reindex."""
        asset_row = FakeRow(registered_by="user-alice")
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[asset_row])]

        other_user = FakeTokenContext(user_id="user-eve")
        async with make_client(db, other_user) as client:
            resp = await client.post(f"/api/agent-context/assets/{asset_row.id}/reindex")

        assert resp.status_code == 403
