"""Tests for eventbridge/handler.py — Issue #2154.

Tests the EventBridge handler's shape detection, guard enforcement,
service identity resolution, rate limiting, and root lineage creation.
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
    dedup_key="high-error-rate-alarm",
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
                "dedup_key": dedup_key,
                "target": target or {"repo": "acme/infra", "create_issue": True},
            },
            "alarmName": dedup_key,
            "state": {"value": "ALARM", "reason": reason},
        },
    }


def _mock_service_identity(
    tenant_id="acme-corp",
    org_id="acme-corp",
    allowed_personas=None,
):
    """Create a mock ServiceIdentityResult."""
    from common.service_identity import ServiceIdentityResult

    return ServiceIdentityResult(
        tenant_id=tenant_id,
        org_id=org_id,
        service_identity="eventbridge:adp-dev-alarm-state-change",
        allowed_personas=allowed_personas or [],
    )


def _mock_rate_result(allowed=True):
    """Create a mock RateLimitResult."""
    mock = MagicMock()
    mock.allowed = allowed
    mock.current_count = 5
    mock.limit = 50
    mock.retry_after_seconds = 120
    return mock


class TestEventBridgeShapeDetection:
    """Tests that the shape-based router correctly identifies EventBridge events."""

    def test_eventbridge_event_detected(self):
        """EventBridge events have source + detail-type + detail, no headers."""
        from github.handler import _is_eventbridge_event

        event = _alarm_event()
        assert _is_eventbridge_event(event) is True

    def test_api_gateway_event_not_detected(self):
        """API Gateway events have headers — not EventBridge."""
        from github.handler import _is_eventbridge_event

        event = {
            "headers": {"x-github-event": "issues"},
            "body": "{}",
            "requestContext": {},
        }
        assert _is_eventbridge_event(event) is False

    def test_event_with_headers_and_source_is_not_eventbridge(self):
        """An event with both headers and source is API GW (GitHub), not EventBridge."""
        from github.handler import _is_eventbridge_event

        event = {
            "headers": {"x-github-event": "issues"},
            "body": "{}",
            "source": "something",
            "detail-type": "something",
            "detail": {},
        }
        assert _is_eventbridge_event(event) is False

    def test_event_missing_detail_is_not_eventbridge(self):
        """Incomplete EventBridge shape (missing detail) is not matched."""
        from github.handler import _is_eventbridge_event

        event = {"source": "aws.cloudwatch", "detail-type": "Alarm"}
        assert _is_eventbridge_event(event) is False


class TestEventBridgeHandlerValidation:
    """Tests for adp_trigger validation and persona checks."""

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_missing_adp_trigger_ignored(self, mock_svc, mock_rl):
        """Events without adp_trigger are silently ignored."""
        from eventbridge.handler import handle_eventbridge

        event = {
            "source": "aws.cloudwatch",
            "detail-type": "CloudWatch Alarm State Change",
            "detail": {"alarmName": "test", "state": {"value": "ALARM"}},
        }

        result = handle_eventbridge(event, None)

        assert result["statusCode"] == 200
        assert "ignored" in result["body"]

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_missing_persona_returns_400(self, mock_svc, mock_rl):
        """adp_trigger without persona is rejected."""
        from eventbridge.handler import handle_eventbridge

        event = {
            "source": "aws.cloudwatch",
            "detail-type": "CloudWatch Alarm State Change",
            "detail": {
                "adp_trigger": {
                    "service_identity": "eventbridge:test",
                    "reason": "test",
                }
            },
        }

        result = handle_eventbridge(event, None)

        assert result["statusCode"] == 400
        assert "persona" in result["body"]

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_missing_service_identity_returns_400(self, mock_svc, mock_rl):
        """adp_trigger without service_identity is rejected."""
        from eventbridge.handler import handle_eventbridge

        event = {
            "source": "aws.cloudwatch",
            "detail-type": "CloudWatch Alarm State Change",
            "detail": {
                "adp_trigger": {
                    "persona": "operations",
                    "reason": "test",
                }
            },
        }

        result = handle_eventbridge(event, None)

        assert result["statusCode"] == 400
        assert "service_identity" in result["body"]

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_unknown_persona_returns_400(self, mock_svc, mock_rl):
        """Unknown persona is rejected before identity resolution."""
        from eventbridge.handler import handle_eventbridge

        event = _alarm_event(persona="nonexistent-persona")

        result = handle_eventbridge(event, None)

        assert result["statusCode"] == 400
        assert "Unknown persona" in result["body"]


class TestEventBridgeIdentityResolution:
    """Tests for service identity resolution and authorization."""

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_unknown_service_identity_returns_403(self, mock_svc_mod, mock_rl):
        """Unknown service identity returns 403."""
        from eventbridge.handler import handle_eventbridge

        mock_svc_mod.return_value.resolve_service_identity.return_value = (
            None,
            "unknown_service_identity",
        )

        event = _alarm_event()
        result = handle_eventbridge(event, None)

        assert result["statusCode"] == 403
        assert "unknown_service_identity" in result["body"]

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_persona_not_in_allowed_list_returns_403(self, mock_svc_mod, mock_rl):
        """Persona blocked by allowed_personas restriction."""
        from eventbridge.handler import handle_eventbridge

        identity = _mock_service_identity(allowed_personas=["developer", "architect"])
        mock_svc_mod.return_value.resolve_service_identity.return_value = (identity, "ok")

        event = _alarm_event(persona="operations")
        result = handle_eventbridge(event, None)

        assert result["statusCode"] == 403
        assert "persona_not_allowed" in result["body"]

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_persona_in_allowed_list_passes(self, mock_svc_mod, mock_rl):
        """Persona in allowed_personas passes authorization."""
        from eventbridge.handler import handle_eventbridge

        identity = _mock_service_identity(allowed_personas=["operations", "developer"])
        mock_svc_mod.return_value.resolve_service_identity.return_value = (identity, "ok")
        mock_rl.return_value.check_and_increment.return_value = _mock_rate_result(allowed=True)

        event = _alarm_event(persona="operations")

        with patch("common.spawn_persona.spawn_persona") as mock_spawn:
            mock_spawn.return_value = MagicMock(
                success=True, message_id="msg-123", block_reason=None
            )
            result = handle_eventbridge(event, None)

        assert result["statusCode"] == 202

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_empty_allowed_personas_permits_all(self, mock_svc_mod, mock_rl):
        """Empty allowed_personas means no restriction (all personas OK)."""
        from eventbridge.handler import handle_eventbridge

        identity = _mock_service_identity(allowed_personas=[])
        mock_svc_mod.return_value.resolve_service_identity.return_value = (identity, "ok")
        mock_rl.return_value.check_and_increment.return_value = _mock_rate_result(allowed=True)

        event = _alarm_event(persona="operations")

        with patch("common.spawn_persona.spawn_persona") as mock_spawn:
            mock_spawn.return_value = MagicMock(
                success=True, message_id="msg-123", block_reason=None
            )
            result = handle_eventbridge(event, None)

        assert result["statusCode"] == 202


class TestEventBridgeRateLimit:
    """Tests for per-service rate limiting."""

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_rate_limited_returns_429(self, mock_svc_mod, mock_rl):
        """Rate-limited service identity returns 429."""
        from eventbridge.handler import handle_eventbridge

        identity = _mock_service_identity()
        mock_svc_mod.return_value.resolve_service_identity.return_value = (identity, "ok")
        mock_rl.return_value.check_and_increment.return_value = _mock_rate_result(allowed=False)

        event = _alarm_event()
        result = handle_eventbridge(event, None)

        assert result["statusCode"] == 429
        assert "rate_limited" in result["body"]


class TestEventBridgeRootLineage:
    """Tests that EventBridge events create correct ROOT lineage."""

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_root_lineage_is_human_rooted_false(self, mock_svc_mod, mock_rl):
        """EventBridge spawns have is_human_rooted=false."""
        from eventbridge.handler import handle_eventbridge

        identity = _mock_service_identity()
        mock_svc_mod.return_value.resolve_service_identity.return_value = (identity, "ok")
        mock_rl.return_value.check_and_increment.return_value = _mock_rate_result(allowed=True)

        event = _alarm_event()

        with patch("common.spawn_persona.spawn_persona") as mock_spawn:
            mock_spawn.return_value = MagicMock(
                success=True, message_id="msg-123", block_reason=None
            )
            result = handle_eventbridge(event, None)

            # Verify spawn_persona was called with correct lineage
            call_kwargs = mock_spawn.call_args.kwargs
            correlation_ctx = call_kwargs["correlation_ctx"]

            assert correlation_ctx["is_human_rooted"] is False
            assert correlation_ctx["chain_depth"] == 0
            assert correlation_ctx["parent_invocation_id"] is None
            assert correlation_ctx["is_new_chain"] is True
            # root_human_id is the service identity
            assert correlation_ctx["root_human_id"] == "eventbridge:adp-dev-alarm-state-change"

        assert result["statusCode"] == 202
        import json

        body = json.loads(result["body"])
        assert body["is_human_rooted"] is False
        assert "correlation_id" in body

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_correlation_id_is_new_uuid(self, mock_svc_mod, mock_rl):
        """Each EventBridge event gets a fresh correlation_id (new chain)."""
        from eventbridge.handler import handle_eventbridge

        identity = _mock_service_identity()
        mock_svc_mod.return_value.resolve_service_identity.return_value = (identity, "ok")
        mock_rl.return_value.check_and_increment.return_value = _mock_rate_result(allowed=True)

        event = _alarm_event()

        with patch("common.spawn_persona.spawn_persona") as mock_spawn:
            mock_spawn.return_value = MagicMock(
                success=True, message_id="msg-123", block_reason=None
            )
            handle_eventbridge(event, None)

            call_kwargs = mock_spawn.call_args.kwargs
            correlation_ctx = call_kwargs["correlation_ctx"]
            # Should be a valid UUID string
            import uuid

            uuid.UUID(correlation_ctx["correlation_id"])  # Raises if invalid

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_event_type_is_eventbridge(self, mock_svc_mod, mock_rl):
        """spawn_persona is called with event_type='eventbridge'."""
        from eventbridge.handler import handle_eventbridge

        identity = _mock_service_identity()
        mock_svc_mod.return_value.resolve_service_identity.return_value = (identity, "ok")
        mock_rl.return_value.check_and_increment.return_value = _mock_rate_result(allowed=True)

        event = _alarm_event()

        with patch("common.spawn_persona.spawn_persona") as mock_spawn:
            mock_spawn.return_value = MagicMock(
                success=True, message_id="msg-123", block_reason=None
            )
            handle_eventbridge(event, None)

            call_kwargs = mock_spawn.call_args.kwargs
            assert call_kwargs["event_type"] == "eventbridge"
            assert call_kwargs["action"] == "CloudWatch Alarm State Change"
            assert call_kwargs["intent_trigger"] == "eventbridge_rule"


class TestEventBridgeDedupKey:
    """Tests for alarm dedup_key in channel_key."""

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_dedup_key_used_in_channel_key(self, mock_svc_mod, mock_rl):
        """dedup_key from adp_trigger is used in channel_key for pointer writes."""
        from eventbridge.handler import handle_eventbridge

        identity = _mock_service_identity()
        mock_svc_mod.return_value.resolve_service_identity.return_value = (identity, "ok")
        mock_rl.return_value.check_and_increment.return_value = _mock_rate_result(allowed=True)

        event = _alarm_event(dedup_key="high-error-rate-alarm")

        with patch("common.spawn_persona.spawn_persona") as mock_spawn:
            mock_spawn.return_value = MagicMock(
                success=True, message_id="msg-123", block_reason=None
            )
            handle_eventbridge(event, None)

            call_kwargs = mock_spawn.call_args.kwargs
            assert call_kwargs["channel_key"] == "eventbridge:high-error-rate-alarm"

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_fallback_channel_key_without_dedup_key(self, mock_svc_mod, mock_rl):
        """Without dedup_key, channel_key falls back to source:detail-type:identity."""
        from eventbridge.handler import handle_eventbridge

        identity = _mock_service_identity()
        mock_svc_mod.return_value.resolve_service_identity.return_value = (identity, "ok")
        mock_rl.return_value.check_and_increment.return_value = _mock_rate_result(allowed=True)

        event = _alarm_event(dedup_key="")

        with patch("common.spawn_persona.spawn_persona") as mock_spawn:
            mock_spawn.return_value = MagicMock(
                success=True, message_id="msg-123", block_reason=None
            )
            handle_eventbridge(event, None)

            call_kwargs = mock_spawn.call_args.kwargs
            expected = "eventbridge:aws.cloudwatch:CloudWatch Alarm State Change:eventbridge:adp-dev-alarm-state-change"
            assert call_kwargs["channel_key"] == expected


class TestEventBridgeSpawnFailure:
    """Tests for spawn_persona failure handling."""

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_sqs_failure_returns_500(self, mock_svc_mod, mock_rl):
        """SQS publish failure returns 500."""
        from eventbridge.handler import handle_eventbridge

        identity = _mock_service_identity()
        mock_svc_mod.return_value.resolve_service_identity.return_value = (identity, "ok")
        mock_rl.return_value.check_and_increment.return_value = _mock_rate_result(allowed=True)

        event = _alarm_event()

        with patch("common.spawn_persona.spawn_persona") as mock_spawn:
            mock_spawn.return_value = MagicMock(
                success=False, message_id=None, block_reason="sqs_publish_failed"
            )
            result = handle_eventbridge(event, None)

        assert result["statusCode"] == 500

    @patch("eventbridge.handler._get_rate_limiter")
    @patch("eventbridge.handler._get_service_identity_mod")
    def test_guard_block_returns_200_no_op(self, mock_svc_mod, mock_rl):
        """Guard blocks (e.g. depth exceeded) return 200 no_op."""
        from eventbridge.handler import handle_eventbridge

        identity = _mock_service_identity()
        mock_svc_mod.return_value.resolve_service_identity.return_value = (identity, "ok")
        mock_rl.return_value.check_and_increment.return_value = _mock_rate_result(allowed=True)

        event = _alarm_event()

        with patch("common.spawn_persona.spawn_persona") as mock_spawn:
            mock_spawn.return_value = MagicMock(
                success=False, message_id=None, block_reason="chain_depth_exceeded"
            )
            result = handle_eventbridge(event, None)

        assert result["statusCode"] == 200
        assert "chain_depth_exceeded" in result["body"]
