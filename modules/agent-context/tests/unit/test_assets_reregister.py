"""Unit tests for re-registration after soft-delete and IntegrityError handling.

Issue #3524: soft-deleted assets permanently block re-registration because the
unique index did not exclude status='removed' rows, and register_asset lacked
an IntegrityError handler for race conditions.

Tests cover:
- register -> soft-delete -> re-register same source/scope -> 201
- duplicate active asset -> 409 (app-level dup-check still works)
- IntegrityError on uq_knowledge_assets_source_scope -> 409 (race condition)
- IntegrityError from a different constraint -> re-raised (500)
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
from sqlalchemy.exc import IntegrityError

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
        self.execute_results: list[FakeResult | Exception] = []
        self._call_index = 0
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt: Any, params: dict | None = None) -> FakeResult:
        if self._call_index < len(self.execute_results):
            result = self.execute_results[self._call_index]
            self._call_index += 1
            if isinstance(result, Exception):
                raise result
            return result
        return FakeResult()

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _make_integrity_error(constraint_name: str) -> IntegrityError:
    """Create a simulated IntegrityError referencing a specific constraint."""

    class FakeOrig(Exception):
        def __str__(self):
            return (
                f'duplicate key value violates unique constraint "{constraint_name}"\n'
                f"DETAIL: Key (source_ref, ...)=(...) already exists."
            )

    orig = FakeOrig(f'constraint "{constraint_name}"')
    return IntegrityError(
        statement="INSERT INTO knowledge_assets ...",
        params={},
        orig=orig,
    )


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
# Test: register -> soft-delete -> re-register same source (201)
# ---------------------------------------------------------------------------


class TestReRegisterAfterSoftDelete:
    """Verify that soft-deleted assets do not block re-registration."""

    @pytest.mark.anyio
    async def test_register_after_remove_succeeds(self, make_client, fake_user):
        """Register an asset, soft-delete it, then register the same source again.

        The app-level dup-check should not find the removed row (status='removed'
        is excluded), and the partial unique index should allow the INSERT.
        This test exercises the app-level path (no IntegrityError raised).
        """
        new_asset = FakeRow(id=uuid.uuid4())
        db = FakeAsyncSession()
        # execute calls:
        # 1) quota count -> 0 (within quota)
        # 2) dup-check -> no rows (removed row excluded by status != 'removed')
        # 3) INSERT -> succeeds (no IntegrityError — partial index allows it)
        # 4) fetch created row
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota count
            FakeResult(rows=[]),  # dup-check: no active duplicate
            FakeResult(),  # insert succeeds
            FakeResult(rows=[new_asset]),  # fetch created
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
        assert data["id"] == str(new_asset.id)
        assert data["status"] == "registered"
        assert db.committed is True


# ---------------------------------------------------------------------------
# Test: duplicate active asset -> 409 (app-level check)
# ---------------------------------------------------------------------------


class TestDuplicateActiveAsset:
    """App-level dup-check still returns 409 for active duplicates."""

    @pytest.mark.anyio
    async def test_duplicate_returns_409(self, make_client, fake_user):
        """Second registration of the same source/scope returns 409."""
        existing_id = uuid.uuid4()
        existing_row = FakeRow(id=existing_id)
        db = FakeAsyncSession()
        # execute calls:
        # 1) quota count -> 1
        # 2) dup-check -> finds existing active row
        db.execute_results = [
            FakeResult(scalar_val=1),  # quota count
            FakeResult(rows=[existing_row]),  # dup-check finds active row
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
        assert data["detail"]["message"] == "Asset already registered under this scope"
        assert data["detail"]["existing_id"] == str(existing_id)


# ---------------------------------------------------------------------------
# Test: IntegrityError on uq_knowledge_assets_source_scope -> 409
# ---------------------------------------------------------------------------


class TestIntegrityErrorOnConstraint:
    """Race condition: IntegrityError from the unique constraint maps to 409."""

    @pytest.mark.anyio
    async def test_integrity_error_on_source_scope_returns_409(self, make_client, fake_user):
        """Simulated race: dup-check passes but INSERT hits the constraint.

        The handler should rollback, look up the conflicting row, and return
        the standard 409 payload.
        """
        conflicting_id = uuid.uuid4()
        conflicting_row = FakeRow(id=conflicting_id)
        integrity_exc = _make_integrity_error("uq_knowledge_assets_source_scope")

        db = FakeAsyncSession()
        # execute calls:
        # 1) quota count -> 0
        # 2) dup-check -> no rows (race: not yet visible)
        # 3) INSERT -> raises IntegrityError (another request won the race)
        # 4) post-rollback dup lookup -> finds the conflicting row
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota
            FakeResult(rows=[]),  # dup-check: clear
            integrity_exc,  # INSERT raises IntegrityError
            FakeResult(rows=[conflicting_row]),  # post-rollback lookup
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
        assert data["detail"]["message"] == "Asset already registered under this scope"
        assert data["detail"]["existing_id"] == str(conflicting_id)
        assert db.rolled_back is True
        assert db.committed is False

    @pytest.mark.anyio
    async def test_integrity_error_on_source_scope_no_dup_found(self, make_client, fake_user):
        """Edge case: IntegrityError on constraint but post-rollback lookup empty.

        Still returns 409 with existing_id=None (best-effort lookup).
        """
        integrity_exc = _make_integrity_error("uq_knowledge_assets_source_scope")

        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota
            FakeResult(rows=[]),  # dup-check: clear
            integrity_exc,  # INSERT raises IntegrityError
            FakeResult(rows=[]),  # post-rollback lookup: empty (e.g. row removed)
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
        assert data["detail"]["message"] == "Asset already registered under this scope"
        assert data["detail"]["existing_id"] is None


# ---------------------------------------------------------------------------
# Test: IntegrityError from a different constraint -> re-raised (500)
# ---------------------------------------------------------------------------


class TestIntegrityErrorOtherConstraint:
    """IntegrityError from a non-source-scope constraint is NOT masked as 409."""

    @pytest.mark.anyio
    async def test_other_constraint_error_propagates(self, make_client, fake_user):
        """IntegrityError referencing a different constraint should re-raise, not 409.

        The handler only catches uq_knowledge_assets_source_scope violations.
        Any other IntegrityError must propagate so it surfaces as a real error
        (500 in production, unhandled exception in test ASGI transport).
        """
        other_exc = _make_integrity_error("some_other_constraint")

        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota
            FakeResult(rows=[]),  # dup-check: clear
            other_exc,  # INSERT raises IntegrityError (other constraint)
        ]

        with pytest.raises(IntegrityError) as exc_info:
            async with make_client(db, fake_user) as client:
                await client.post(
                    "/api/agent-context/assets",
                    json={
                        "asset_type": "repo",
                        "source_ref": "https://github.com/acme/my-service",
                        "scope": "personal",
                    },
                )

        # Verify it's the non-source-scope error that propagated
        assert "some_other_constraint" in str(exc_info.value.orig)
        # Rollback should have been called before re-raise
        assert db.rolled_back is True
