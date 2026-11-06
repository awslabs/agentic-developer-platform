"""Retired trigger routing, no-side-effect refusals, and shared GSI lookups."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
    """Build a mock chain record (webhook-events row) from the GSI.

    Issue #4128: ``event_id`` matches the ``parent_invocation_id`` in
    :func:`_valid_body`, and ``repo`` matches ``target.repo``. Both are now
    checked, and the previous fixture described a row that cannot exist in a
    real chain — an event whose id is not the parent the caller claims, in a
    chain with no repo. The fixture was encoding the vulnerable behaviour.
    """
    defaults = {
        "event_id": "inv-parent-001",
        "arrived_at": "2026-06-27T10:00:00Z",
        "tenant_id": "test-tenant",
        "correlation_id": "corr-chain-001",
        "repo": "org/repo",
        "root_human_id": "user-human-789",
        "is_human_rooted": True,
        "chain_depth": 1,
        "user_id": "user-human-789",
        "persona": "operations",
        "status": "webhook_received",
    }
    defaults.update(overrides)
    return defaults


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


@pytest.mark.parametrize("authority", [None, "false", "true"])
@pytest.mark.parametrize(
    "body",
    [
        "",
        "{invalid",
        json.dumps(_valid_body()),
        json.dumps(
            {**_valid_body(), "tenant_id": "other", "provenance_marker": "signed-looking-marker"}
        ),
    ],
)
def test_retired_route_cannot_publish_or_resolve_lineage(monkeypatch, authority, body):
    if authority is None:
        monkeypatch.delenv("AGENT_AUTHORITY_ENABLED", raising=False)
    else:
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", authority)
    event = _make_event({})
    event["body"] = body
    event["requestContext"]["identity"]["userArn"] = (
        "arn:aws:sts::999999999999:assumed-role/unrelated/session"
    )
    with (
        patch("agent_trigger._resolve_chain") as resolve,
        patch("common.spawn_persona.spawn_persona") as spawn,
        patch("common.sqs_publisher.publish_envelope") as publish,
    ):
        response = handle_agent_trigger(event, None)
    assert response["statusCode"] == 410
    assert json.loads(response["body"])["error"] == "legacy_agent_trigger_retired"
    resolve.assert_not_called()
    spawn.assert_not_called()
    publish.assert_not_called()
