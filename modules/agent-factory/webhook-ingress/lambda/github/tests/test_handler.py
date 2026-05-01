"""Tests for handler.py."""

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent directories to path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Set required env vars before importing handler
os.environ.setdefault("WEBHOOK_SECRET", "test-secret-123")
os.environ.setdefault("SUBMIT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123456789/adp-dev-agent-submit.fifo")
os.environ.setdefault("TENANTS_TABLE", "adp-tenants")
os.environ.setdefault("RATE_LIMIT_TABLE", "adp-rate-limits")
os.environ.setdefault("AWS_REGION", "us-east-1")


WEBHOOK_SECRET = "test-secret-123"


def _sign_payload(payload_str: str) -> str:
    """Generate HMAC signature for a payload string."""
    sig = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        payload_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={sig}"


def _make_event(event_type: str, payload: dict, signed: bool = True) -> dict:
    """Build an API Gateway v2 event."""
    body = json.dumps(payload)
    headers = {"x-github-event": event_type, "content-type": "application/json"}
    if signed:
        headers["x-hub-signature-256"] = _sign_payload(body)
    return {
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }


class TestSignatureValidation:
    @patch("handler._get_events_log")
    @patch("handler._get_signature")
    def test_invalid_signature_returns_401(self, mock_sig, mock_log):
        mock_sig.return_value.verify_github_signature.return_value = False
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        event = _make_event("issues", {"action": "labeled"}, signed=False)
        event["headers"]["x-hub-signature-256"] = "sha256=invalid"

        result = handler(event, None)
        assert result["statusCode"] == 401

    @patch("handler._get_events_log")
    @patch("handler._get_signature")
    def test_missing_event_header_returns_400(self, mock_sig, mock_log):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        event = _make_event("issues", {"action": "labeled"})
        event["headers"].pop("x-github-event")

        result = handler(event, None)
        assert result["statusCode"] == 400


class TestTenantResolution:
    @patch("handler._get_events_log")
    @patch("handler._get_tenant_resolver")
    @patch("handler._get_signature")
    def test_unknown_installation_returns_200_ignored(self, mock_sig, mock_tenant, mock_log):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_tenant.return_value.resolve_tenant.return_value = None
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        payload = {
            "action": "labeled",
            "label": {"name": "developer"},
            "issue": {"number": 1},
            "repository": {"full_name": "unknown-org/repo"},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 999999},
        }
        event = _make_event("issues", payload)
        result = handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "ignored"
        assert body["reason"] == "unknown_installation"


