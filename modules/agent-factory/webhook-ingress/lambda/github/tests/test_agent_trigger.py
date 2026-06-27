"""Tests for POST /agent/trigger handler — Issue #2152.

Covers:
  - Body → chain resolution (happy path)
  - Missing correlation_id → 400 missing_lineage
  - Unknown/expired correlation_id → 422 unknown_chain
  - Cross-tenant mismatch → 403 cross_tenant
  - Guard rejections → 422 guard_rejected
  - GSI retry path (empty-then-present)
  - Missing required fields → 400 invalid_body
  - Dispatch from handler.py routing
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add lambda root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_trigger import handle_agent_trigger


def _make_event(body: dict, resource: str = "/agent/trigger") -> dict:
    """Build a minimal API Gateway event for agent trigger."""
    return {
        "resource": resource,
        "httpMethod": "POST",
        "body": json.dumps(body),
        "isBase64Encoded": False,
        "headers": {"content-type": "application/json"},
        "requestContext": {
            "identity": {
                "userArn": "arn:aws:sts::123456789012:assumed-role/adp-dev-agent-worker-role/session-abc"
            }
        },
    }


def _valid_body(**overrides) -> dict:
    """Build a valid request body with optional overrides."""
    defaults = {
        "correlation_id": "corr-chain-001",
        "parent_invocation_id": "inv-parent-001",
        "persona": "developer",
        "target": {"repo": "org/repo", "issue": 42},
        "reason": "Need developer to implement fix",
    }
    defaults.update(overrides)
    return defaults


def _chain_record(**overrides) -> dict:
    """Build a mock chain record (webhook-events row) from the GSI."""
    defaults = {
        "event_id": "evt-001",
        "arrived_at": "2026-06-27T10:00:00Z",
        "tenant_id": "test-tenant",
        "correlation_id": "corr-chain-001",
        "root_human_id": "user-human-789",
        "is_human_rooted": True,
        "chain_depth": 1,
        "user_id": "user-human-789",
        "persona": "operations",
        "status": "webhook_received",
    }
    defaults.update(overrides)
    return defaults


# =============================================================================
# Missing / Invalid Body
# =============================================================================


class TestBodyValidation:
    """Request body validation."""

    def test_missing_correlation_id_returns_400_missing_lineage(self):
        body = _valid_body()
        del body["correlation_id"]
        event = _make_event(body)
        resp = handle_agent_trigger(event, None)
        assert resp["statusCode"] == 400
        result = json.loads(resp["body"])
        assert result["error"] == "missing_lineage"

    def test_empty_correlation_id_returns_400_missing_lineage(self):
        body = _valid_body(correlation_id="")
        event = _make_event(body)
        resp = handle_agent_trigger(event, None)
        assert resp["statusCode"] == 400
        result = json.loads(resp["body"])
        assert result["error"] == "missing_lineage"

    def test_missing_persona_returns_400_invalid_body(self):
        body = _valid_body()
        del body["persona"]
        event = _make_event(body)
        resp = handle_agent_trigger(event, None)
        assert resp["statusCode"] == 400
        result = json.loads(resp["body"])
        assert result["error"] == "invalid_body"

    def test_missing_target_returns_400_invalid_body(self):
        body = _valid_body()
        del body["target"]
        event = _make_event(body)
        resp = handle_agent_trigger(event, None)
        assert resp["statusCode"] == 400
        result = json.loads(resp["body"])
        assert result["error"] == "invalid_body"

    def test_invalid_target_no_repo_returns_400(self):
        body = _valid_body(target={"issue": 1})
        event = _make_event(body)
        resp = handle_agent_trigger(event, None)
        assert resp["statusCode"] == 400
        result = json.loads(resp["body"])
        assert result["error"] == "invalid_body"
        assert "target" in result["detail"]

    def test_invalid_target_no_issue_returns_400(self):
        body = _valid_body(target={"repo": "org/repo"})
        event = _make_event(body)
        resp = handle_agent_trigger(event, None)
        assert resp["statusCode"] == 400
        result = json.loads(resp["body"])
        assert result["error"] == "invalid_body"

    def test_invalid_json_returns_400(self):
        event = {
            "resource": "/agent/trigger",
            "body": "not-json{{{",
            "isBase64Encoded": False,
            "headers": {},
            "requestContext": {"identity": {"userArn": "arn:aws:sts::123:role/x"}},
        }
        resp = handle_agent_trigger(event, None)
        assert resp["statusCode"] == 400
        assert json.loads(resp["body"])["error"] == "invalid_json"


# =============================================================================
# Chain Resolution
# =============================================================================


class TestChainResolution:
    """Chain resolution from correlation-index GSI."""

    @patch("agent_trigger._resolve_chain", return_value=None)
    def test_unknown_chain_returns_422(self, mock_resolve):
        """Unknown/expired correlation_id returns 422 unknown_chain."""
        event = _make_event(_valid_body())
        resp = handle_agent_trigger(event, None)
        assert resp["statusCode"] == 422
        result = json.loads(resp["body"])
        assert result["error"] == "unknown_chain"

    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_valid_chain_calls_spawn_persona(self, mock_resolve, mock_spawn):
        """Valid chain record leads to spawn_persona call."""
        mock_resolve.return_value = _chain_record()
        mock_spawn.return_value = MagicMock(success=True, message_id="msg-123", block_reason=None)

        event = _make_event(_valid_body())
        resp = handle_agent_trigger(event, None)

        assert resp["statusCode"] == 202
        result = json.loads(resp["body"])
        assert result["status"] == "accepted"
        assert result["message_id"] == "msg-123"
        mock_spawn.assert_called_once()

    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_chain_depth_incremented(self, mock_resolve, mock_spawn):
        """Chain depth from record is incremented by 1 in correlation_ctx."""
        mock_resolve.return_value = _chain_record(chain_depth=3)
        mock_spawn.return_value = MagicMock(success=True, message_id="msg-456", block_reason=None)

        event = _make_event(_valid_body())
        handle_agent_trigger(event, None)

        call_kwargs = mock_spawn.call_args[1]
        assert call_kwargs["correlation_ctx"]["chain_depth"] == 4

    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_root_human_from_chain_not_body(self, mock_resolve, mock_spawn):
        """root_human_id comes from the chain record, not the request body."""
        mock_resolve.return_value = _chain_record(root_human_id="real-human-999")
        mock_spawn.return_value = MagicMock(success=True, message_id="msg-789", block_reason=None)

        # Body does NOT contain root_human_id — it's server-resolved
        event = _make_event(_valid_body())
        handle_agent_trigger(event, None)

        call_kwargs = mock_spawn.call_args[1]
        assert call_kwargs["correlation_ctx"]["root_human_id"] == "real-human-999"

    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_parent_invocation_id_from_body(self, mock_resolve, mock_spawn):
        """parent_invocation_id comes from the body (caller declares itself)."""
        mock_resolve.return_value = _chain_record()
        mock_spawn.return_value = MagicMock(success=True, message_id="msg-pid", block_reason=None)

        body = _valid_body(parent_invocation_id="my-inv-id-555")
        event = _make_event(body)
        handle_agent_trigger(event, None)

        call_kwargs = mock_spawn.call_args[1]
        assert call_kwargs["correlation_ctx"]["parent_invocation_id"] == "my-inv-id-555"


# =============================================================================
# Cross-Tenant Check
# =============================================================================


class TestCrossTenantCheck:
    """Cross-tenant validation."""

    @patch("agent_trigger._resolve_chain")
    def test_cross_tenant_mismatch_returns_403(self, mock_resolve):
        """Body tenant_id != chain tenant_id returns 403."""
        mock_resolve.return_value = _chain_record(tenant_id="real-tenant")
        body = _valid_body(tenant_id="different-tenant")
        event = _make_event(body)
        resp = handle_agent_trigger(event, None)
        assert resp["statusCode"] == 403
        result = json.loads(resp["body"])
        assert result["error"] == "cross_tenant"

    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_matching_tenant_allowed(self, mock_resolve, mock_spawn):
        """Body tenant_id == chain tenant_id is fine."""
        mock_resolve.return_value = _chain_record(tenant_id="my-tenant")
        mock_spawn.return_value = MagicMock(success=True, message_id="msg-t", block_reason=None)
        body = _valid_body(tenant_id="my-tenant")
        event = _make_event(body)
        resp = handle_agent_trigger(event, None)
        assert resp["statusCode"] == 202

    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_no_body_tenant_allowed(self, mock_resolve, mock_spawn):
        """Body without tenant_id (omitted) bypasses cross-tenant check."""
        mock_resolve.return_value = _chain_record(tenant_id="my-tenant")
        mock_spawn.return_value = MagicMock(success=True, message_id="msg-nt", block_reason=None)
        body = _valid_body()  # No tenant_id in body
        event = _make_event(body)
        resp = handle_agent_trigger(event, None)
        assert resp["statusCode"] == 202


# =============================================================================
# Guard Rejections
# =============================================================================


class TestGuardRejections:
    """Guard rejections surface as 422 guard_rejected."""

    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_guard_rejected_returns_422(self, mock_resolve, mock_spawn):
        """When spawn_persona blocks, return 422 with guard_rejected."""
        mock_resolve.return_value = _chain_record()
        mock_spawn.return_value = MagicMock(
            success=False, message_id=None, block_reason="chain_depth_exceeded"
        )
        event = _make_event(_valid_body())
        resp = handle_agent_trigger(event, None)
        assert resp["statusCode"] == 422
        result = json.loads(resp["body"])
        assert result["error"] == "guard_rejected"
        assert result["detail"] == "chain_depth_exceeded"

    @patch("common.spawn_persona.spawn_persona")
    @patch("agent_trigger._resolve_chain")
    def test_sqs_failure_returns_500(self, mock_resolve, mock_spawn):
        """SQS publish failure returns 500 enqueue_failed."""
        mock_resolve.return_value = _chain_record()
        mock_spawn.return_value = MagicMock(
            success=False, message_id=None, block_reason="sqs_publish_failed"
        )
        event = _make_event(_valid_body())
        resp = handle_agent_trigger(event, None)
        assert resp["statusCode"] == 500
        assert json.loads(resp["body"])["error"] == "enqueue_failed"


# =============================================================================
# GSI Retry Path
# =============================================================================


class TestGSIRetry:
    """GSI eventual consistency retry."""

    @patch("time.sleep")
    @patch("boto3.resource")
    def test_gsi_empty_then_present_resolves(self, mock_boto_resource, mock_sleep):
        """First query returns empty, retry returns data."""
        import os

        os.environ["EVENTS_TABLE"] = "test-events"
        os.environ["AWS_REGION"] = "us-east-1"

        mock_table = MagicMock()
        mock_boto_resource.return_value.Table.return_value = mock_table

        # First call returns empty, second returns data
        mock_table.query.side_effect = [
            {"Items": []},
            {"Items": [_chain_record()]},
        ]

        # Reset module-level cache
        import agent_trigger

        result = agent_trigger._resolve_chain("corr-test-001")

        assert result is not None
        assert result["correlation_id"] == "corr-chain-001"
        assert mock_table.query.call_count == 2
        mock_sleep.assert_called_once_with(0.2)

    @patch("time.sleep")
    @patch("boto3.resource")
    def test_gsi_both_empty_returns_none(self, mock_boto_resource, mock_sleep):
        """Both attempts return empty → returns None."""
        import os

        os.environ["EVENTS_TABLE"] = "test-events"
        os.environ["AWS_REGION"] = "us-east-1"

        mock_table = MagicMock()
        mock_boto_resource.return_value.Table.return_value = mock_table
        mock_table.query.return_value = {"Items": []}

        import agent_trigger

        result = agent_trigger._resolve_chain("corr-nonexistent")

        assert result is None
        assert mock_table.query.call_count == 2

    @patch("boto3.resource")
    def test_gsi_first_call_succeeds_no_retry(self, mock_boto_resource):
        """First call returns data — no retry needed."""
        import os

        os.environ["EVENTS_TABLE"] = "test-events"
        os.environ["AWS_REGION"] = "us-east-1"

        mock_table = MagicMock()
        mock_boto_resource.return_value.Table.return_value = mock_table
        mock_table.query.return_value = {"Items": [_chain_record()]}

        import agent_trigger

        result = agent_trigger._resolve_chain("corr-chain-001")

        assert result is not None
        assert mock_table.query.call_count == 1


# =============================================================================
# Handler Dispatch
# =============================================================================


class TestHandlerDispatch:
    """handler.py dispatches /agent/trigger before HMAC validation."""

    @patch("agent_trigger.handle_agent_trigger")
    def test_handler_dispatches_agent_trigger(self, mock_handle):
        """handler() routes /agent/trigger to handle_agent_trigger."""
        from handler import handler

        mock_handle.return_value = {"statusCode": 202, "body": "{}"}

        event = _make_event(_valid_body())
        result = handler(event, None)

        mock_handle.assert_called_once_with(event, None)
        assert result["statusCode"] == 202

    def test_handler_does_not_dispatch_github_to_agent_trigger(self):
        """handler() does NOT route /github to handle_agent_trigger."""
        # This verifies that the normal GitHub path still needs HMAC validation
        # (which will fail in tests without a secret setup — that's fine,
        # we just verify the dispatch didn't happen)
        from handler import handler

        event = {
            "resource": "/github",
            "body": "{}",
            "isBase64Encoded": False,
            "headers": {"x-github-event": "ping"},
            "requestContext": {},
        }
        # This will fail at HMAC validation (no secret), which is correct
        # behavior — it means it didn't get dispatched to agent_trigger
        with patch("handler._resolve_webhook_secret", return_value="test-secret"):
            with patch("handler._get_signature") as mock_sig:
                mock_sig.return_value.verify_github_signature.return_value = False
                result = handler(event, None)
                assert result["statusCode"] == 401  # HMAC failure, not agent_trigger
