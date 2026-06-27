"""Tests for shape-based routing in handler.py — Issue #2154.

Verifies that the top-level handler() correctly routes EventBridge events
to the EventBridge handler and keeps API Gateway events on the existing path.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent directories to path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

os.environ.setdefault("WEBHOOK_SECRET", "test-secret-123")
os.environ.setdefault("WEBHOOK_SECRET_ARN", "")
os.environ.setdefault("SUBMIT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test.fifo")
os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "adp-dev-rate-limits")
os.environ.setdefault("AWS_REGION", "us-east-1")


class TestShapeBasedRouting:
    """Verify handler() dispatches correctly based on event shape."""

    @patch("eventbridge.handler.handle_eventbridge")
    def test_eventbridge_event_routed_to_eventbridge_handler(self, mock_eb_handler):
        """Events with source+detail-type+detail (no headers) go to EventBridge handler."""
        from handler import handler

        mock_eb_handler.return_value = {"statusCode": 202, "body": "{}"}

        event = {
            "source": "aws.cloudwatch",
            "detail-type": "CloudWatch Alarm State Change",
            "detail": {
                "adp_trigger": {
                    "persona": "operations",
                    "service_identity": "eventbridge:test-rule",
                    "reason": "test",
                }
            },
        }

        result = handler(event, None)

        mock_eb_handler.assert_called_once_with(event, None)
        assert result["statusCode"] == 202

    @patch("handler._get_events_log")
    @patch("handler._get_signature")
    def test_api_gateway_event_stays_on_github_path(self, mock_sig, mock_log):
        """Events with headers stay on the GitHub webhook path."""
        from handler import handler

        mock_sig.return_value.verify_github_signature.return_value = False
        mock_log.return_value.log_event = MagicMock()

        event = {
            "headers": {"x-github-event": "issues", "x-hub-signature-256": "sha256=bad"},
            "body": "{}",
            "isBase64Encoded": False,
        }

        result = handler(event, None)

        # Should hit signature validation (401) on the GitHub path
        assert result["statusCode"] == 401

    @patch("eventbridge.handler.handle_eventbridge")
    def test_event_with_both_headers_and_source_goes_to_github(self, mock_eb_handler):
        """An event with BOTH headers and source/detail is API GW, not EventBridge."""
        from handler import handler

        # This shouldn't reach the EventBridge handler
        event = {
            "headers": {"x-github-event": "issues"},
            "body": "{}",
            "isBase64Encoded": False,
            "source": "something",
            "detail-type": "test",
            "detail": {},
        }

        with (
            patch("handler._get_events_log") as mock_log,
            patch("handler._get_signature") as mock_sig,
        ):
            mock_sig.return_value.verify_github_signature.return_value = False
            mock_log.return_value.log_event = MagicMock()
            result = handler(event, None)

        mock_eb_handler.assert_not_called()
        # Should go through GitHub path (401 because bad sig)
        assert result["statusCode"] == 401

    @patch("eventbridge.handler.handle_eventbridge")
    def test_scheduled_rule_event_routed_to_eventbridge(self, mock_eb_handler):
        """Scheduled rule events are correctly identified as EventBridge."""
        from handler import handler

        mock_eb_handler.return_value = {"statusCode": 202, "body": "{}"}

        event = {
            "source": "aws.events",
            "detail-type": "Scheduled Event",
            "detail": {
                "adp_trigger": {
                    "persona": "operations",
                    "service_identity": "eventbridge:daily-audit",
                    "reason": "Scheduled daily audit",
                }
            },
        }

        handler(event, None)

        mock_eb_handler.assert_called_once_with(event, None)
