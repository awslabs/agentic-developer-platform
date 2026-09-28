"""Tests for min_author_association enforcement in the webhook handler.

Issue #3134: Verifies that:
- cross_tenant_denied outcome → 403 with RejectedReason metric.
- min_author_association=COLLABORATOR + payload NONE → 403 insufficient_association.
- min_author_association absent → association not checked (pass-through).
"""

import hashlib
import hmac
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add paths
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _reset_module(monkeypatch):
    """Reset module state and set base env vars."""
    monkeypatch.setenv("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
    monkeypatch.setenv("USER_IDENTITY_INDEX_TABLE", "adp-dev-user-identity-index")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("WEBHOOK_SECRET_ARN", "")
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("SUBMIT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/test-queue")
    monkeypatch.setenv("USER_IDENTITY_INDEX_V2_READ", "true")
    monkeypatch.setenv("RESOLVE_CANONICAL_VIA_GATEWAY", "false")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("INTERNAL_API_KEY_ARN", "")
    monkeypatch.setenv("BG_INTERNAL_API_KEY", "test-key")
    monkeypatch.setenv("GATEWAY_API_URL", "http://gateway.internal:8080")

    # Clear all handler-related module caches
    mods_to_clear = [
        k
        for k in sys.modules
        if k.startswith("github.handler")
        or k.startswith("common.identity_resolver")
        or k.startswith("common.gateway_client")
        or k.startswith("common.metrics")
        or k.startswith("common.signature")
        or k.startswith("common.secrets")
        or k.startswith("common.sqs_publisher")
        or k.startswith("common.rate_limit")
    ]
    for mod in mods_to_clear:
        del sys.modules[mod]
    yield
    mods_to_clear = [
        k
        for k in sys.modules
        if k.startswith("github.handler")
        or k.startswith("common.identity_resolver")
        or k.startswith("common.gateway_client")
        or k.startswith("common.metrics")
        or k.startswith("common.signature")
        or k.startswith("common.secrets")
        or k.startswith("common.sqs_publisher")
        or k.startswith("common.rate_limit")
    ]
    for mod in mods_to_clear:
        del sys.modules[mod]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

INSTALLATION_ID = 55555
SENDER_ID = 20402445


def _sign(body: bytes, secret: str = "test-secret") -> str:
    """Generate a valid GitHub HMAC signature."""
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _make_event(payload: dict, event_type: str = "issue_comment") -> dict:
    """Build an API Gateway event with proper HMAC signature."""
    body = json.dumps(payload)
    body_bytes = body.encode()
    sig = _sign(body_bytes)
    return {
        "headers": {
            "x-github-event": event_type,
            "x-hub-signature-256": sig,
            "x-github-delivery": "test-delivery-123",
            "content-type": "application/json",
        },
        "body": body,
        "isBase64Encoded": False,
    }


def _make_comment_payload(
    *,
    author_association: str = "NONE",
    body: str = "@agent-developer help",
) -> dict:
    """Build a minimal issue_comment payload."""
    return {
        "action": "created",
        "installation": {"id": INSTALLATION_ID},
        "repository": {"full_name": "test-org/test-repo"},
        "organization": {"login": "test-org"},
        "sender": {"id": SENDER_ID, "login": "testuser", "type": "User"},
        "issue": {"number": 42},
        "comment": {
            "id": 999,
            "body": body,
            "author_association": author_association,
        },
    }


# ---------------------------------------------------------------------------
# Tests: cross_tenant_denied → 403
# ---------------------------------------------------------------------------


class TestCrossTenantDeniedReturns403:
    """cross_tenant_denied outcome from resolver → 403 response."""

    def test_cross_tenant_denied_returns_403(self, monkeypatch):
        """When identity resolver returns (None, 'cross_tenant_denied'),
        the handler responds with 403 and emits the RejectedReason metric."""
        from common import identity_resolver

        mock_metrics = MagicMock()

        with patch.object(
            identity_resolver,
            "resolve",
            return_value=(None, "cross_tenant_denied"),
        ):
            with patch(
                "github.handler._get_metrics",
                return_value=mock_metrics,
            ):
                with patch("github.handler._resolve_webhook_secret", return_value="test-secret"):
                    from github import handler

                    # Reset handler module state
                    handler._identity_mod = identity_resolver
                    handler._metrics_mod = mock_metrics

                    payload = _make_comment_payload()
                    event = _make_event(payload)
                    response = handler.handler(event, {})

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["outcome"] == "cross_tenant_denied"

        # Verify rejected metric was recorded
        mock_metrics.record_rejected.assert_called_once_with(reason="cross_tenant_denied")


# ---------------------------------------------------------------------------
# Tests: min_author_association enforcement
# ---------------------------------------------------------------------------


class TestMinAuthorAssociationEnforcement:
    """min_author_association on installation row enforces author_association."""

    def test_insufficient_association_returns_403(self, monkeypatch):
        """COLLABORATOR required but commenter is NONE → 403."""
        from common import identity_resolver
        from common.identity_resolver import ResolvedIdentity

        identity_resolver._dynamodb = None
        identity_resolver._cloudwatch = None

        resolved = ResolvedIdentity(
            tenant_id="target-org",
            org_id="target-org",
            user_id="user-123",
            user_provisioning_mode="strict",
        )

        # Installation row has min_author_association=COLLABORATOR
        tenant_item = {
            "identity_type": "github_installation_id",
            "identity_value": str(INSTALLATION_ID),
            "org_id": "target-org",
            "user_provisioning_mode": "strict",
            "min_author_association": "COLLABORATOR",
        }

        def mock_ddb_get_item(items_by_table):
            mock_resource = MagicMock()

            def make_table(table_name):
                mock_table = MagicMock()

                def get_item(Key=None, **kwargs):  # noqa: N803
                    table_items = items_by_table.get(table_name, {})
                    key_str = "|".join(str(v) for v in Key.values())
                    item = table_items.get(key_str)
                    return {"Item": item} if item else {}

                mock_table.get_item = get_item
                return mock_table

            mock_resource.Table = make_table
            return mock_resource

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": tenant_item,
            },
        }
        mock_ddb = mock_ddb_get_item(ddb_items)
        mock_metrics = MagicMock()

        with patch.object(identity_resolver, "resolve", return_value=(resolved, "ok")):
            with patch("boto3.resource", return_value=mock_ddb):
                with patch("github.handler._get_metrics", return_value=mock_metrics):
                    with patch("github.handler._resolve_webhook_secret", return_value="test-secret"):
                        from github import handler

                        handler._identity_mod = identity_resolver
                        handler._metrics_mod = mock_metrics

                        payload = _make_comment_payload(author_association="NONE")
                        event = _make_event(payload)
                        response = handler.handler(event, {})

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["outcome"] == "insufficient_association"
        mock_metrics.record_rejected.assert_called_once_with(reason="insufficient_association")

    def test_sufficient_association_passes(self, monkeypatch):
        """COLLABORATOR required and commenter is MEMBER → pass (no 403)."""
        from common import identity_resolver
        from common.identity_resolver import ResolvedIdentity

        resolved = ResolvedIdentity(
            tenant_id="target-org",
            org_id="target-org",
            user_id="user-123",
            user_provisioning_mode="strict",
        )

        tenant_item = {
            "identity_type": "github_installation_id",
            "identity_value": str(INSTALLATION_ID),
            "org_id": "target-org",
            "user_provisioning_mode": "strict",
            "min_author_association": "COLLABORATOR",
        }

        def mock_ddb_get_item(items_by_table):
            mock_resource = MagicMock()

            def make_table(table_name):
                mock_table = MagicMock()

                def get_item(Key=None):  # noqa: N803
                    table_items = items_by_table.get(table_name, {})
                    key_str = "|".join(str(v) for v in Key.values())
                    item = table_items.get(key_str)
                    return {"Item": item} if item else {}

                mock_table.get_item = get_item
                return mock_table

            mock_resource.Table = make_table
            return mock_resource

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": tenant_item,
            },
        }
        mock_ddb = mock_ddb_get_item(ddb_items)
        mock_metrics = MagicMock()
        mock_rate_limiter = MagicMock()
        mock_rate_limiter.check_and_increment.return_value = MagicMock(
            allowed=False, retry_after_seconds=60
        )

        with patch.object(identity_resolver, "resolve", return_value=(resolved, "ok")):
            with patch("boto3.resource", return_value=mock_ddb):
                with patch("github.handler._get_metrics", return_value=mock_metrics):
                    with patch("github.handler._get_rate_limiter", return_value=mock_rate_limiter):
                        with patch("github.handler._resolve_webhook_secret", return_value="test-secret"):
                            from github import handler

                            handler._identity_mod = identity_resolver
                            handler._metrics_mod = mock_metrics

                            payload = _make_comment_payload(author_association="MEMBER")
                            event = _make_event(payload)
                            response = handler.handler(event, {})

        # Should NOT be 403 insufficient_association (it gets to rate limit)
        assert response["statusCode"] != 403 or "insufficient_association" not in json.loads(response["body"]).get("outcome", "")

    def test_absent_min_assoc_no_enforcement(self, monkeypatch):
        """No min_author_association on installation → no enforcement."""
        from common import identity_resolver
        from common.identity_resolver import ResolvedIdentity

        resolved = ResolvedIdentity(
            tenant_id="target-org",
            org_id="target-org",
            user_id="user-123",
            user_provisioning_mode="strict",
        )

        # No min_author_association attr
        tenant_item = {
            "identity_type": "github_installation_id",
            "identity_value": str(INSTALLATION_ID),
            "org_id": "target-org",
            "user_provisioning_mode": "strict",
        }

        def mock_ddb_get_item(items_by_table):
            mock_resource = MagicMock()

            def make_table(table_name):
                mock_table = MagicMock()

                def get_item(Key=None):  # noqa: N803
                    table_items = items_by_table.get(table_name, {})
                    key_str = "|".join(str(v) for v in Key.values())
                    item = table_items.get(key_str)
                    return {"Item": item} if item else {}

                mock_table.get_item = get_item
                return mock_table

            mock_resource.Table = make_table
            return mock_resource

        ddb_items = {
            "adp-dev-identity-index": {
                f"github_installation_id|{INSTALLATION_ID}": tenant_item,
            },
        }
        mock_ddb = mock_ddb_get_item(ddb_items)
        mock_metrics = MagicMock()
        mock_rate_limiter = MagicMock()
        mock_rate_limiter.check_and_increment.return_value = MagicMock(
            allowed=False, retry_after_seconds=60
        )

        with patch.object(identity_resolver, "resolve", return_value=(resolved, "ok")):
            with patch("boto3.resource", return_value=mock_ddb):
                with patch("github.handler._get_metrics", return_value=mock_metrics):
                    with patch("github.handler._get_rate_limiter", return_value=mock_rate_limiter):
                        with patch("github.handler._resolve_webhook_secret", return_value="test-secret"):
                            from github import handler

                            handler._identity_mod = identity_resolver
                            handler._metrics_mod = mock_metrics

                            # NONE association but no min required
                            payload = _make_comment_payload(author_association="NONE")
                            event = _make_event(payload)
                            response = handler.handler(event, {})

        # Should NOT return 403 insufficient_association
        if response["statusCode"] == 403:
            body = json.loads(response["body"])
            assert body.get("outcome") != "insufficient_association"
