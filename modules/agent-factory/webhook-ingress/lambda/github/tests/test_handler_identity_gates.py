"""Tests for identity-gated webhook handler (Issue #402).

Covers the 4 acceptance criteria scenarios:
1. Unknown installation → 403 {"outcome":"unknown_installation"}
2. Known installation, unknown sender → 403 {"outcome":"unknown_user"}
3. Known installation, sender in different org → 403 {"outcome":"cross_tenant_identity"}
4. Known installation + known sender (same org) → 202 accepted with full actor fields
5. Auto-provision path: unknown sender + auto_provision mode → 202 after provision
"""

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
os.environ.setdefault("WEBHOOK_SECRET_ARN", "")
os.environ.setdefault(
    "SUBMIT_QUEUE_URL",
    "https://sqs.us-east-1.amazonaws.com/123456789/adp-dev-agent-submit.fifo",
)
os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "adp-dev-rate-limits")
os.environ.setdefault("AWS_REGION", "us-east-1")


def _make_event(event_type: str, payload: dict) -> dict:
    """Build an API Gateway v2 event (signature validation mocked out)."""
    body = json.dumps(payload)
    return {
        "headers": {
            "x-github-event": event_type,
            "x-hub-signature-256": "sha256=mocked",
            "content-type": "application/json",
        },
        "body": body,
        "isBase64Encoded": False,
    }


def _labeled_payload(
    installation_id: int = 12345,
    sender_id: int = 999,
    sender_login: str = "alice",
) -> dict:
    """Standard issue-labeled payload."""
    return {
        "action": "labeled",
        "label": {"name": "developer"},
        "issue": {"number": 42},
        "repository": {"full_name": "acme/repo"},
        "sender": {"login": sender_login, "id": sender_id, "type": "User"},
        "installation": {"id": installation_id},
    }


class TestUnknownInstallation:
    """Webhook from an installation not in identity-index — behavior depends
    on whether the webhook carries an org login (auto-heal) or not (403)."""

    @patch("handler._get_events_log")
    @patch("handler._get_identity_resolver")
    @patch("handler._get_signature")
    def test_unknown_installation_no_org_login_returns_403(self, mock_sig, mock_resolver, mock_log):
        """If the webhook has no org/owner login to derive tenant from, 403."""
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (None, "unknown_installation")
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        # _labeled_payload has no `repository.owner.login` or `organization`
        event = _make_event("issues", _labeled_payload(installation_id=999999))
        result = handler(event, None)

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "unknown_identity"
        assert body["outcome"] == "unknown_installation"

    @patch("handler._get_events_log")
    @patch("handler._auto_register_installation")
    @patch("handler._get_identity_resolver")
    @patch("handler._get_signature")
    def test_unknown_installation_with_org_auto_registers_and_retries(
        self, mock_sig, mock_resolver, mock_auto_reg, mock_log
    ):
        """If the webhook has an org login, auto-register and retry resolve."""
        from common.identity_resolver import ResolvedIdentity

        mock_sig.return_value.verify_github_signature.return_value = True
        # First resolve: unknown_installation. After auto-register + retry: ok.
        mock_resolver.return_value.resolve.side_effect = [
            (None, "unknown_installation"),
            (
                ResolvedIdentity(
                    tenant_id="sophos-hackathon",
                    org_id="sophos-hackathon",
                    user_id="u_xyz",
                    user_provisioning_mode="strict",
                ),
                "ok",
            ),
        ]
        mock_auto_reg.return_value = "sophos-hackathon"
        mock_log.return_value.log_event = MagicMock()

        # Patch downstream dispatch bits so the handler can complete
        with (
            patch("handler._get_rate_limiter") as mock_rate,
            patch("common.sqs_publisher.publish_envelope") as mock_sqs_pub,
            patch("common.spawn_persona._write_pointer_and_provenance"),
            patch("common.spawn_persona._capture_invocation_event"),
        ):
            mock_rate.return_value.check_and_increment.return_value = MagicMock(
                allowed=True, retry_after_seconds=0
            )
            mock_sqs_pub.return_value = "msg-heal-1"

            from handler import handler

            # Payload carries the org login via repository.owner.login
            payload = _labeled_payload(installation_id=888888)
            payload["repository"]["owner"] = {"login": "sophos-hackathon"}
            event = _make_event("issues", payload)
            result = handler(event, None)

        assert result["statusCode"] == 202
        mock_auto_reg.assert_called_once_with(888888, "sophos-hackathon")
        assert mock_resolver.return_value.resolve.call_count == 2


class TestUnknownUser:
    """Webhook from known installation but unknown sender → 403."""

    @patch("handler._get_events_log")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    @patch("handler._get_signature")
    def test_unknown_user_returns_403(self, mock_sig, mock_resolver, mock_gw_client, mock_log):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (None, "unknown_user")
        # Simulate _get_table for auto-provision check — strict mode
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {
                "identity_type": "github_installation_id",
                "identity_value": "12345",
                "org_id": "acme",
                "user_provisioning_mode": "strict",
            }
        }
        mock_resolver.return_value._get_table.return_value = mock_table
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        event = _make_event("issues", _labeled_payload())
        result = handler(event, None)

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "unknown_identity"
        assert body["outcome"] == "unknown_user"


