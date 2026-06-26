"""Unit tests for Phase-1 inline SQS dispatch.

Issue #2045: Relocated from agent-context into gateway tests.
Original: Issue #1797 (Story H of E10 #1736).

Tests cover:
- dispatch builds correct scope envelope (personal, tenant, shared)
- dispatch includes correct steps from type registry
- row-before-publish order: status updated only after SQS publish succeeds
- SQS failure leaves row at 'registered' (returns False)
- Unknown asset_type returns False without publishing
- extract_source_identifier strips GitHub prefix for repos
- build_scope_envelope derives visibility correctly
- Zero agent-context dependency (all imports from src.knowledge)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.knowledge.dispatch import (
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
        QueueUrl: str,  # noqa: N803
        MessageBody: str,  # noqa: N803
        MessageAttributes: dict[str, Any] | None = None,  # noqa: N803
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
    monkeypatch.setenv("INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123456789012/test-ingestion")


# ---------------------------------------------------------------------------
# Tests: extract_source_identifier
# ---------------------------------------------------------------------------


class TestExtractSourceIdentifier:
    """Tests for extract_source_identifier()."""

    def test_repo_github_https_stripped(self):
        result = extract_source_identifier("https://github.com/acme/my-service", "repo")
        assert result == "acme/my-service"

    def test_repo_github_https_with_git_suffix(self):
        result = extract_source_identifier("https://github.com/acme/my-service.git", "repo")
        assert result == "acme/my-service"

    def test_repo_github_ssh_stripped(self):
        result = extract_source_identifier("git@github.com:acme/my-service", "repo")
        assert result == "acme/my-service"

    def test_repo_trailing_slash_stripped(self):
        result = extract_source_identifier("https://github.com/acme/my-service/", "repo")
        assert result == "acme/my-service"

    def test_url_type_returns_as_is(self):
        url = "https://docs.example.com/page"
        result = extract_source_identifier(url, "url")
        assert result == url

    def test_doc_type_returns_as_is(self):
        path = "s3://bucket/docs/file.pdf"
        result = extract_source_identifier(path, "doc")
        assert result == path


# ---------------------------------------------------------------------------
# Tests: build_scope_envelope
# ---------------------------------------------------------------------------


class TestBuildScopeEnvelope:
    """Tests for build_scope_envelope()."""

    def test_personal_scope(self):
        scope = build_scope_envelope(tenant_id="acme-corp", owner_sub="user-alice", project_id=None)
        assert scope["visibility"] == "personal"
        assert scope["owner_sub"] == "user-alice"

    def test_tenant_scope(self):
        scope = build_scope_envelope(tenant_id="acme-corp", owner_sub=None, project_id=None)
        assert scope["visibility"] == "tenant"

    def test_shared_scope(self):
        scope = build_scope_envelope(tenant_id=None, owner_sub=None, project_id=None)
        assert scope["visibility"] == "shared"

    def test_with_project_id(self):
        scope = build_scope_envelope(tenant_id="acme-corp", owner_sub="user-alice", project_id="proj-123")
        assert scope["project_id"] == "proj-123"


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
        assert len(fake_sqs.messages) == 1
        msg = fake_sqs.messages[0]
        body = msg["MessageBody"]
        assert body["source"] == "acme/my-service"
        assert body["content_type"] == "repo"
        assert body["registry_asset_id"] == "asset-001"
        assert body["steps"] == ["s3_upload", "cgc", "deepwiki", "graphrag"]
        assert body["triggered_by"] == "self_serve"
        assert body["scope"]["visibility"] == "personal"
        assert msg["MessageAttributes"]["content_type"]["StringValue"] == "repo"
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
        assert body["scope"]["visibility"] == "tenant"

    @pytest.mark.anyio
    async def test_sqs_failure_returns_false(self, fake_db):
        """SQS publish failure -> return False, no status update."""
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
        assert fake_db.committed is False
        set_sqs_client(None)

    @pytest.mark.anyio
    async def test_unknown_asset_type_returns_false(self, fake_sqs, fake_db):
        """Unknown asset_type -> return False without publishing."""
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
        assert "T" in body["enqueued_at"]

    @pytest.mark.anyio
    async def test_missing_queue_url_returns_false(self, fake_db, monkeypatch):
        """Missing INGESTION_QUEUE_URL -> RuntimeError caught, returns False."""
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


class TestZeroAgentContextDependency:
    """Verify that dispatch.py has no agent-context imports."""

    def test_no_agent_context_imports(self):
        """dispatch.py must not import from agent_context (excluding docstrings)."""
        import ast
        import importlib

        module = importlib.import_module("src.knowledge.dispatch")
        source_file = module.__file__
        with open(source_file) as f:
            tree = ast.parse(f.read())

        # Check all Import and ImportFrom nodes
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("agent_context"), f"Found prohibited import: from {node.module}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("agent_context"), f"Found prohibited import: import {alias.name}"
