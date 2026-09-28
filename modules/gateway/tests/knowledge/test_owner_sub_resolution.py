"""Unit tests for owner_sub = canonical users.id resolution at dispatch.

Issue #2047: Verifies that the knowledge routes resolve the authenticated
Cognito sub to the canonical users.id UUID via resolve_canonical_user_id()
before writing owner_sub to the DB row and building the SQS scope envelope.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.knowledge.dispatch import (
    build_scope_envelope,
    dispatch_ingestion,
    set_sqs_client,
)
from tests.knowledge.conftest import (
    FakeAsyncSession,
    FakeResult,
    FakeRow,
    FakeTokenContext,
)

# ---------------------------------------------------------------------------
# Tests: register_asset writes canonical owner_sub
# ---------------------------------------------------------------------------


class TestRegisterWritesCanonicalOwnerSub:
    """Verifies that register_asset() resolves canonical users.id, NOT the raw Cognito sub."""

    @pytest.mark.anyio
    async def test_register_personal_scope_uses_canonical_id(self, knowledge_app, make_client, monkeypatch):
        """owner_sub in DB INSERT should be the canonical UUID, not raw Cognito sub."""
        monkeypatch.setenv("AGENT_CONTEXT_ENABLED", "true")

        cognito_sub = "raw-cognito-sub-xyz"
        canonical_id = str(uuid.uuid4())

        user = FakeTokenContext(user_id=cognito_sub)
        asset_row = FakeRow(owner_sub=canonical_id)
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=0),  # quota count
            FakeResult(rows=[]),  # no duplicate
            FakeResult(),  # insert
            FakeResult(rows=[asset_row]),  # fetch created
        ]

        mock_resolve = AsyncMock(return_value=canonical_id)
        mock_dispatch = AsyncMock(return_value=True)

        with (
            patch("src.knowledge.routes.resolve_canonical_user_id", mock_resolve),
            patch("src.knowledge.routes.dispatch_ingestion", mock_dispatch),
        ):
            async with make_client(db, user) as client:
                resp = await client.post(
                    "/api/agent-context/assets",
                    json={
                        "asset_type": "repo",
                        "source_ref": "https://github.com/acme/my-service",
                        "scope": "personal",
                    },
                )

        assert resp.status_code == 201
        mock_resolve.assert_called_once_with(db, cognito_sub)

        # Verify the INSERT statement used canonical_id as owner_sub
        insert_stmt = db.executed_statements[2]  # 3rd call = INSERT
        params = insert_stmt[1]
        assert params["owner_sub"] == canonical_id

        # Verify dispatch was called with the canonical id
        mock_dispatch.assert_called_once()
        call_kwargs = mock_dispatch.call_args[1]
        assert call_kwargs["owner_sub"] == canonical_id

    @pytest.mark.anyio
    async def test_register_tenant_scope_no_owner_sub(self, knowledge_app, make_client, monkeypatch):
        """Tenant scope should NOT resolve owner_sub (stays None)."""
        monkeypatch.setenv("AGENT_CONTEXT_ENABLED", "true")

        admin = FakeTokenContext(user_id="admin-bob", is_admin=True)
        asset_row = FakeRow(owner_sub=None, registered_by="admin-bob")
        db = FakeAsyncSession()
        db.execute_results = [
            FakeResult(scalar_val=0),
            FakeResult(rows=[]),
            FakeResult(),
            FakeResult(rows=[asset_row]),
        ]

        mock_resolve = AsyncMock(return_value="should-not-be-called")
        mock_dispatch = AsyncMock(return_value=True)

        with (
            patch("src.knowledge.routes.resolve_canonical_user_id", mock_resolve),
            patch("src.knowledge.routes.dispatch_ingestion", mock_dispatch),
        ):
            async with make_client(db, admin) as client:
                resp = await client.post(
                    "/api/agent-context/assets",
                    json={
                        "asset_type": "repo",
                        "source_ref": "https://github.com/acme/my-service",
                        "scope": "tenant",
                    },
                )

        assert resp.status_code == 201
        mock_resolve.assert_not_called()

        # Verify dispatch was called with None owner_sub
        mock_dispatch.assert_called_once()
        call_kwargs = mock_dispatch.call_args[1]
        assert call_kwargs["owner_sub"] is None


# ---------------------------------------------------------------------------
# Tests: SQS scope envelope carries canonical id
# ---------------------------------------------------------------------------


class TestSQSEnvelopeCarriesCanonicalId:
    """Verifies that the SQS message scope envelope uses the resolved canonical id."""

    @pytest.mark.anyio
    async def test_dispatch_envelope_contains_canonical_owner_sub(self, monkeypatch):
        """dispatch_ingestion passes the canonical owner_sub into the SQS message."""

        @dataclass
        class FakeSQS:
            messages: list[dict[str, Any]] = field(default_factory=list)

            def send_message(self, QueueUrl, MessageBody, MessageAttributes=None):  # noqa: N803
                self.messages.append(
                    {
                        "QueueUrl": QueueUrl,
                        "MessageBody": json.loads(MessageBody),
                        "MessageAttributes": MessageAttributes,
                    }
                )
                return {"MessageId": "fake-msg-id"}

        monkeypatch.setenv(
            "INGESTION_QUEUE_URL",
            "https://sqs.us-east-1.amazonaws.com/123456789012/test-q",
        )
        fake_sqs = FakeSQS()
        set_sqs_client(fake_sqs)

        canonical_id = str(uuid.uuid4())
        fake_db = AsyncMock()
        fake_db.execute = AsyncMock()
        fake_db.commit = AsyncMock()

        try:
            result = await dispatch_ingestion(
                asset_id="asset-001",
                asset_type="repo",
                source_ref="https://github.com/acme/my-service",
                tenant_id="acme-corp",
                owner_sub=canonical_id,
                project_id=None,
                db=fake_db,
            )

            assert result is True
            assert len(fake_sqs.messages) == 1
            scope = fake_sqs.messages[0]["MessageBody"]["scope"]
            assert scope["owner_sub"] == canonical_id
            assert scope["visibility"] == "personal"
        finally:
            set_sqs_client(None)

    def test_build_scope_envelope_passes_owner_sub_through(self):
        """build_scope_envelope faithfully carries whatever owner_sub is passed."""
        canonical_id = str(uuid.uuid4())
        envelope = build_scope_envelope(tenant_id="acme-corp", owner_sub=canonical_id, project_id=None)
        assert envelope["owner_sub"] == canonical_id
        assert envelope["visibility"] == "personal"
