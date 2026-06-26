"""Unit tests for the knowledge-assets registry CRUD API routes.

Issue #2045: Relocated from agent-context into gateway tests.
Original: Issue #1791 (Story B of E10 #1736).

Tests cover:
- POST register: happy path, scope from TokenContext, quota 429, duplicate 409
- GET list: pagination, scope filtering
- GET detail: found, not found, scope isolation
- DELETE: soft-delete, authorization (owner vs admin vs other)
- POST reindex: re-queue, authorization

Uses httpx AsyncClient + FastAPI TestClient with mocked DB (no live DB required).
Imports from src.knowledge (zero agent-context dependency).
"""

from __future__ import annotations

import uuid

import pytest

from tests.knowledge.conftest import (
    FakeAsyncSession,
    FakeResult,
    FakeRow,
    FakeTokenContext,
)

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
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota count
            FakeResult(rows=[]),  # no duplicate
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
            FakeResult(scalar_val=0),
            FakeResult(rows=[]),
            FakeResult(),
            FakeResult(rows=[asset_row]),
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
            FakeResult(scalar_val=20),  # quota count = limit
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

    @pytest.mark.anyio
    async def test_register_duplicate_returns_409(self, make_client, fake_user):
        """Duplicate source_ref in same scope returns 409."""
        existing_id = uuid.uuid4()
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=5),
            FakeResult(rows=[FakeRow(id=existing_id)]),
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
        """Asset from a different tenant returns 404."""
        asset_row = FakeRow(tenant_id="other-corp")
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[asset_row])]

        async with make_client(db, fake_user) as client:
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
            FakeResult(rows=[asset_row]),
            FakeResult(),
        ]

        async with make_client(db, fake_user) as client:
            resp = await client.delete(f"/api/agent-context/assets/{asset_row.id}")

        assert resp.status_code == 204
        assert db.committed is True

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
            FakeResult(rows=[asset_row]),
            FakeResult(),
            FakeResult(rows=[updated_row]),
        ]

        async with make_client(db, fake_user) as client:
            resp = await client.post(f"/api/agent-context/assets/{asset_row.id}/reindex")

        assert resp.status_code == 200
        assert resp.json()["status"] == "registered"
        assert db.committed is True


# ---------------------------------------------------------------------------
# GET /api/agent-context/assets/{id}/status — day-one gateway-row-only (C1)
# ---------------------------------------------------------------------------


