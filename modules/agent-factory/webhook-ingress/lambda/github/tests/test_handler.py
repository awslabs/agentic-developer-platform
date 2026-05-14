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
# Use WEBHOOK_SECRET (fallback path) for testing without Secrets Manager
os.environ.setdefault("WEBHOOK_SECRET", "test-secret-123")
os.environ.setdefault("WEBHOOK_SECRET_ARN", "")  # Empty = use WEBHOOK_SECRET fallback
os.environ.setdefault(
    "SUBMIT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123456789/adp-dev-agent-submit.fifo"
)
os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "adp-dev-rate-limits")
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


def _mock_resolved_identity(tenant_id="acme", user_id="u_test1"):
    """Create a mock ResolvedIdentity for tests that need a resolved identity."""
    from common.identity_resolver import ResolvedIdentity

    return ResolvedIdentity(
        tenant_id=tenant_id,
        org_id=tenant_id,
        user_id=user_id,
        user_provisioning_mode="strict",
    )


def _mock_rate_result(allowed=True, retry_after=0):
    """Create a mock rate limit result."""
    mock = MagicMock()
    mock.allowed = allowed
    mock.retry_after_seconds = retry_after
    return mock


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
    @patch("handler._get_identity_resolver")
    @patch("handler._get_signature")
    def test_unknown_installation_returns_403(self, mock_sig, mock_resolver, mock_log):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (None, "unknown_installation")
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

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "unknown_identity"
        assert body["outcome"] == "unknown_installation"


class TestRateLimiting:
    @patch("handler._get_events_log")
    @patch("handler._get_rate_limiter")
    @patch("handler._get_identity_resolver")
    @patch("handler._get_signature")
    def test_rate_limited_returns_429(self, mock_sig, mock_resolver, mock_rate, mock_log):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (
            _mock_resolved_identity(), "ok"
        )
        mock_rate.return_value.check_and_increment.return_value = _mock_rate_result(
            allowed=False, retry_after=45
        )
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
    @patch("handler._get_rate_limiter")
    @patch("handler._get_identity_resolver")
    @patch("handler._get_signature")
    def test_no_actionable_intent_returns_200_noop(
        self, mock_sig, mock_resolver, mock_rate, mock_log
    ):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (
            _mock_resolved_identity(), "ok"
        )
        mock_rate.return_value.check_and_increment.return_value = _mock_rate_result()
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
    @patch("handler._get_rate_limiter")
    @patch("handler._get_identity_resolver")
    @patch("handler._get_signature")
    def test_labeled_issue_publishes_envelope(
        self, mock_sig, mock_resolver, mock_rate, mock_sqs, mock_log
    ):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (
            _mock_resolved_identity("acme", "u_jane"), "ok"
        )
        mock_rate.return_value.check_and_increment.return_value = _mock_rate_result()
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

        assert result["statusCode"] == 202
        body = json.loads(result["body"])
        assert body["status"] == "accepted"
        assert body["message_id"] == "msg-id-123"

        # Verify envelope structure
        envelope = mock_sqs.return_value.publish_envelope.call_args[0][0]
        assert envelope["version"] == "1.0"
        assert envelope["channel"] == "github"
        assert envelope["tenant_id"] == "acme"
        assert envelope["persona"] == "developer"
        assert envelope["actor"]["user_id"] == "u_jane"
        assert envelope["actor"]["org_id"] == "acme"
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
    @patch("handler._get_rate_limiter")
    @patch("handler._get_identity_resolver")
    @patch("handler._get_signature")
    def test_pr_opened_publishes_reviewer_envelope(
        self, mock_sig, mock_resolver, mock_rate, mock_sqs, mock_log
    ):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (
            _mock_resolved_identity("acme", "u_bob"), "ok"
        )
        mock_rate.return_value.check_and_increment.return_value = _mock_rate_result()
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

        assert result["statusCode"] == 202
        envelope = mock_sqs.return_value.publish_envelope.call_args[0][0]
        assert envelope["persona"] == "reviewer"
        assert envelope["source_ref"]["pr"] == 15
        assert envelope["source_ref"]["sha"] == "abc123"
        assert envelope["intent"]["trigger"] == "pr_opened"

    @patch("handler._get_events_log")
    @patch("handler._get_sqs_publisher")
    @patch("handler._get_rate_limiter")
    @patch("handler._get_identity_resolver")
    @patch("handler._get_signature")
    def test_sqs_publish_failure_returns_500(
        self, mock_sig, mock_resolver, mock_rate, mock_sqs, mock_log
    ):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (
            _mock_resolved_identity(), "ok"
        )
        mock_rate.return_value.check_and_increment.return_value = _mock_rate_result()
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


