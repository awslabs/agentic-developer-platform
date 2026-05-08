"""Test installation.created event bypass (Issue #538).

Verifies that installation.created and new_permissions_accepted events
return 200 no_op without attempting identity resolution.
"""

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add parent directories to path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Set required env vars before importing handler
os.environ.setdefault("WEBHOOK_SECRET", "test-secret-123")
os.environ.setdefault("WEBHOOK_SECRET_ARN", "")
os.environ.setdefault(
    "SUBMIT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123456789/adp-dev-agent-submit.fifo"
)
os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "adp-dev-rate-limits")
os.environ.setdefault("AWS_REGION", "us-east-1")

WEBHOOK_SECRET = "test-secret-123"


def _sign_payload(payload_str: str) -> str:
    sig = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        payload_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={sig}"


def _make_event(event_type: str, payload: dict) -> dict:
    body = json.dumps(payload)
    headers = {
        "x-github-event": event_type,
        "content-type": "application/json",
        "x-hub-signature-256": _sign_payload(body),
    }
    return {
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }


class TestInstallationEventBypass:
    """Issue #538: installation.created and new_permissions_accepted bypass identity resolution."""

    @patch("handler._get_events_log")
    @patch("handler._get_signature")
    def test_installation_created_returns_200_no_op(self, mock_sig, mock_log):
        """installation.created event should return 200 without identity resolution."""
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        payload = {
            "action": "created",
            "installation": {"id": 12345, "account": {"login": "testorg"}},
            "sender": {"id": 999, "login": "installer"},
        }
        event = _make_event("installation", payload)
        result = handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "no_op"
        assert body["reason"] == "installation_event"

    @patch("handler._get_events_log")
    @patch("handler._get_signature")
    def test_installation_new_permissions_returns_200_no_op(self, mock_sig, mock_log):
        """new_permissions_accepted event should return 200 without identity resolution."""
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        payload = {
            "action": "new_permissions_accepted",
            "installation": {"id": 12345, "account": {"login": "testorg"}},
            "sender": {"id": 999, "login": "installer"},
        }
        event = _make_event("installation", payload)
        result = handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "no_op"
        assert body["reason"] == "installation_event"

    @patch("handler._get_events_log")
    @patch("handler._get_signature")
    @patch("handler._get_identity_resolver")
    def test_installation_deleted_still_resolves_identity(self, mock_resolver, mock_sig, mock_log):
        """installation.deleted should NOT bypass — still goes through identity resolution."""
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_log.return_value.log_event = MagicMock()
        # Return None to simulate no identity found
        mock_resolver.return_value.resolve.return_value = (None, "unknown_installation")

        from handler import handler

        payload = {
            "action": "deleted",
            "installation": {"id": 12345, "account": {"login": "testorg"}},
            "sender": {"id": 999, "login": "installer"},
            "repository": {"full_name": "testorg/repo"},
        }
        event = _make_event("installation", payload)

        # Mock metrics to avoid hitting CloudWatch
        with patch("handler._get_metrics") as mock_metrics:
            mock_metrics.return_value.record_rejected = MagicMock()
            mock_metrics.return_value.flush = MagicMock()
            result = handler(event, None)

        # Should proceed to identity resolution, which fails → 403
        assert result["statusCode"] == 403