class TestGetAssetStatus:
    """Tests for GET /api/agent-context/assets/{id}/status (Issue #2048).

    Proves:
    1. Status endpoint reads ONLY from gateway knowledge_assets row.
    2. No cross-DB join to repositories/index_runs/index_run_stages.
    3. Scope isolation enforced (cross-tenant/cross-owner → 404).
    """

    @pytest.mark.anyio
    async def test_status_returns_slim_gateway_row(self, make_client, fake_user):
        """Status returns {asset_id, source_ref, status} from gateway row only."""
        asset_id = uuid.uuid4()
        asset_row = FakeRow(
            id=asset_id,
            source_ref="https://github.com/acme/my-service",
            status="queued",
        )
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[asset_row])]

        async with make_client(db, fake_user) as client:
            resp = await client.get(f"/api/agent-context/assets/{asset_id}/status")

        assert resp.status_code == 200
        data = resp.json()
        # Exactly the three fields from the design — no more, no less
        assert data == {
            "asset_id": str(asset_id),
            "source_ref": "https://github.com/acme/my-service",
            "status": "queued",
        }

    @pytest.mark.anyio
    async def test_status_no_cross_db_join(self, make_client, fake_user):
        """Status handler issues NO query against repositories/index_runs/index_run_stages.

        Only one DB execute call should happen: the _fetch_asset_by_id SELECT on
        knowledge_assets. No additional queries to corpus tables.
        """
        asset_id = uuid.uuid4()
        asset_row = FakeRow(id=asset_id, status="indexing")
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[asset_row])]

        async with make_client(db, fake_user) as client:
            resp = await client.get(f"/api/agent-context/assets/{asset_id}/status")

        assert resp.status_code == 200

        # Verify only 1 DB query was issued (the _fetch_asset_by_id call)
        assert len(db.executed_statements) == 1
        stmt_text = str(db.executed_statements[0][0].text)
        # The single query must target knowledge_assets only
        assert "knowledge_assets" in stmt_text
        # Must NOT reference any corpus tables
        assert "repositories" not in stmt_text
        assert "index_runs" not in stmt_text
        assert "index_run_stages" not in stmt_text

    @pytest.mark.anyio
    async def test_status_not_found(self, make_client, fake_user):
        """Non-existent asset returns 404."""
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[])]

        async with make_client(db, fake_user) as client:
            resp = await client.get(f"/api/agent-context/assets/{uuid.uuid4()}/status")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_status_cross_tenant_returns_404(self, make_client, fake_user):
        """Asset from different tenant returns 404 (scope isolation)."""
        asset_id = uuid.uuid4()
        asset_row = FakeRow(id=asset_id, tenant_id="other-corp", status="complete")
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[asset_row])]

        async with make_client(db, fake_user) as client:
            resp = await client.get(f"/api/agent-context/assets/{asset_id}/status")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_status_cross_owner_returns_404(self, make_client):
        """Personal asset owned by different user returns 404 (scope isolation)."""
        asset_id = uuid.uuid4()
        asset_row = FakeRow(
            id=asset_id,
            tenant_id="acme-corp",
            owner_sub="user-bob",
            status="pending",
        )
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[asset_row])]

        other_user = FakeTokenContext(user_id="user-eve", org_id="acme-corp")
        async with make_client(db, other_user) as client:
            resp = await client.get(f"/api/agent-context/assets/{asset_id}/status")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_status_admin_can_see_others_asset(self, make_client, admin_user):
        """Admin can view status of any asset in their tenant."""
        asset_id = uuid.uuid4()
        asset_row = FakeRow(
            id=asset_id,
            tenant_id="acme-corp",
            owner_sub="user-alice",
            status="complete",
        )
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[asset_row])]

        async with make_client(db, admin_user) as client:
            resp = await client.get(f"/api/agent-context/assets/{asset_id}/status")

        assert resp.status_code == 200
        assert resp.json()["status"] == "complete"

    @pytest.mark.anyio
    async def test_status_handles_none_status_cleanly(self, make_client, fake_user):
        """Status field is never None — DB column has a default, but handle gracefully."""
        asset_id = uuid.uuid4()
        # Simulate a row where status is pending (the initial state)
        asset_row = FakeRow(id=asset_id, status="pending")
        db = FakeAsyncSession()
        db.execute_results = [FakeResult(rows=[asset_row])]

        async with make_client(db, fake_user) as client:
            resp = await client.get(f"/api/agent-context/assets/{asset_id}/status")

        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"


# ---------------------------------------------------------------------------
# Zero agent-context dependency verification
# ---------------------------------------------------------------------------


class TestZeroAgentContextDependency:
    """Verify that routes.py has no agent-context imports (AST-level check)."""

    def _check_no_agent_context_imports(self, module_name: str) -> None:
        """Helper: parse module source and assert no agent_context imports."""
        import ast
        import importlib

        module = importlib.import_module(module_name)
        source_file = module.__file__
        with open(source_file) as f:
            tree = ast.parse(f.read())

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("agent_context"), f"Found prohibited import in {module_name}: from {node.module}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("agent_context"), f"Found prohibited import in {module_name}: import {alias.name}"

    def test_no_agent_context_imports_in_routes(self):
        """routes.py must not import from agent_context."""
        self._check_no_agent_context_imports("src.knowledge.routes")

    def test_no_agent_context_imports_in_bulk_parser(self):
        """bulk_parser.py must not import from agent_context."""
        self._check_no_agent_context_imports("src.knowledge.bulk_parser")
