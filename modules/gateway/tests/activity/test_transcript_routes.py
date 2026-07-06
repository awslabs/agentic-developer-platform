"""Unit tests for transcript endpoints (Issue #3069).

Covers:
- GET /me/agent-invocations/{id}/transcript — 200 with markdown for owned invocation
- GET /me/agent-invocations/{id}/transcript — 404 when invocation not found (ownership)
- GET /me/agent-invocations/{id}/transcript — 404 when row has no transcript_key
- GET /me/agent-invocations/{id}/transcript — 404 when S3 object missing
- GET /admin/agent-invocations/{id}/transcript — admin variant with tenant scoping
- Schema: transcript_key optional and back-compat (rows without it deserialize)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.activity.routes import get_access_control, get_activity_service, router
from src.activity.schemas import InvocationItem
from src.activity.service import ActivityService
from src.admin.access_control import AccessControl
from src.auth.dependencies import get_current_user
from src.shared.database import get_db

CANONICAL_USER_ID = "canonical-abc-999"


def _make_invocation_item(transcript_key: str | None = None) -> InvocationItem:
    """Create a test InvocationItem with optional transcript_key."""
    return InvocationItem(
        invocation_id="inv-001",
        invoked_at="2026-07-06T15:00:00Z",
        channel="github",
        status="complete",
        topic="test topic",
        persona="developer",
        transcript_key=transcript_key,
    )


@pytest.fixture
def app():
    """Create a test FastAPI app with activity routes."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def mock_db():
    """Mock AsyncSession."""
    db = MagicMock()
    db.scalar = AsyncMock(return_value=CANONICAL_USER_ID)
    return db


@pytest.fixture
def mock_service():
    """Create a mock ActivityService."""
    service = MagicMock(spec=ActivityService)
    return service


@pytest.fixture
def mock_access():
    """Create a mock AccessControl that allows everything."""
    ac = MagicMock(spec=AccessControl)
    ac.check_permission = AsyncMock(return_value=True)
    return ac


@pytest.fixture
def client(app, mock_service, mock_access, mock_db, regular_user):
    """Create a test client with regular user auth."""

    async def override_current_user():
        return regular_user

    def override_service():
        return mock_service

    async def override_access():
        return mock_access

    async def override_db():
        return mock_db

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_activity_service] = override_service
    app.dependency_overrides[get_access_control] = override_access
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


@pytest.fixture
def admin_client(app, mock_service, mock_access, mock_db, admin_user):
    """Create a test client with platform admin auth."""

    async def override_current_user():
        return admin_user

    def override_service():
        return mock_service

    async def override_access():
        return mock_access

    async def override_db():
        return mock_db

    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_activity_service] = override_service
    app.dependency_overrides[get_access_control] = override_access
    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


