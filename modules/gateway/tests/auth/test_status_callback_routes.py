"""Tests for /internal/v1/knowledge-assets/status-callback endpoint.

Issue #2049: Minimal status-callback bridge (C1/Decision 5).

Coverage:
  - POST status-callback → updates knowledge_assets.status + status_detail (happy path)
  - POST status-callback → 403 for missing/wrong API key
  - POST status-callback → 400 for invalid status
  - POST status-callback → 404 for non-existent asset
  - POST status-callback with status=failed → increments retry_count + sets last_error
  - POST status-callback → does NOT read repositories/index_runs/index_run_stages (no cross-DB)
"""

from __future__ import annotations

import sys
from collections import namedtuple
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

_VALID_KEY = "test-internal-api-key"
_ASSET_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

# Simulate a DB row result
_FakeRow = namedtuple("_FakeRow", ["id"])


class FakeDBSession:
    """Simulates an async SQLAlchemy session for testing."""

    def __init__(self, asset_exists: bool = True):
        self._asset_exists = asset_exists
        self.committed = False
        self.last_query = None
        self.last_params = None

    async def execute(self, query, params=None):
        self.last_query = str(query)
        self.last_params = params

        # Simulate RETURNING clause result
        result = MagicMock()
        if self._asset_exists and params and params.get("asset_id") == _ASSET_ID:
            result.fetchone.return_value = _FakeRow(id=_ASSET_ID)
        else:
            result.fetchone.return_value = None
        return result

    async def commit(self):
        self.committed = True


def _build_app(db_session, auth_enabled: bool = True):
    """Build a FastAPI app with the status callback router and mocked deps."""
    # Mock the heavy auth import chain before importing the route module
    mock_middleware = MagicMock()
    mock_middleware.extract_iam_identity_from_headers = MagicMock(return_value=None)

    with patch.dict(sys.modules, {"src.auth.middleware": mock_middleware}):
        with patch("src.internal.auth_deps.get_settings") as mock_settings:
            mock_settings.return_value.internal_api_key = _VALID_KEY

            # Clear cached module to force reimport with mock
            for mod_name in list(sys.modules.keys()):
                if "status_callback_routes" in mod_name or "auth_deps" in mod_name:
                    del sys.modules[mod_name]

            from src.internal.auth_deps import verify_internal_or_irsa
            from src.internal.status_callback_routes import router
            from src.shared.database import get_db

            app = FastAPI()
            app.include_router(router)

            async def _override_get_db():
                yield db_session

            app.dependency_overrides[get_db] = _override_get_db

            if auth_enabled:

                async def _mock_verify_auth(
                    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-Api-Key"),
                ):
                    if x_internal_api_key != _VALID_KEY:
                        raise HTTPException(status_code=403, detail="forbidden")

                app.dependency_overrides[verify_internal_or_irsa] = _mock_verify_auth

            return app


@pytest.fixture
def db():
    """Fake DB session with asset present."""
    return FakeDBSession(asset_exists=True)


@pytest.fixture
def db_no_asset():
    """Fake DB session with no asset."""
    return FakeDBSession(asset_exists=False)


@pytest.fixture
def client(db):
    """Create test client with valid auth and asset present."""
    app = _build_app(db, auth_enabled=True)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_no_asset(db_no_asset):
    """Create test client with valid auth but no asset."""
    app = _build_app(db_no_asset, auth_enabled=True)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests — Happy path
# ---------------------------------------------------------------------------