class TestSecretResolution:
    """Verify handler resolves webhook secret from WEBHOOK_SECRET_ARN."""

    def setup_method(self):
        """Reset the cached secret between tests."""
        import handler

        handler._webhook_secret = None

    @patch("handler._get_events_log")
    @patch("handler._get_secrets")
    @patch("handler._get_signature")
    def test_resolves_secret_from_arn(self, mock_sig, mock_secrets, mock_log):
        """When WEBHOOK_SECRET_ARN is set, handler fetches secret from Secrets Manager."""
        mock_sig.return_value.verify_github_signature.return_value = False
        mock_secrets.return_value.get_secret.return_value = "resolved-secret"
        mock_log.return_value.log_event = MagicMock()

        import handler

        original_arn = handler.WEBHOOK_SECRET_ARN
        handler.WEBHOOK_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123:secret:test"
        try:
            from handler import handler as h

            event = _make_event("issues", {"action": "labeled"}, signed=False)
            event["headers"]["x-hub-signature-256"] = "sha256=invalid"
            h(event, None)

            # verify_github_signature was called with the resolved secret
            mock_sig.return_value.verify_github_signature.assert_called_once()
            call_args = mock_sig.return_value.verify_github_signature.call_args
            assert call_args[0][2] == "resolved-secret"
        finally:
            handler.WEBHOOK_SECRET_ARN = original_arn
            handler._webhook_secret = None

    @patch("handler._get_events_log")
    @patch("handler._get_signature")
    def test_falls_back_to_env_var_when_arn_empty(self, mock_sig, mock_log):
        """When WEBHOOK_SECRET_ARN is empty, handler uses WEBHOOK_SECRET env var."""
        mock_sig.return_value.verify_github_signature.return_value = False
        mock_log.return_value.log_event = MagicMock()

        import handler

        handler.WEBHOOK_SECRET_ARN = ""
        handler._webhook_secret = None
        try:
            from handler import handler as h

            event = _make_event("issues", {"action": "labeled"}, signed=False)
            event["headers"]["x-hub-signature-256"] = "sha256=invalid"
            h(event, None)

            call_args = mock_sig.return_value.verify_github_signature.call_args
            assert call_args[0][2] == "test-secret-123"
        finally:
            handler.WEBHOOK_SECRET_ARN = ""
            handler._webhook_secret = None


class TestMessageIdOnEnvelope:
    """Verify handler sets a unique message_id UUID on the envelope before publish."""

    @patch("handler._get_events_log")
    @patch("handler._get_sqs_publisher")
    @patch("handler._get_rate_limiter")
    @patch("handler._get_identity_resolver")
    @patch("handler._get_signature")
    def test_handler_sets_message_id_on_envelope(
        self, mock_sig, mock_resolver, mock_rate, mock_sqs, mock_log
    ):
        """Envelope passed to publish_envelope contains a valid UUID4 message_id."""
        import uuid as uuid_mod

        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (
            _mock_resolved_identity(), "ok"
        )
        mock_rate.return_value.check_and_increment.return_value = _mock_rate_result()
        mock_sqs.return_value.publish_envelope.return_value = "msg-id-100"
        mock_log.return_value.log_event = MagicMock()

        known_uuid = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
        with patch("handler.uuid.uuid4", return_value=uuid_mod.UUID(known_uuid)):
            from handler import handler

            payload = {
                "action": "labeled",
                "label": {"name": "developer"},
                "issue": {"number": 10},
                "repository": {"full_name": "acme/repo"},
                "sender": {"login": "user", "id": 1, "type": "User"},
                "installation": {"id": 123},
            }
            event = _make_event("issues", payload)
            result = handler(event, None)

        assert result["statusCode"] == 202
        envelope = mock_sqs.return_value.publish_envelope.call_args[0][0]
        assert envelope["message_id"] == known_uuid

    @patch("handler._get_events_log")
    @patch("handler._get_sqs_publisher")
    @patch("handler._get_rate_limiter")
    @patch("handler._get_identity_resolver")
    @patch("handler._get_signature")
    def test_two_dispatches_get_distinct_message_ids(
        self, mock_sig, mock_resolver, mock_rate, mock_sqs, mock_log
    ):
        """Two consecutive dispatches produce different message_id UUIDs."""
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (
            _mock_resolved_identity(), "ok"
        )
        mock_rate.return_value.check_and_increment.return_value = _mock_rate_result()
        mock_sqs.return_value.publish_envelope.return_value = "msg-id-200"
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        payload = {
            "action": "labeled",
            "label": {"name": "developer"},
            "issue": {"number": 10},
            "repository": {"full_name": "acme/repo"},
            "sender": {"login": "user", "id": 1, "type": "User"},
            "installation": {"id": 123},
        }
        event = _make_event("issues", payload)

        handler(event, None)
        handler(event, None)

        calls = mock_sqs.return_value.publish_envelope.call_args_list
        id_1 = calls[-2][0][0]["message_id"]
        id_2 = calls[-1][0][0]["message_id"]
        assert id_1 != id_2
        # Both should be valid UUID4 strings
        import uuid as uuid_mod
        uuid_mod.UUID(id_1, version=4)
        uuid_mod.UUID(id_2, version=4)


class TestBase64Body:
    @patch("handler._get_events_log")
    @patch("handler._get_sqs_publisher")
    @patch("handler._get_rate_limiter")
    @patch("handler._get_identity_resolver")
    @patch("handler._get_signature")
    def test_base64_encoded_body(self, mock_sig, mock_resolver, mock_rate, mock_sqs, mock_log):
        """API Gateway may send base64-encoded bodies."""
        import base64

        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (
            _mock_resolved_identity(), "ok"
        )
        mock_rate.return_value.check_and_increment.return_value = _mock_rate_result()
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
        assert result["statusCode"] == 202
        body = json.loads(result["body"])
        assert body["status"] == "accepted"
