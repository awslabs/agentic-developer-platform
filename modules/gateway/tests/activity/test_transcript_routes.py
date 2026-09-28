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


class TestChainAttributedTranscript:
    """Issue #3949: chain-attributed run (bot user_id, caller root_human_id) → 200.

    This is the case that maps directly to the operator smoke test: clicking
    "View transcript" on a chain-attributed `complete` run used to 404 because
    `get_invocation` queried `user-index` with the caller's id and a bot-owned row
    never matched. These tests exercise the REAL ActivityService over a mocked
    DynamoDB table (not a mocked service) so the authorization path is actually
    executed end-to-end — a service-level mock would assert nothing about scoping.
    """

    _BOT_USER = "bot-user-id-xyz"
    _OTHER_USER = "some-other-human-id"
    _TRANSCRIPT_KEY = "developer/org/repo/issue-42/20260730T100000Z-abc12345.md"

    def _row(self, *, user_id: str, root_human_id: str | None) -> dict:
        """A raw webhook-events row as DynamoDB would return it."""
        row = {
            "event_id": "inv-chain-001",
            "arrived_at": "2026-07-30T10:00:00Z",
            "channel": "github",
            "status": "complete",
            "status_updated_at": "2026-07-30T10:09:00Z",
            "topic": "Chain-attributed run",
            "persona": "developer",
            "user_id": user_id,
            "tenant_id": "org-embark1",
            "correlation_id": "corr-chain-001",
            "parent_invocation_id": "inv-parent-000",
            "transcript_key": self._TRANSCRIPT_KEY,
            "is_human_rooted": True,
        }
        if root_human_id is not None:
            row["root_human_id"] = root_human_id
        return row

    def _client_with_real_service(self, app, mock_access, mock_db, regular_user, row: dict):
        """Wire the app to a REAL ActivityService backed by a mocked DDB table."""
        table = MagicMock()
        table.query.return_value = {"Items": [row], "Count": 1}
        resource = MagicMock()
        resource.Table.return_value = table
        service = ActivityService(table_name="test-table", dynamodb_resource=resource)

        async def override_current_user():
            return regular_user

        async def override_access():
            return mock_access

        async def override_db():
            return mock_db

        app.dependency_overrides[get_current_user] = override_current_user
        app.dependency_overrides[get_activity_service] = lambda: service
        app.dependency_overrides[get_access_control] = override_access
        app.dependency_overrides[get_db] = override_db
        return TestClient(app), table

    @patch("src.activity.routes._get_run_logs_bucket")
    @patch("src.activity.routes._get_s3_client")
    def test_chain_attributed_run_returns_200(self, mock_s3_client_fn, mock_bucket, app, mock_access, mock_db, regular_user):
        """Bot-owned row with root_human_id == caller → 200 with the transcript body."""
        mock_bucket.return_value = "adp-dev-agent-run-logs-123456"
        mock_s3 = MagicMock()
        mock_body = MagicMock()
        mock_body.read.return_value = b"# Chain Transcript\n\nagent output"
        mock_s3.get_object.return_value = {"Body": mock_body}
        mock_s3_client_fn.return_value = mock_s3

        # user_id is the BOT; the caller is only the root_human_id
        row = self._row(user_id=self._BOT_USER, root_human_id=CANONICAL_USER_ID)
        client, table = self._client_with_real_service(app, mock_access, mock_db, regular_user, row)

        resp = client.get("/me/agent-invocations/inv-chain-001/transcript")

        assert resp.status_code == 200
        assert "# Chain Transcript" in resp.text
        # The lookup is a single base-table Query on event_id — no GSI, no page loop
        call_kwargs = table.query.call_args[1]
        assert "IndexName" not in call_kwargs
        assert table.query.call_count == 1
        # transcript_key is resolved server-side from the row, never from the client
        mock_s3.get_object.assert_called_once()
        assert mock_s3.get_object.call_args[1]["Key"] == self._TRANSCRIPT_KEY

    @patch("src.activity.routes._get_run_logs_bucket")
    @patch("src.activity.routes._get_s3_client")
    def test_foreign_chain_attributed_run_returns_404(self, mock_s3_client_fn, mock_bucket, app, mock_access, mock_db, regular_user):
        """Another human's chain-attributed row → 404, and S3 is never touched.

        Disclosure guard for the transcript path: DynamoDB DOES return the row
        (the mock always does), so a pass here proves the in-code authorization
        block rejected it rather than the query simply missing.
        """
        mock_bucket.return_value = "adp-dev-agent-run-logs-123456"
        mock_s3 = MagicMock()
        mock_s3_client_fn.return_value = mock_s3

        row = self._row(user_id=self._BOT_USER, root_human_id=self._OTHER_USER)
        client, _table = self._client_with_real_service(app, mock_access, mock_db, regular_user, row)

        resp = client.get("/me/agent-invocations/inv-chain-001/transcript")

        assert resp.status_code == 404
        # Never reached S3 — no transcript body could leak
        mock_s3.get_object.assert_not_called()

    @patch("src.activity.routes._get_run_logs_bucket")
    @patch("src.activity.routes._get_s3_client")
    def test_sparse_root_human_id_foreign_row_returns_404(self, mock_s3_client_fn, mock_bucket, app, mock_access, mock_db, regular_user):
        """Pre-#2042 row with NO root_human_id owned by someone else → 404.

        Guards against a missing attribute reading as an authorization pass
        (e.g. if both sides of the comparison were ever None).
        """
        mock_bucket.return_value = "adp-dev-agent-run-logs-123456"
        mock_s3 = MagicMock()
        mock_s3_client_fn.return_value = mock_s3

        row = self._row(user_id=self._OTHER_USER, root_human_id=None)
        client, _table = self._client_with_real_service(app, mock_access, mock_db, regular_user, row)

        resp = client.get("/me/agent-invocations/inv-chain-001/transcript")

        assert resp.status_code == 404
        mock_s3.get_object.assert_not_called()


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