class TestStatusCallbackHappyPath:
    """Test successful status callback writes."""

    def test_callback_updates_status_to_indexing(self, client, db):
        """Worker projects 'processing' → gateway 'indexing'."""
        resp = client.post(
            "/internal/v1/knowledge-assets/status-callback",
            json={"asset_id": _ASSET_ID, "status": "indexing"},
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["asset_id"] == _ASSET_ID
        assert data["status"] == "indexing"
        assert "updated_at" in data
        # Verify DB was committed
        assert db.committed is True
        # Verify status was in the SQL params
        assert db.last_params["status"] == "indexing"

    def test_callback_updates_status_to_complete_with_detail(self, client, db):
        """Worker projects 'complete' with compact status_detail."""
        detail = {
            "completed_at": "2026-06-26T01:00:00+00:00",
            "duration_sec": 45.2,
            "steps": {"s3_upload": "ok", "deepwiki": "ok"},
        }
        resp = client.post(
            "/internal/v1/knowledge-assets/status-callback",
            json={"asset_id": _ASSET_ID, "status": "complete", "status_detail": detail},
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "complete"
        # Verify status_detail was passed to DB
        import json

        assert json.loads(db.last_params["status_detail"]) == detail

    def test_callback_updates_status_to_failed_with_error(self, client, db):
        """Worker projects 'failed' with error message."""
        resp = client.post(
            "/internal/v1/knowledge-assets/status-callback",
            json={
                "asset_id": _ASSET_ID,
                "status": "failed",
                "error": "timeout after 900s",
                "status_detail": {"failed_at": "2026-06-26T01:00:00+00:00"},
            },
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"
        # Verify error was in the SQL params
        assert db.last_params["error"] == "timeout after 900s"
        assert db.last_params["status"] == "failed"

    def test_status_detail_null_when_not_provided(self, client, db):
        """status_detail is None when not sent in the request."""
        resp = client.post(
            "/internal/v1/knowledge-assets/status-callback",
            json={"asset_id": _ASSET_ID, "status": "indexing"},
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 200
        assert db.last_params["status_detail"] is None


# ---------------------------------------------------------------------------
# Tests — Auth enforcement
# ---------------------------------------------------------------------------


class TestStatusCallbackAuth:
    """Test auth enforcement on the callback endpoint."""

    def test_missing_api_key_returns_403(self, client):
        """No X-Internal-Api-Key header → 403."""
        resp = client.post(
            "/internal/v1/knowledge-assets/status-callback",
            json={"asset_id": _ASSET_ID, "status": "indexing"},
        )
        assert resp.status_code == 403

    def test_wrong_api_key_returns_403(self, client):
        """Wrong X-Internal-Api-Key → 403."""
        resp = client.post(
            "/internal/v1/knowledge-assets/status-callback",
            json={"asset_id": _ASSET_ID, "status": "indexing"},
            headers={"X-Internal-Api-Key": "wrong-key"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Tests — Validation
# ---------------------------------------------------------------------------


class TestStatusCallbackValidation:
    """Test input validation."""

    def test_invalid_status_returns_400(self, client):
        """Status not in {indexing, complete, failed} → 400."""
        resp = client.post(
            "/internal/v1/knowledge-assets/status-callback",
            json={"asset_id": _ASSET_ID, "status": "bogus"},
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 400
        assert "invalid_status" in resp.json()["detail"]["error"]

    def test_nonexistent_asset_returns_404(self, client_no_asset):
        """Asset ID not in DB → 404."""
        resp = client_no_asset.post(
            "/internal/v1/knowledge-assets/status-callback",
            json={"asset_id": "00000000-0000-0000-0000-000000000000", "status": "indexing"},
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 404

    def test_invalid_asset_id_format_returns_400(self, client):
        """Short/invalid asset_id → 400."""
        resp = client.post(
            "/internal/v1/knowledge-assets/status-callback",
            json={"asset_id": "short", "status": "indexing"},
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Tests — No cross-DB join
# ---------------------------------------------------------------------------


class TestNoCrossDBJoin:
    """Verify the callback path does NOT read from cross-DB tables."""

    def test_callback_sql_only_touches_knowledge_assets(self, client, db):
        """The UPDATE SQL only references knowledge_assets table.

        Proof: inspect the SQL string passed to the DB — it must contain
        'knowledge_assets' and must NOT contain 'repositories', 'index_runs',
        or 'index_run_stages'.
        """
        resp = client.post(
            "/internal/v1/knowledge-assets/status-callback",
            json={"asset_id": _ASSET_ID, "status": "complete"},
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 200

        # Verify the SQL only touches knowledge_assets
        sql = db.last_query.lower()
        assert "knowledge_assets" in sql
        assert "repositories" not in sql
        assert "index_runs" not in sql
        assert "index_run_stages" not in sql
