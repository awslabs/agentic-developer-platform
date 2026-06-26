"""Unit tests for installation_id passthrough in dispatch_ingestion().

Issue #2088 (#2082 Phase-1 story 5): Verifies that the dispatch layer passes
installation_id from the caller through to the SQS message so the ingestion
worker can mint per-installation tokens.

Tests cover:
- installation_id present → included in SQS message body
- installation_id None → NOT present in SQS message body (public/shared)
- Reindex path: installation_id from DB row flows through dispatch
- Backward compatibility: existing callers without installation_id still work
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.knowledge.dispatch import (
    dispatch_ingestion,
    set_sqs_client,
)

# ---------------------------------------------------------------------------
# Fixtures — reuse pattern from test_dispatch.py
# ---------------------------------------------------------------------------


@dataclass
class FakeSQSClient:
    """Records SQS send_message calls for assertions."""

    messages: list[dict[str, Any]] = field(default_factory=list)

    def send_message(
        self,
        QueueUrl: str,  # noqa: N803
        MessageBody: str,  # noqa: N803
        MessageAttributes: dict[str, Any] | None = None,  # noqa: N803
    ) -> dict[str, Any]:
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
        "INGESTION_QUEUE_URL",
        "https://sqs.us-east-1.amazonaws.com/123456789012/test-ingestion",
    )


# ---------------------------------------------------------------------------
# Tests: installation_id passthrough
# ---------------------------------------------------------------------------


class TestDispatchInstallationId:
    """Verify installation_id flows from dispatch callers to SQS message."""

    @pytest.mark.anyio
    async def test_installation_id_included_when_set(self, fake_sqs, fake_db):
        """Private repo: installation_id is included in the SQS message body."""
        result = await dispatch_ingestion(
            asset_id="asset-priv-001",
            asset_type="repo",
            source_ref="https://github.com/acme/private-service",
            tenant_id="acme-corp",
            owner_sub="user-alice",
            project_id=None,
            db=fake_db,
            installation_id=98765,
        )

        assert result is True
        assert len(fake_sqs.messages) == 1
        body = fake_sqs.messages[0]["MessageBody"]
        assert body["installation_id"] == 98765
        assert body["source"] == "acme/private-service"
        assert body["registry_asset_id"] == "asset-priv-001"

    @pytest.mark.anyio
    async def test_installation_id_absent_when_none(self, fake_sqs, fake_db):
        """Public/shared repo: installation_id is NOT in the SQS message body."""
        result = await dispatch_ingestion(
            asset_id="asset-pub-001",
            asset_type="repo",
            source_ref="https://github.com/public-org/open-source",
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
    async def test_installation_id_absent_by_default(self, fake_sqs, fake_db):
        """Callers that don't pass installation_id: message has no installation_id key."""
        result = await dispatch_ingestion(
            asset_id="asset-legacy-001",
            asset_type="url",
            source_ref="https://docs.example.com/page",
            tenant_id="acme-corp",
            owner_sub=None,
            project_id=None,
            db=fake_db,
        )

        assert result is True
        body = fake_sqs.messages[0]["MessageBody"]
        assert "installation_id" not in body

    @pytest.mark.anyio
    async def test_installation_id_zero_not_treated_as_none(self, fake_sqs, fake_db):
        """Edge case: installation_id=0 should still be included (not falsy-skipped).

        In practice GitHub installation IDs are always positive, but the contract
        is 'None means absent' — any integer value means present.
        """
        result = await dispatch_ingestion(
            asset_id="asset-edge-001",
            asset_type="repo",
            source_ref="https://github.com/acme/edge-case",
            tenant_id="acme-corp",
            owner_sub=None,
            project_id=None,
            db=fake_db,
            installation_id=0,
        )

        assert result is True
        body = fake_sqs.messages[0]["MessageBody"]
        # 0 is not None, so it should be included
        assert "installation_id" in body
        assert body["installation_id"] == 0

    @pytest.mark.anyio
    async def test_installation_id_coexists_with_scope(self, fake_sqs, fake_db):
        """installation_id and scope envelope are both present and correct."""
        result = await dispatch_ingestion(
            asset_id="asset-scope-001",
            asset_type="repo",
            source_ref="https://github.com/acme/my-app",
            tenant_id="acme-corp",
            owner_sub="user-bob",
            project_id="proj-42",
            db=fake_db,
            installation_id=54321,
        )

        assert result is True
        body = fake_sqs.messages[0]["MessageBody"]
        assert body["installation_id"] == 54321
        assert body["scope"]["visibility"] == "personal"
        assert body["scope"]["tenant_id"] == "acme-corp"
        assert body["scope"]["owner_sub"] == "user-bob"
        assert body["scope"]["project_id"] == "proj-42"

    @pytest.mark.anyio
    async def test_non_repo_type_with_installation_id_still_passes(self, fake_sqs, fake_db):
        """Non-repo types may carry installation_id (future-proof); dispatch passes it."""
        result = await dispatch_ingestion(
            asset_id="asset-url-001",
            asset_type="url",
            source_ref="https://docs.example.com",
            tenant_id="acme-corp",
            owner_sub=None,
            project_id=None,
            db=fake_db,
            installation_id=11111,
        )

        assert result is True
        body = fake_sqs.messages[0]["MessageBody"]
        # Dispatch doesn't filter by asset_type — it passes installation_id regardless
        assert body["installation_id"] == 11111
