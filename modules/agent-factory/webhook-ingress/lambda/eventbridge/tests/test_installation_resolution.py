"""Tests for EventBridge handler installation_id resolution — Issue #2336.

Tests that the EventBridge handler resolves a real installation_id
from the identity-index DDB before calling spawn_persona, and returns
422 when resolution fails.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add lambda root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "adp-dev-rate-limits")
os.environ.setdefault("SUBMIT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123456789/test.fifo")
os.environ.setdefault("AWS_REGION", "us-east-1")


def _alarm_event(
    persona="operations",
    service_identity="eventbridge:adp-dev-alarm-state-change",
    reason="Threshold exceeded",
    target=None,
):
    """Build a standard EventBridge alarm event with adp_trigger."""
    return {
        "source": "aws.cloudwatch",
        "detail-type": "CloudWatch Alarm State Change",
        "detail": {
            "adp_trigger": {
                "persona": persona,
                "service_identity": service_identity,
                "reason": reason,
                "dedup_key": "high-error-rate-alarm",
                "target": target or {"repo": "acme/infra", "issue_number": 42},
            },
            "alarmName": "high-error-rate-alarm",
            "state": {"value": "ALARM", "reason": reason},
        },
    }


def _mock_service_identity(tenant_id="acme-corp", org_id="acme-corp"):
    """Create a mock ServiceIdentityResult."""
    from common.service_identity import ServiceIdentityResult

    return ServiceIdentityResult(
        tenant_id=tenant_id,
        org_id=org_id,
        service_identity="eventbridge:adp-dev-alarm-state-change",
        allowed_personas=[],
    )


def _mock_rate_result(allowed=True):
    """Create a mock RateLimitResult."""
    mock = MagicMock()
    mock.allowed = allowed
    mock.current_count = 5
    mock.limit = 50
    mock.retry_after_seconds = 120
    return mock


class TestEventBridgeInstallationResolution:
    """Tests that EventBridge handler resolves installation_id from DDB."""

    @patch("common.installation_resolver.resolve_installation_for_tenant")
    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_resolved_installation_id_passed_to_spawn(
        self, mock_svc_mod, mock_rl, mock_resolve
    ):
        """Resolved installation_id is passed to spawn_persona (not 0)."""
        from eventbridge.handler import handle_eventbridge

        identity = _mock_service_identity()
        mock_svc_mod.return_value.resolve_service_identity.return_value = (identity, "ok")
        mock_rl.return_value.check_and_increment.return_value = _mock_rate_result()
        mock_resolve.return_value = 124731131

        event = _alarm_event()

        with patch("common.spawn_persona.spawn_persona") as mock_spawn:
            mock_spawn.return_value = MagicMock(
                success=True, message_id="msg-123", block_reason=None
            )
            result = handle_eventbridge(event, None)

            # Verify installation_id is the resolved value, not 0
            call_kwargs = mock_spawn.call_args.kwargs
            assert call_kwargs["installation_id"] == 124731131

        assert result["statusCode"] == 202

    @patch("common.installation_resolver.resolve_installation_for_tenant")
    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_unresolved_installation_returns_422(
        self, mock_svc_mod, mock_rl, mock_resolve
    ):
        """When installation_id cannot be resolved, handler returns 422."""
        from eventbridge.handler import handle_eventbridge

        identity = _mock_service_identity()
        mock_svc_mod.return_value.resolve_service_identity.return_value = (identity, "ok")
        mock_rl.return_value.check_and_increment.return_value = _mock_rate_result()
        mock_resolve.return_value = None  # Resolution failed

        event = _alarm_event()
        result = handle_eventbridge(event, None)

        assert result["statusCode"] == 422
        import json
        body = json.loads(result["body"])
        assert body["error"] == "no_installation_for_tenant"

    @patch("common.installation_resolver.resolve_installation_for_tenant")
    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_resolver_called_with_org_id(
        self, mock_svc_mod, mock_rl, mock_resolve
    ):
        """Resolver is called with the org_id from identity resolution."""
        from eventbridge.handler import handle_eventbridge

        identity = _mock_service_identity(org_id="aws-e")
        mock_svc_mod.return_value.resolve_service_identity.return_value = (identity, "ok")
        mock_rl.return_value.check_and_increment.return_value = _mock_rate_result()
        mock_resolve.return_value = 124731131

        event = _alarm_event()

        with patch("common.spawn_persona.spawn_persona") as mock_spawn:
            mock_spawn.return_value = MagicMock(
                success=True, message_id="msg-123", block_reason=None
            )
            handle_eventbridge(event, None)

        mock_resolve.assert_called_once_with("aws-e")


class TestAgentTriggerInstallationResolution:
    """Tests that agent_trigger handler resolves installation_id from DDB."""

    @patch("common.installation_resolver.resolve_installation_for_tenant")
    def test_resolved_installation_id_passed_to_spawn(self, mock_resolve):
        """Resolved installation_id is passed to spawn_persona (not 0)."""
        from github.agent_trigger import handle_agent_trigger

        mock_resolve.return_value = 124731274

        # Build a valid agent_trigger request with chain lookup
        event = {
            "body": '{"correlation_id":"corr-1","parent_invocation_id":"inv-1","persona":"developer","target":{"repo":"aws-e/adp","issue":123},"reason":"test"}',
            "isBase64Encoded": False,
            "requestContext": {
                "identity": {"userArn": "arn:aws:sts::123:assumed-role/agent-worker/session"}
            },
        }

        with patch("github.agent_trigger._resolve_chain") as mock_chain:
            mock_chain.return_value = {
                "tenant_id": "aws-e",
                "root_human_id": "user-alice",
                "is_human_rooted": True,
                "chain_depth": 1,
            }
            with patch("common.spawn_persona.spawn_persona") as mock_spawn:
                mock_spawn.return_value = MagicMock(
                    success=True, message_id="msg-456", block_reason=None
                )
                result = handle_agent_trigger(event, None)

                call_kwargs = mock_spawn.call_args.kwargs
                assert call_kwargs["installation_id"] == 124731274

        assert result["statusCode"] == 202

    @patch("common.installation_resolver.resolve_installation_for_tenant")
    def test_unresolved_installation_returns_422(self, mock_resolve):
        """When installation_id cannot be resolved, handler returns 422."""
        from github.agent_trigger import handle_agent_trigger

        mock_resolve.return_value = None  # Resolution failed

        event = {
            "body": '{"correlation_id":"corr-1","parent_invocation_id":"inv-1","persona":"developer","target":{"repo":"aws-e/adp","issue":123},"reason":"test"}',
            "isBase64Encoded": False,
            "requestContext": {
                "identity": {"userArn": "arn:aws:sts::123:assumed-role/agent-worker/session"}
            },
        }

        with patch("github.agent_trigger._resolve_chain") as mock_chain:
            mock_chain.return_value = {
                "tenant_id": "aws-e",
                "root_human_id": "user-alice",
                "is_human_rooted": True,
                "chain_depth": 1,
            }
            result = handle_agent_trigger(event, None)

        assert result["statusCode"] == 422
        import json
        body = json.loads(result["body"])
        assert body["error"] == "no_installation_for_tenant"
