"""Unit tests for internal admin endpoints.

Issue #3462: Adversarial E2E depends on two admin read-only endpoints:
  - GET /internal/v1/admin/tenant-config/{tenant}
  - GET /internal/v1/admin/audit-entries?provenance_id=

Coverage:
  - tenant-config returns correct feature flags
  - audit-entries filters by provenance_id and returns matching rows
  - both endpoints reject requests without valid auth (403)
  - audit-entries requires provenance_id query param (422)
  - audit-entries requires org_id and always scopes by it (#3985)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.internal.admin_routes import router
from src.shared.database import get_db
from src.shared.models.audit import AuditLog

# ---------------------------------------------------------------------------
# Test app setup
# ---------------------------------------------------------------------------

_VALID_KEY = "test-internal-api-key"

app = FastAPI()
app.include_router(router)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _mock_settings():
    """Patch get_settings for both the auth deps and the admin routes module."""
    with (
        patch("src.internal.auth_deps.get_settings") as mock_auth,
        patch("src.internal.admin_routes.get_settings") as mock_admin,
    ):
        settings = MagicMock()
        settings.internal_api_key = _VALID_KEY
        settings.enable_user_credentials = True
        settings.enforce_credential_binding = True
        mock_auth.return_value = settings
        mock_admin.return_value = settings
        yield settings


@pytest.fixture
def client(_mock_settings):
    """FastAPI test client with mocked settings."""
    return TestClient(app)


@pytest.fixture
def client_creds_disabled():
    """Client where credential features are disabled."""
    with (
        patch("src.internal.auth_deps.get_settings") as mock_auth,
        patch("src.internal.admin_routes.get_settings") as mock_admin,
    ):
        settings = MagicMock()
        settings.internal_api_key = _VALID_KEY
        settings.enable_user_credentials = False
        settings.enforce_credential_binding = False
        mock_auth.return_value = settings
        mock_admin.return_value = settings
        yield TestClient(app)


# ---------------------------------------------------------------------------
# tenant-config tests
# ---------------------------------------------------------------------------


class TestTenantConfig:
    """Tests for GET /internal/v1/admin/tenant-config/{tenant}."""

    def test_returns_config_for_known_tenant(self, client):
        resp = client.get(
            "/internal/v1/admin/tenant-config/adp-security-test",
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["tenant"] == "adp-security-test"
        assert data["enable_user_credentials"] is True
        assert data["enforce_credential_binding"] is True

    def test_returns_false_when_features_disabled(self, client_creds_disabled):
        resp = client_creds_disabled.get(
            "/internal/v1/admin/tenant-config/adp-security-test",
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enable_user_credentials"] is False
        assert data["enforce_credential_binding"] is False

    def test_rejects_without_auth(self, client):
        resp = client.get("/internal/v1/admin/tenant-config/adp-security-test")
        assert resp.status_code == 403

    def test_rejects_wrong_key(self, client):
        resp = client.get(
            "/internal/v1/admin/tenant-config/adp-security-test",
            headers={"X-Internal-Api-Key": "wrong-key"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# audit-entries tests
# ---------------------------------------------------------------------------


def _make_audit_log(
    *,
    event_type: str = "credential_authorization_denied",
    provenance_id: str = "test-run-123",
    actor_id: str | None = "attacker-bot",
    org_id: str = "org-1",
) -> AuditLog:
    """Create a mock AuditLog instance."""
    log = AuditLog()
    log.id = "audit-id-1"
    log.event_type = event_type
    log.actor_id = actor_id
    log.org_id = org_id
    log.details = {"provenance_id": provenance_id, "service": "github"}
    log.created_at = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
    return log


class TestAuditEntries:
    """Tests for GET /internal/v1/admin/audit-entries."""

    def test_returns_entries_filtered_by_provenance_id(self, _mock_settings):
        """Verify audit-entries returns matching rows."""
        mock_row = _make_audit_log(provenance_id="run-abc")

        # Mock the DB session
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_row]

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        app.dependency_overrides[get_db] = lambda: mock_session

        try:
            client = TestClient(app)
            resp = client.get(
                "/internal/v1/admin/audit-entries",
                params={"provenance_id": "run-abc", "org_id": "test-org"},
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "entries" in data
            assert len(data["entries"]) == 1
            entry = data["entries"][0]
            assert entry["event_type"] == "credential_authorization_denied"
            assert entry["actor_id"] == "attacker-bot"
            assert entry["details"]["provenance_id"] == "run-abc"
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_returns_empty_when_no_matches(self, _mock_settings):
        """Verify audit-entries returns empty list when no rows match."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        app.dependency_overrides[get_db] = lambda: mock_session

        try:
            client = TestClient(app)
            resp = client.get(
                "/internal/v1/admin/audit-entries",
                params={"provenance_id": "no-such-run", "org_id": "test-org"},
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["entries"] == []
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_rejects_without_auth(self, _mock_settings):
        resp = TestClient(app).get(
            "/internal/v1/admin/audit-entries",
            params={"provenance_id": "run-abc"},
        )
        assert resp.status_code == 403

    def test_filters_by_org_id_when_provided(self, _mock_settings):
        """When org_id param is provided, results are scoped to that tenant."""
        mock_row = _make_audit_log(provenance_id="run-xyz", org_id="org-sandbox")

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_row]

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        app.dependency_overrides[get_db] = lambda: mock_session

        try:
            client = TestClient(app)
            resp = client.get(
                "/internal/v1/admin/audit-entries",
                params={"provenance_id": "run-xyz", "org_id": "org-sandbox"},
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["entries"]) == 1
            assert data["entries"][0]["org_id"] == "org-sandbox"

            # Verify the SQL statement included org_id filter — inspect the call
            call_args = mock_session.execute.call_args
            stmt = call_args[0][0]
            # The compiled SQL should reference org_id in its WHERE clause
            compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
            assert "org_id" in compiled
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_no_org_id_rejected(self, _mock_settings):
        """Issue #3985: omitting org_id is now a 422 — it was a cross-tenant read.

        Inverted from test_no_org_id_returns_all_tenants, which asserted 200 and
        that the WHERE clause did NOT mention org_id. Unscoped results let one
        caller on this endpoint read every tenant's security audit trail.
        """
        mock_session = AsyncMock()
        app.dependency_overrides[get_db] = lambda: mock_session

        try:
            client = TestClient(app)
            resp = client.get(
                "/internal/v1/admin/audit-entries",
                params={"provenance_id": "run-xyz"},
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
            assert resp.status_code == 422
            # The query must never have run.
            mock_session.execute.assert_not_called()
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_empty_org_id_rejected(self, _mock_settings):
        """Issue #3985: org_id="" must not be accepted as a wildcard either.

        Without min_length=1 an empty string would satisfy a required str param
        and reach the query, matching only rows whose org_id is literally "".
        """
        mock_session = AsyncMock()
        app.dependency_overrides[get_db] = lambda: mock_session

        try:
            client = TestClient(app)
            resp = client.get(
                "/internal/v1/admin/audit-entries",
                params={"provenance_id": "run-xyz", "org_id": ""},
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
            assert resp.status_code == 422
            mock_session.execute.assert_not_called()
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_org_id_always_in_where_clause(self, _mock_settings):
        """Issue #3985: the tenant filter is unconditional, not opt-in."""
        mock_row = _make_audit_log(provenance_id="run-xyz", org_id="org-a")

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_row]

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)

        app.dependency_overrides[get_db] = lambda: mock_session

        try:
            client = TestClient(app)
            resp = client.get(
                "/internal/v1/admin/audit-entries",
                params={"provenance_id": "run-xyz", "org_id": "org-a"},
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )
            assert resp.status_code == 200

            call_args = mock_session.execute.call_args
            stmt = call_args[0][0]
            compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
            where_clause = compiled.split("WHERE")[1] if "WHERE" in compiled else ""
            assert "org_id" in where_clause
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_requires_provenance_id_param(self, _mock_settings):
        """Missing provenance_id should return 422 (FastAPI validation)."""
        client = TestClient(app)
        resp = client.get(
            "/internal/v1/admin/audit-entries",
            headers={"X-Internal-Api-Key": _VALID_KEY},
        )
        assert resp.status_code == 422
