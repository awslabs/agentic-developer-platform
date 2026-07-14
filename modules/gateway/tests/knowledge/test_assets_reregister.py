"""Unit tests for re-registration after soft-delete and IntegrityError handling.

Issue #3524: soft-deleted assets permanently block re-registration because the
unique index did not exclude status='removed' rows, and register_asset lacked
an IntegrityError handler for race conditions.

Issue #3564: Ported to the gateway production copy (modules/gateway/src/knowledge/routes.py)
per review verdict — the agent-context copy is not mounted in production.

Tests cover:
- register -> soft-delete -> re-register same source/scope -> 201
- duplicate active asset -> 409 (app-level dup-check still works)
- IntegrityError on uq_knowledge_assets_source_scope -> 409 (race condition)
- IntegrityError from a different constraint -> re-raised (500)
- bulk_commit IntegrityError on constraint -> skipped_duplicates + 1
- bulk_commit IntegrityError on other constraint -> re-raised
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from tests.knowledge.conftest import (
    FakeAsyncSession,
    FakeResult,
    FakeRow,
    FakeTokenContext,
)

# ---------------------------------------------------------------------------
# Extended FakeAsyncSession that supports raising exceptions from execute_results
# ---------------------------------------------------------------------------


class FakeAsyncSessionWithErrors(FakeAsyncSession):
    """FakeAsyncSession that can raise exceptions stored in execute_results.

    The base conftest's FakeAsyncSession only returns FakeResult objects.
    This subclass checks if an entry is an Exception and raises it, enabling
    simulation of IntegrityError from db.execute().
    """

    def __init__(self):
        super().__init__()
        self.rolled_back = False

    async def execute(self, stmt: Any, params: dict | None = None) -> FakeResult:
        self.executed_statements.append((stmt, params))
        if self._call_index < len(self.execute_results):
            result = self.execute_results[self._call_index]
            self._call_index += 1
            if isinstance(result, Exception):
                raise result
            return result
        return FakeResult()

    async def rollback(self) -> None:
        self.rolled_back = True


# ---------------------------------------------------------------------------
# Helper: create a simulated IntegrityError
# ---------------------------------------------------------------------------


def _make_integrity_error(constraint_name: str) -> IntegrityError:
    """Create a simulated IntegrityError referencing a specific constraint."""

    class FakeOrigError(Exception):
        def __str__(self):
            return f'duplicate key value violates unique constraint "{constraint_name}"\nDETAIL: Key (source_ref, ...)=(...) already exists.'

    orig = FakeOrigError(f'constraint "{constraint_name}"')
    return IntegrityError(
        statement="INSERT INTO knowledge_assets ...",
        params={},
        orig=orig,
    )


# ---------------------------------------------------------------------------
# Fixture: make_client override that uses our error-aware session
# ---------------------------------------------------------------------------


@pytest.fixture
def make_error_client(knowledge_app):
    """Factory to create a test client using FakeAsyncSessionWithErrors."""
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db
    from src.shared.database_agent_context import get_agent_context_db

    def _make(
        db_session: FakeAsyncSessionWithErrors,
        user: FakeTokenContext | None = None,
    ):
        if user is None:
            user = FakeTokenContext()

        async def override_db():
            yield db_session

        async def override_gateway_db():
            yield db_session

        async def override_user():
            return user

        knowledge_app.dependency_overrides[get_agent_context_db] = override_db
        knowledge_app.dependency_overrides[get_db] = override_gateway_db
        knowledge_app.dependency_overrides[get_current_user] = override_user

        from httpx import ASGITransport, AsyncClient

        return AsyncClient(
            transport=ASGITransport(app=knowledge_app),
            base_url="http://test",
        )

    return _make


# ---------------------------------------------------------------------------
# Test: register -> soft-delete -> re-register same source (201)
# ---------------------------------------------------------------------------


class TestReRegisterAfterSoftDelete:
    """Verify that soft-deleted assets do not block re-registration."""

    @pytest.mark.anyio
    async def test_register_after_remove_succeeds(self, make_error_client, fake_user):
        """Register an asset, soft-delete it, then register the same source again.

        The app-level dup-check should not find the removed row (status='removed'
        is excluded), and the partial unique index should allow the INSERT.
        This test exercises the app-level path (no IntegrityError raised).
        """
        new_asset = FakeRow(id=uuid.uuid4())
        db = FakeAsyncSessionWithErrors()
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

        async with make_error_client(db, fake_user) as client:
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
    async def test_duplicate_returns_409(self, make_error_client, fake_user):
        """Second registration of the same source/scope returns 409."""
        existing_id = uuid.uuid4()
        existing_row = FakeRow(id=existing_id)
        db = FakeAsyncSessionWithErrors()
        # execute calls:
        # 1) quota count -> 1
        # 2) dup-check -> finds existing active row
        db.execute_results = [
            FakeResult(scalar_val=1),  # quota count
            FakeResult(rows=[existing_row]),  # dup-check finds active row
        ]

        async with make_error_client(db, fake_user) as client:
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
    async def test_integrity_error_on_source_scope_returns_409(self, make_error_client, fake_user):
        """Simulated race: dup-check passes but INSERT hits the constraint.

        The handler should rollback, look up the conflicting row, and return
        the standard 409 payload.
        """
        conflicting_id = uuid.uuid4()
        conflicting_row = FakeRow(id=conflicting_id)
        integrity_exc = _make_integrity_error("uq_knowledge_assets_source_scope")

        db = FakeAsyncSessionWithErrors()
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

        async with make_error_client(db, fake_user) as client:
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
    async def test_integrity_error_on_source_scope_no_dup_found(self, make_error_client, fake_user):
        """Edge case: IntegrityError on constraint but post-rollback lookup empty.

        Still returns 409 with existing_id=None (best-effort lookup).
        """
        integrity_exc = _make_integrity_error("uq_knowledge_assets_source_scope")

        db = FakeAsyncSessionWithErrors()
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota
            FakeResult(rows=[]),  # dup-check: clear
            integrity_exc,  # INSERT raises IntegrityError
            FakeResult(rows=[]),  # post-rollback lookup: empty
        ]

        async with make_error_client(db, fake_user) as client:
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
    async def test_other_constraint_error_propagates(self, make_error_client, fake_user):
        """IntegrityError referencing a different constraint should re-raise, not 409.

        The handler only catches uq_knowledge_assets_source_scope violations.
        Any other IntegrityError must propagate so it surfaces as a real error.
        """
        other_exc = _make_integrity_error("some_other_constraint")

        db = FakeAsyncSessionWithErrors()
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota
            FakeResult(rows=[]),  # dup-check: clear
            other_exc,  # INSERT raises IntegrityError (other constraint)
        ]

        with pytest.raises(IntegrityError) as exc_info:
            async with make_error_client(db, fake_user) as client:
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


# ---------------------------------------------------------------------------
# Test: bulk_commit IntegrityError on source-scope constraint -> skip
# ---------------------------------------------------------------------------


class TestBulkCommitIntegrityError:
    """Bulk commit: IntegrityError on source-scope constraint increments skip counter."""

    @pytest.mark.anyio
    async def test_bulk_integrity_error_skips_duplicate(self, make_error_client, fake_user):
        """Simulated race in bulk_commit: INSERT hits constraint, item is skipped.

        The handler should rollback, increment skipped_duplicates, and continue.
        """
        integrity_exc = _make_integrity_error("uq_knowledge_assets_source_scope")

        db = FakeAsyncSessionWithErrors()
        # bulk_commit execute calls (for a single-item batch):
        # 1) per-scope-group quota count query (GROUP BY asset_type)
        # 2) per-item dup-check -> no rows (race)
        # 3) INSERT -> raises IntegrityError
        # (no further calls for this item — it's skipped)
        db.execute_results = [
            FakeResult(rows=[]),  # quota count by type: no existing assets
            FakeResult(rows=[]),  # dup-check: clear
            integrity_exc,  # INSERT raises IntegrityError (race)
        ]

        async with make_error_client(db, fake_user) as client:
            resp = await client.post(
                "/api/agent-context/assets/bulk/commit",
                json={
                    "scope": "personal",
                    "items": [
                        {
                            "asset_type": "repo",
                            "source_ref": "https://github.com/acme/my-service",
                        },
                    ],
                },
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["skipped_duplicates"] == 1
        assert data["created"] == 0
        assert db.rolled_back is True

    @pytest.mark.anyio
    async def test_bulk_other_constraint_error_propagates(self, make_error_client, fake_user):
        """IntegrityError from non-source-scope constraint in bulk_commit re-raises."""
        other_exc = _make_integrity_error("some_other_constraint")

        db = FakeAsyncSessionWithErrors()
        db.execute_results = [
            FakeResult(rows=[]),  # quota count: no existing
            FakeResult(rows=[]),  # dup-check: clear
            other_exc,  # INSERT raises IntegrityError (other)
        ]

        with pytest.raises(IntegrityError) as exc_info:
            async with make_error_client(db, fake_user) as client:
                await client.post(
                    "/api/agent-context/assets/bulk/commit",
                    json={
                        "scope": "personal",
                        "items": [
                            {
                                "asset_type": "repo",
                                "source_ref": "https://github.com/acme/my-service",
                            },
                        ],
                    },
                )

        assert "some_other_constraint" in str(exc_info.value.orig)
        assert db.rolled_back is True