class TestCrossTenantIdentity:
    """Cross-tenant policy: if the resolver returns 'cross_tenant_identity'
    anyway (e.g. in a future stricter mode gated by config), the handler
    must still reject it with 403. The current resolver accepts cross-tenant
    and returns 'ok' — see test_cross_tenant_returns_ok in the resolver tests."""

    @patch("handler._get_events_log")
    @patch("handler._get_identity_resolver")
    @patch("handler._get_signature")
    def test_cross_tenant_outcome_from_resolver_still_403s(self, mock_sig, mock_resolver, mock_log):
        # This keeps the handler-level contract intact: if something upstream
        # ever decides to hard-block cross-tenant again, the handler will 403.
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (
            None,
            "cross_tenant_identity",
        )
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        event = _make_event("issues", _labeled_payload())
        result = handler(event, None)

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "unknown_identity"
        assert body["outcome"] == "cross_tenant_identity"


class TestSuccessfulIdentityResolution:
    """Known installation + known sender (same org) → 202 with full actor."""

    @patch("handler._get_events_log")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.sqs_publisher.publish_envelope")
    @patch("handler._get_rate_limiter")
    @patch("handler._get_identity_resolver")
    @patch("handler._get_signature")
    def test_known_identity_returns_202_with_actor(
        self, mock_sig, mock_resolver, mock_rate, mock_sqs, mock_write, mock_capture, mock_log
    ):
        from common.identity_resolver import ResolvedIdentity

        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (
            ResolvedIdentity(
                tenant_id="acme",
                org_id="acme",
                user_id="u_abc123",
                user_provisioning_mode="strict",
            ),
            "ok",
        )
        mock_rate.return_value.check_and_increment.return_value = MagicMock(
            allowed=True, retry_after_seconds=0
        )
        mock_sqs.return_value = "msg-id-202"
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        event = _make_event("issues", _labeled_payload())
        result = handler(event, None)

        assert result["statusCode"] == 202
        body = json.loads(result["body"])
        assert body["status"] == "accepted"

        # Verify envelope actor has user_id and org_id
        envelope = mock_sqs.call_args[0][0]
        assert envelope["actor"]["user_id"] == "u_abc123"
        assert envelope["actor"]["org_id"] == "acme"
        assert envelope["actor"]["github_id"] == 999
        assert envelope["actor"]["github_login"] == "alice"
        assert envelope["actor"]["is_bot"] is False


class TestAutoProvision:
    """Auto-provision: unknown sender + auto_provision mode → 202 after provision."""

    @patch("handler._get_events_log")
    @patch("common.spawn_persona._capture_invocation_event")
    @patch("common.spawn_persona._write_pointer_and_provenance")
    @patch("common.sqs_publisher.publish_envelope")
    @patch("handler._get_rate_limiter")
    @patch("handler._get_gateway_client")
    @patch("handler._get_identity_resolver")
    @patch("handler._get_signature")
    def test_auto_provision_creates_user_and_accepts(
        self, mock_sig, mock_resolver, mock_gw_client, mock_rate, mock_sqs,
        mock_write, mock_capture, mock_log
    ):
        from common.identity_resolver import ResolvedIdentity

        mock_sig.return_value.verify_github_signature.return_value = True

        # First call: unknown_user; second call (after provision): resolved
        resolved_identity = ResolvedIdentity(
            tenant_id="acme",
            org_id="acme",
            user_id="u_new_user",
            user_provisioning_mode="auto_provision",
        )
        mock_resolver.return_value.resolve.side_effect = [
            (None, "unknown_user"),
            (resolved_identity, "ok"),
        ]

        # Mock _get_table for the auto-provision check
        mock_table = MagicMock()
        mock_table.get_item.return_value = {
            "Item": {
                "identity_type": "github_installation_id",
                "identity_value": "12345",
                "org_id": "acme",
                "user_provisioning_mode": "auto_provision",
            }
        }
        mock_resolver.return_value._get_table.return_value = mock_table

        mock_gw_client.return_value.auto_provision_user.return_value = True
        mock_rate.return_value.check_and_increment.return_value = MagicMock(
            allowed=True, retry_after_seconds=0
        )
        mock_sqs.return_value = "msg-id-auto"
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        event = _make_event("issues", _labeled_payload())
        result = handler(event, None)

        assert result["statusCode"] == 202
        body = json.loads(result["body"])
        assert body["status"] == "accepted"

        # Verify auto_provision_user was called
        mock_gw_client.return_value.auto_provision_user.assert_called_once_with(
            org_id="acme",
            github_id=999,
            github_login="alice",
        )

        # Verify envelope has provisioned user's identity
        envelope = mock_sqs.call_args[0][0]
        assert envelope["actor"]["user_id"] == "u_new_user"
        assert envelope["actor"]["org_id"] == "acme"