class TestRateLimiting:
    @patch("handler._get_events_log")
    @patch("handler._get_rate_limit")
    @patch("handler._get_tenant_resolver")
    @patch("handler._get_signature")
    def test_rate_limited_returns_429(self, mock_sig, mock_tenant, mock_rate, mock_log):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_tenant.return_value.resolve_tenant.return_value = {"tenant_id": "acme"}
        mock_rate.return_value.check_rate_limit.return_value = (False, 45)
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        payload = {
            "action": "labeled",
            "label": {"name": "developer"},
            "issue": {"number": 1},
            "repository": {"full_name": "acme/repo"},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        event = _make_event("issues", payload)
        result = handler(event, None)

        assert result["statusCode"] == 429
        assert result["headers"]["Retry-After"] == "45"


class TestIntentParsing:
    @patch("handler._get_events_log")
    @patch("handler._get_rate_limit")
    @patch("handler._get_tenant_resolver")
    @patch("handler._get_signature")
    def test_no_actionable_intent_returns_200_noop(self, mock_sig, mock_tenant, mock_rate, mock_log):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_tenant.return_value.resolve_tenant.return_value = {"tenant_id": "acme"}
        mock_rate.return_value.check_rate_limit.return_value = (True, 0)
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        # issues.opened has no intent mapping
        payload = {
            "action": "opened",
            "issue": {"number": 1},
            "repository": {"full_name": "acme/repo"},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        event = _make_event("issues", payload)
        result = handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "no_op"


class TestSuccessfulPublish:
    @patch("handler._get_events_log")
    @patch("handler._get_sqs_publisher")
    @patch("handler._get_rate_limit")
    @patch("handler._get_tenant_resolver")
    @patch("handler._get_signature")
    def test_labeled_issue_publishes_envelope(self, mock_sig, mock_tenant, mock_rate, mock_sqs, mock_log):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_tenant.return_value.resolve_tenant.return_value = {"tenant_id": "acme"}
        mock_rate.return_value.check_rate_limit.return_value = (True, 0)
        mock_sqs.return_value.publish_envelope.return_value = "msg-id-123"
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        payload = {
            "action": "labeled",
            "label": {"name": "developer"},
            "issue": {"number": 42},
            "repository": {"full_name": "acme/flagship-app"},
            "sender": {"login": "jane", "id": 999, "type": "User"},
            "installation": {"id": 99887766},
        }
        event = _make_event("issues", payload)
        result = handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "accepted"
        assert body["message_id"] == "msg-id-123"

        # Verify envelope structure
        envelope = mock_sqs.return_value.publish_envelope.call_args[0][0]
        assert envelope["version"] == "1.0"
        assert envelope["channel"] == "github"
        assert envelope["tenant_id"] == "acme"
        assert envelope["persona"] == "developer"
        assert envelope["actor"]["github_login"] == "jane"
        assert envelope["actor"]["github_id"] == 999
        assert envelope["actor"]["is_bot"] is False
        assert envelope["source_ref"]["installation_id"] == 99887766
        assert envelope["source_ref"]["repo"] == "acme/flagship-app"
        assert envelope["source_ref"]["issue"] == 42
        assert envelope["intent"]["trigger"] == "issue_labeled"
        assert envelope["intent"]["label"] == "developer"
        assert envelope["intent"]["persona"] == "developer"

    @patch("handler._get_events_log")
    @patch("handler._get_sqs_publisher")
    @patch("handler._get_rate_limit")
    @patch("handler._get_tenant_resolver")
    @patch("handler._get_signature")
    def test_pr_opened_publishes_reviewer_envelope(self, mock_sig, mock_tenant, mock_rate, mock_sqs, mock_log):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_tenant.return_value.resolve_tenant.return_value = {"tenant_id": "acme"}
        mock_rate.return_value.check_rate_limit.return_value = (True, 0)
        mock_sqs.return_value.publish_envelope.return_value = "msg-id-456"
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        payload = {
            "action": "opened",
            "pull_request": {"number": 15, "head": {"sha": "abc123"}},
            "repository": {"full_name": "acme/flagship-app"},
            "sender": {"login": "bob", "id": 1001, "type": "User"},
            "installation": {"id": 99887766},
        }
        event = _make_event("pull_request", payload)
        result = handler(event, None)

        assert result["statusCode"] == 200
        envelope = mock_sqs.return_value.publish_envelope.call_args[0][0]
        assert envelope["persona"] == "reviewer"
        assert envelope["source_ref"]["pr"] == 15
        assert envelope["source_ref"]["sha"] == "abc123"
        assert envelope["intent"]["trigger"] == "pr_opened"

    @patch("handler._get_events_log")
    @patch("handler._get_sqs_publisher")
    @patch("handler._get_rate_limit")
    @patch("handler._get_tenant_resolver")
    @patch("handler._get_signature")
    def test_sqs_publish_failure_returns_500(self, mock_sig, mock_tenant, mock_rate, mock_sqs, mock_log):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_tenant.return_value.resolve_tenant.return_value = {"tenant_id": "acme"}
        mock_rate.return_value.check_rate_limit.return_value = (True, 0)
        mock_sqs.return_value.publish_envelope.return_value = None  # failure
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        payload = {
            "action": "labeled",
            "label": {"name": "developer"},
            "issue": {"number": 1},
            "repository": {"full_name": "acme/repo"},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        event = _make_event("issues", payload)
        result = handler(event, None)

        assert result["statusCode"] == 500


class TestBase64Body:
    @patch("handler._get_events_log")
    @patch("handler._get_sqs_publisher")
    @patch("handler._get_rate_limit")
    @patch("handler._get_tenant_resolver")
    @patch("handler._get_signature")
    def test_base64_encoded_body(self, mock_sig, mock_tenant, mock_rate, mock_sqs, mock_log):
        """API Gateway may send base64-encoded bodies."""
        import base64

        mock_sig.return_value.verify_github_signature.return_value = True
        mock_tenant.return_value.resolve_tenant.return_value = {"tenant_id": "acme"}
        mock_rate.return_value.check_rate_limit.return_value = (True, 0)
        mock_sqs.return_value.publish_envelope.return_value = "msg-id-789"
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        payload = {
            "action": "labeled",
            "label": {"name": "pm"},
            "issue": {"number": 5},
            "repository": {"full_name": "acme/repo"},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        body_str = json.dumps(payload)
        event = {
            "headers": {
                "x-github-event": "issues",
                "x-hub-signature-256": "sha256=abc",
                "content-type": "application/json",
            },
            "body": base64.b64encode(body_str.encode()).decode(),
            "isBase64Encoded": True,
        }

        result = handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "accepted"
