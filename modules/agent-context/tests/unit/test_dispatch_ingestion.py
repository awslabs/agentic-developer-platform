"""Unit tests for Phase-1 inline SQS dispatch.

Issue #1797 (Story H of E10 #1736).

Tests cover:
- dispatch builds correct scope envelope (personal, tenant, shared)
- dispatch includes correct steps from type registry
- row-before-publish order: status updated only after SQS publish succeeds
- SQS failure leaves row at 'registered' (returns False)
- Unknown asset_type returns False without publishing
- extract_source_identifier strips GitHub prefix for repos
- build_scope_envelope derives visibility correctly
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_context.ingestion.dispatch import (
    build_scope_envelope,
    dispatch_ingestion,
    extract_source_identifier,
    set_sqs_client,
)


# ---------------------------------------------------------------------------
# Fixtures — fake SQS client + fake DB session
# ---------------------------------------------------------------------------


@dataclass
class FakeSQSClient:
    """Records SQS send_message calls for assertions."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    should_fail: bool = False

    def send_message(
        self,
        QueueUrl: str,
        MessageBody: str,
        MessageAttributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.should_fail:
            raise RuntimeError("SQS publish failed (simulated)")
        call = {
            "QueueUrl": QueueUrl,
            "MessageBody": json.loads(MessageBody),
            "MessageAttributes": MessageAttributes,
        }
        self.messages.append(call)
        return {"MessageId": "fake-msg-id"}


class FakeAsyncSession:
    """Async mock for SQLAlchemy AsyncSession."""

    def __init__(self):
        self.execute_calls: list[tuple[Any, dict | None]] = []
        self.committed = False

    async def execute(self, stmt: Any, params: dict | None = None) -> Any:
        self.execute_calls.append((stmt, params))
        return MagicMock()

    async def commit(self) -> None:
        self.committed = True


@pytest.fixture
def fake_sqs():
    """Install a fake SQS client and yield it for assertions."""
    client = FakeSQSClient()
    set_sqs_client(client)
    yield client
    set_sqs_client(None)


@pytest.fixture
def fake_db():
    """Return a fresh fake DB session."""
    return FakeAsyncSession()


@pytest.fixture(autouse=True)
def set_queue_url(monkeypatch):
    """Set INGESTION_QUEUE_URL env var for all tests."""
    monkeypatch.setenv(
        "INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123456789012/test-ingestion"
    )


# ---------------------------------------------------------------------------
# Tests: extract_source_identifier
# ---------------------------------------------------------------------------


class TestExtractSourceIdentifier:
    """Tests for extract_source_identifier()."""

    def test_repo_github_https_stripped(self):
        """GitHub HTTPS URL is stripped to org/repo."""
        result = extract_source_identifier("https://github.com/acme/my-service", "repo")
        assert result == "acme/my-service"

    def test_repo_github_https_with_git_suffix(self):
        """Trailing .git is stripped."""
        result = extract_source_identifier("https://github.com/acme/my-service.git", "repo")
        assert result == "acme/my-service"

    def test_repo_github_ssh_stripped(self):
        """GitHub SSH URL is stripped to org/repo."""
        result = extract_source_identifier("git@github.com:acme/my-service", "repo")
        assert result == "acme/my-service"

    def test_repo_github_ssh_with_git_suffix(self):
        """SSH URL with .git suffix stripped."""
        result = extract_source_identifier("git@github.com:acme/my-service.git", "repo")
        assert result == "acme/my-service"

    def test_repo_trailing_slash_stripped(self):
        """Trailing slash is stripped."""
        result = extract_source_identifier("https://github.com/acme/my-service/", "repo")
        assert result == "acme/my-service"

    def test_url_type_returns_as_is(self):
        """URL type returns source_ref unchanged."""
        url = "https://docs.example.com/page"
        result = extract_source_identifier(url, "url")
        assert result == url

    def test_doc_type_returns_as_is(self):
        """Doc type returns source_ref unchanged."""
        path = "s3://bucket/docs/file.pdf"
        result = extract_source_identifier(path, "doc")
        assert result == path


# ---------------------------------------------------------------------------
# Tests: build_scope_envelope
# ---------------------------------------------------------------------------


class TestBuildScopeEnvelope:
    """Tests for build_scope_envelope()."""

    def test_personal_scope(self):
        """owner_sub set → visibility=personal."""
        scope = build_scope_envelope(
            tenant_id="acme-corp",
            owner_sub="user-alice",
            project_id=None,
        )
        assert scope == {
            "tenant_id": "acme-corp",
            "owner_sub": "user-alice",
            "project_id": None,
            "visibility": "personal",
        }

    def test_tenant_scope(self):
        """tenant_id set without owner_sub → visibility=tenant."""
        scope = build_scope_envelope(
            tenant_id="acme-corp",
            owner_sub=None,
            project_id=None,
        )
        assert scope == {
            "tenant_id": "acme-corp",
            "owner_sub": None,
            "project_id": None,
            "visibility": "tenant",
        }

    def test_shared_scope(self):
        """Neither tenant_id nor owner_sub → visibility=shared."""
        scope = build_scope_envelope(
            tenant_id=None,
            owner_sub=None,
            project_id=None,
        )
        assert scope == {
            "tenant_id": None,
            "owner_sub": None,
            "project_id": None,
            "visibility": "shared",
        }

    def test_with_project_id(self):
        """project_id is passed through."""
        scope = build_scope_envelope(
            tenant_id="acme-corp",
            owner_sub="user-alice",
            project_id="proj-123",
        )
        assert scope["project_id"] == "proj-123"
        assert scope["visibility"] == "personal"


# ---------------------------------------------------------------------------
# Tests: dispatch_ingestion
# ---------------------------------------------------------------------------


class TestDispatchIngestion:
    """Tests for dispatch_ingestion() — the core Phase 1 function."""

    @pytest.mark.anyio
    async def test_happy_path_publishes_and_updates_status(self, fake_sqs, fake_db):
        """Successful dispatch: SQS message published, status updated to 'queued'."""
        result = await dispatch_ingestion(
            asset_id="asset-001",
            asset_type="repo",
            source_ref="https://github.com/acme/my-service",
            tenant_id="acme-corp",
            owner_sub="user-alice",
            project_id=None,
            db=fake_db,
        )

        assert result is True
        # SQS message was published
        assert len(fake_sqs.messages) == 1
        msg = fake_sqs.messages[0]
        body = msg["MessageBody"]
        assert body["source"] == "acme/my-service"
        assert body["content_type"] == "repo"
        assert body["registry_asset_id"] == "asset-001"
        assert body["steps"] == ["s3_upload", "cgc", "deepwiki", "graphrag"]
        assert body["triggered_by"] == "self_serve"
        assert body["scope"]["visibility"] == "personal"
        assert body["scope"]["tenant_id"] == "acme-corp"
        assert body["scope"]["owner_sub"] == "user-alice"
        # MessageAttributes
        assert msg["MessageAttributes"]["content_type"]["StringValue"] == "repo"
        # DB status was updated
        assert fake_db.committed is True

    @pytest.mark.anyio
    async def test_url_type_steps(self, fake_sqs, fake_db):
        """URL type uses correct steps from registry."""
        result = await dispatch_ingestion(
            asset_id="asset-002",
            asset_type="url",
            source_ref="https://docs.example.com/page",
            tenant_id="acme-corp",
            owner_sub=None,
            project_id=None,
            db=fake_db,
        )

        assert result is True
        body = fake_sqs.messages[0]["MessageBody"]
        assert body["steps"] == ["s3_upload", "graphrag"]
        assert body["source"] == "https://docs.example.com/page"
        assert body["scope"]["visibility"] == "tenant"

    @pytest.mark.anyio
    async def test_doc_type_steps(self, fake_sqs, fake_db):
        """Doc type uses correct steps from registry."""
        result = await dispatch_ingestion(
            asset_id="asset-003",
            asset_type="doc",
            source_ref="s3://my-bucket/docs/file.pdf",
            tenant_id=None,
            owner_sub=None,
            project_id=None,
            db=fake_db,
        )

        assert result is True
        body = fake_sqs.messages[0]["MessageBody"]
        assert body["steps"] == ["s3_upload", "graphrag"]
        assert body["scope"]["visibility"] == "shared"

    @pytest.mark.anyio
    async def test_sqs_failure_returns_false_no_status_update(self, fake_db):
        """SQS publish failure → return False, no status update."""
        failing_sqs = FakeSQSClient(should_fail=True)
        set_sqs_client(failing_sqs)

        result = await dispatch_ingestion(
            asset_id="asset-004",
            asset_type="repo",
            source_ref="https://github.com/acme/my-service",
            tenant_id="acme-corp",
            owner_sub="user-alice",
            project_id=None,
            db=fake_db,
        )

        assert result is False
        # No DB commit (status stays at 'registered')
        assert fake_db.committed is False
        set_sqs_client(None)

    @pytest.mark.anyio
    async def test_unknown_asset_type_returns_false(self, fake_sqs, fake_db):
        """Unknown asset_type → return False without publishing."""
        result = await dispatch_ingestion(
            asset_id="asset-005",
            asset_type="confluence",
            source_ref="https://acme.atlassian.net/wiki/space",
            tenant_id="acme-corp",
            owner_sub=None,
            project_id=None,
            db=fake_db,
        )

        assert result is False
        assert len(fake_sqs.messages) == 0
        assert fake_db.committed is False

    @pytest.mark.anyio
    async def test_message_contains_enqueued_at(self, fake_sqs, fake_db):
        """Message contains enqueued_at timestamp."""
        await dispatch_ingestion(
            asset_id="asset-006",
            asset_type="repo",
            source_ref="https://github.com/acme/my-service",
            tenant_id="acme-corp",
            owner_sub=None,
            project_id=None,
            db=fake_db,
        )

        body = fake_sqs.messages[0]["MessageBody"]
        assert "enqueued_at" in body
        # ISO format check
        assert "T" in body["enqueued_at"]

    @pytest.mark.anyio
    async def test_queue_url_from_env(self, fake_sqs, fake_db):
        """Queue URL is read from INGESTION_QUEUE_URL env var."""
        await dispatch_ingestion(
            asset_id="asset-007",
            asset_type="repo",
            source_ref="https://github.com/acme/my-service",
            tenant_id="acme-corp",
            owner_sub="user-alice",
            project_id=None,
            db=fake_db,
        )

        msg = fake_sqs.messages[0]
        assert msg["QueueUrl"] == "https://sqs.us-east-1.amazonaws.com/123456789012/test-ingestion"

    @pytest.mark.anyio
    async def test_missing_queue_url_returns_false(self, fake_db, monkeypatch):
        """Missing INGESTION_QUEUE_URL → RuntimeError caught, returns False."""
        monkeypatch.delenv("INGESTION_QUEUE_URL", raising=False)
        fake_client = FakeSQSClient()
        set_sqs_client(fake_client)

        result = await dispatch_ingestion(
            asset_id="asset-008",
            asset_type="repo",
            source_ref="https://github.com/acme/my-service",
            tenant_id="acme-corp",
            owner_sub="user-alice",
            project_id=None,
            db=fake_db,
        )

        assert result is False
        assert len(fake_client.messages) == 0
        set_sqs_client(None)

    @pytest.mark.anyio
    async def test_row_before_publish_invariant(self, fake_sqs, fake_db):
        """The function assumes row already exists at 'registered'.

        It only updates to 'queued' — never creates the row.
        The UPDATE WHERE status='registered' guard ensures idempotency.
        """
        await dispatch_ingestion(
            asset_id="asset-009",
            asset_type="repo",
            source_ref="https://github.com/acme/my-service",
            tenant_id="acme-corp",
            owner_sub="user-alice",
            project_id=None,
            db=fake_db,
        )

        # Verify the DB call is an UPDATE (not INSERT)
        assert len(fake_db.execute_calls) == 1
        stmt = fake_db.execute_calls[0][0]
        # The SQL text should contain UPDATE and status = 'registered' guard
        sql_text = str(stmt.text) if hasattr(stmt, "text") else str(stmt)
        assert "UPDATE" in sql_text
        assert "registered" in sql_text

    @pytest.mark.anyio
    async def test_project_id_in_scope(self, fake_sqs, fake_db):
        """project_id is included in scope envelope when present."""
        await dispatch_ingestion(
            asset_id="asset-010",
            asset_type="url",
            source_ref="https://example.com/docs",
            tenant_id="acme-corp",
            owner_sub="user-alice",
            project_id="proj-xyz",
            db=fake_db,
        )

        body = fake_sqs.messages[0]["MessageBody"]
        assert body["scope"]["project_id"] == "proj-xyz"


class TestDispatchInstallationId:
    """Tests for installation_id passthrough in dispatch_ingestion (#2082)."""

    @pytest.mark.anyio
    async def test_installation_id_included_in_message(self, fake_sqs, fake_db):
        """When installation_id is provided, it appears in the SQS message body."""
        result = await dispatch_ingestion(
            asset_id="asset-install-1",
            asset_type="repo",
            source_ref="https://github.com/acme/private-repo",
            tenant_id="acme-corp",
            owner_sub="user-alice",
            project_id=None,
            db=fake_db,
            installation_id=12345,
        )

        assert result is True
        body = fake_sqs.messages[0]["MessageBody"]
        assert body["installation_id"] == 12345

    @pytest.mark.anyio
    async def test_installation_id_omitted_when_none(self, fake_sqs, fake_db):
        """When installation_id is None (public repo), key is absent from SQS message."""
        result = await dispatch_ingestion(
            asset_id="asset-install-2",
            asset_type="repo",
            source_ref="https://github.com/acme/public-repo",
            tenant_id=None,
            owner_sub=None,
            project_id=None,
            db=fake_db,
            installation_id=None,
        )

        assert result is True
        body = fake_sqs.messages[0]["MessageBody"]
        assert "installation_id" not in body

    @pytest.mark.anyio
    async def test_installation_id_not_set_by_default(self, fake_sqs, fake_db):
        """Without passing installation_id kwarg, it's absent from message."""
        result = await dispatch_ingestion(
            asset_id="asset-install-3",
            asset_type="repo",
            source_ref="https://github.com/acme/public-repo",
            tenant_id="acme-corp",
            owner_sub=None,
            project_id=None,
            db=fake_db,
        )

        assert result is True
        body = fake_sqs.messages[0]["MessageBody"]
        assert "installation_id" not in body