class TestGetMyTranscript:
    """GET /me/agent-invocations/{id}/transcript"""

    @patch("src.activity.routes._get_run_logs_bucket")
    @patch("src.activity.routes._get_s3_client")
    def test_returns_200_with_markdown(self, mock_s3_client_fn, mock_bucket, client, mock_service):
        """Owned invocation with transcript_key → 200 text/markdown."""
        mock_bucket.return_value = "adp-dev-agent-run-logs-123456"
        mock_service.get_invocation.return_value = _make_invocation_item(transcript_key="developer/org/repo/issue-42/20260706T150000Z-abc12345.md")

        mock_s3 = MagicMock()
        mock_body = MagicMock()
        mock_body.read.return_value = b"# Transcript\n\nHello world"
        mock_s3.get_object.return_value = {"Body": mock_body}
        mock_s3_client_fn.return_value = mock_s3

        resp = client.get("/me/agent-invocations/inv-001/transcript")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/markdown; charset=utf-8"
        assert "# Transcript" in resp.text

    def test_returns_404_when_invocation_not_found(self, client, mock_service):
        """Invocation not owned by caller → 404."""
        mock_service.get_invocation.return_value = None

        resp = client.get("/me/agent-invocations/inv-999/transcript")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_returns_404_when_no_transcript_key(self, client, mock_service):
        """Invocation exists but has no transcript_key (pre-#3061 row) → 404."""
        mock_service.get_invocation.return_value = _make_invocation_item(transcript_key=None)

        resp = client.get("/me/agent-invocations/inv-001/transcript")
        assert resp.status_code == 404
        assert "not available" in resp.json()["detail"].lower()

    @patch("src.activity.routes._get_run_logs_bucket")
    @patch("src.activity.routes._get_s3_client")
    def test_returns_404_when_s3_object_missing(self, mock_s3_client_fn, mock_bucket, client, mock_service):
        """transcript_key set but S3 object deleted → 404, not 500."""
        mock_bucket.return_value = "adp-dev-agent-run-logs-123456"
        mock_service.get_invocation.return_value = _make_invocation_item(transcript_key="developer/org/repo/issue-42/20260706T150000Z-abc12345.md")

        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
            "GetObject",
        )
        mock_s3_client_fn.return_value = mock_s3

        resp = client.get("/me/agent-invocations/inv-001/transcript")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    @patch("src.activity.routes._get_run_logs_bucket")
    def test_returns_404_when_bucket_not_configured(self, mock_bucket, client, mock_service):
        """AGENT_RUN_LOGS_BUCKET env not set → 404 (storage not configured)."""
        mock_bucket.return_value = ""
        mock_service.get_invocation.return_value = _make_invocation_item(transcript_key="developer/org/repo/issue-42/20260706T150000Z-abc12345.md")

        resp = client.get("/me/agent-invocations/inv-001/transcript")
        assert resp.status_code == 404
        assert "not configured" in resp.json()["detail"].lower()


class TestGetAdminTranscript:
    """GET /admin/agent-invocations/{id}/transcript"""

    @patch("src.activity.routes._get_run_logs_bucket")
    @patch("src.activity.routes._get_s3_client")
    def test_admin_returns_200(self, mock_s3_client_fn, mock_bucket, admin_client, mock_service):
        """Admin can fetch transcript scoped to tenant."""
        mock_bucket.return_value = "adp-dev-agent-run-logs-123456"
        mock_service.get_invocation.return_value = _make_invocation_item(transcript_key="developer/org/repo/issue-42/20260706T150000Z-abc12345.md")

        mock_s3 = MagicMock()
        mock_body = MagicMock()
        mock_body.read.return_value = b"# Admin Transcript"
        mock_s3.get_object.return_value = {"Body": mock_body}
        mock_s3_client_fn.return_value = mock_s3

        resp = admin_client.get("/admin/agent-invocations/inv-001/transcript")
        assert resp.status_code == 200
        assert "# Admin Transcript" in resp.text

    def test_admin_returns_404_when_not_found(self, admin_client, mock_service):
        """Admin: invocation not in tenant → 404."""
        mock_service.get_invocation.return_value = None

        resp = admin_client.get("/admin/agent-invocations/inv-999/transcript")
        assert resp.status_code == 404


class TestTranscriptKeyInSchema:
    """transcript_key is optional and back-compatible."""

    def test_invocation_item_without_transcript_key(self):
        """Rows without transcript_key deserialize with None (back-compat)."""
        item = InvocationItem(
            invocation_id="inv-old",
            invoked_at="2025-01-01T00:00:00Z",
            status="complete",
        )
        assert item.transcript_key is None

    def test_invocation_item_with_transcript_key(self):
        """Rows with transcript_key deserialize correctly."""
        item = InvocationItem(
            invocation_id="inv-new",
            invoked_at="2026-07-06T15:00:00Z",
            status="complete",
            transcript_key="developer/org/repo/issue-42/20260706T150000Z-abc12345.md",
        )
        assert item.transcript_key == "developer/org/repo/issue-42/20260706T150000Z-abc12345.md"
