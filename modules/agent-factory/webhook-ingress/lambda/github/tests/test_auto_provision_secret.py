"""Tests for auto-provisioning per-tenant GitHub App SM secret (Issue #593).

Validates that when auto-register writes a DDB row, the lambda also creates
a per-tenant SM secret with the platform App credentials — and handles all
failure modes gracefully.
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
    "SUBMIT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123456789/adp-dev-agent-submit.fifo"
)
os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "adp-dev-rate-limits")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("ENVIRONMENT", "dev")


class TestAutoProvisionTenantSecret:
    """Tests for _auto_provision_tenant_github_app_secret."""

    @patch("handler._get_sm_client")
    @patch("handler._emit_metric")
    def test_creates_secret_with_correct_payload_and_tags(self, mock_metric, mock_sm):
        """When SM secret doesn't exist, create_secret is called with correct args."""
        from handler import _auto_provision_tenant_github_app_secret

        sm_client = MagicMock()
        mock_sm.return_value = sm_client

        # Mock reading platform secrets
        sm_client.get_secret_value.side_effect = [
            {"SecretString": "123456"},  # app_id
            {"SecretString": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----"},  # key
        ]
        # Mock create_secret success
        sm_client.create_secret.return_value = {"ARN": "arn:aws:secretsmanager:us-east-1:123:secret:adp/dev/tenants/testorg/github-app-AbCdEf"}

        _auto_provision_tenant_github_app_secret("testorg", 12345)

        # Verify platform secrets were read
        sm_client.get_secret_value.assert_any_call(
            SecretId="adp/dev/github-app/adp-agent-platform-id"
        )
        sm_client.get_secret_value.assert_any_call(
            SecretId="adp/dev/github-app/adp-agent-platform-key"
        )

        # Verify create_secret called with correct args
        sm_client.create_secret.assert_called_once()
        create_kwargs = sm_client.create_secret.call_args[1]
        assert create_kwargs["Name"] == "adp/dev/tenants/testorg/github-app"
        assert "auto-provisioned via webhook auto-register" in create_kwargs["Description"]

        # Verify payload structure matches what worker expects
        payload = json.loads(create_kwargs["SecretString"])
        assert payload["app_id"] == "123456"
        assert "BEGIN RSA PRIVATE KEY" in payload["private_key"]

        # Verify tags
        tags = create_kwargs["Tags"]
        tag_dict = {t["Key"]: t["Value"] for t in tags}
        assert tag_dict["ManagedBy"] == "auto-register"
        assert tag_dict["Tenant"] == "testorg"
        assert tag_dict["InstallationId"] == "12345"

        # No error metrics emitted
        mock_metric.assert_not_called()

    @patch("handler._get_sm_client")
    @patch("handler._emit_metric")
    def test_resource_exists_exception_is_swallowed(self, mock_metric, mock_sm):
        """When per-tenant SM secret already exists, log and return without error."""
        from handler import _auto_provision_tenant_github_app_secret

        sm_client = MagicMock()
        mock_sm.return_value = sm_client

        sm_client.get_secret_value.side_effect = [
            {"SecretString": "123456"},
            {"SecretString": "fake-key"},
        ]
        # Simulate ResourceExistsException
        sm_client.exceptions.ResourceExistsException = type(
            "ResourceExistsException", (Exception,), {}
        )
        sm_client.create_secret.side_effect = sm_client.exceptions.ResourceExistsException(
            "already exists"
        )

        # Should not raise
        _auto_provision_tenant_github_app_secret("existing-tenant", 99999)

        # No error metric emitted for ResourceExistsException
        mock_metric.assert_not_called()

    @patch("handler._get_sm_client")
    @patch("handler._emit_metric")
    def test_platform_secret_read_failure_emits_metric(self, mock_metric, mock_sm):
        """When reading platform App secrets fails, emit metric and return."""
        from handler import _auto_provision_tenant_github_app_secret

        sm_client = MagicMock()
        mock_sm.return_value = sm_client

        sm_client.get_secret_value.side_effect = Exception("AccessDeniedException")

        _auto_provision_tenant_github_app_secret("broken-tenant", 111)

        # create_secret should NOT be called
        sm_client.create_secret.assert_not_called()

        # Metric emitted
        mock_metric.assert_called_once_with("AutoRegister.PlatformSecretReadFailed")

    @patch("handler._get_sm_client")
    @patch("handler._emit_metric")
    def test_create_secret_failure_emits_metric(self, mock_metric, mock_sm):
        """When CreateSecret fails (non-ResourceExists), emit metric and return."""
        from handler import _auto_provision_tenant_github_app_secret

        sm_client = MagicMock()
        mock_sm.return_value = sm_client

        sm_client.get_secret_value.side_effect = [
            {"SecretString": "123456"},
            {"SecretString": "fake-key"},
        ]
        # Simulate a generic failure (not ResourceExistsException)
        sm_client.exceptions.ResourceExistsException = type(
            "ResourceExistsException", (Exception,), {}
        )
        sm_client.create_secret.side_effect = RuntimeError("ThrottlingException")

        _auto_provision_tenant_github_app_secret("throttled-tenant", 222)

        mock_metric.assert_called_once_with("AutoRegister.SecretCreationFailed")

    def test_lazy_sm_client_returns_same_instance(self):
        """_get_sm_client returns the same client on repeated calls."""
        import handler

        # Reset cached client
        handler._sm_client = None

        with patch.dict("sys.modules", {"boto3": MagicMock()}) as _:
            import boto3
            mock_client = MagicMock()
            boto3.client = MagicMock(return_value=mock_client)
            # Reset again after patching
            handler._sm_client = None
            client1 = handler._get_sm_client()
            client2 = handler._get_sm_client()
            assert client1 is client2
            # boto3.client called only once (cached)
            boto3.client.assert_called_once()


class TestAutoRegisterCallsProvision:
    """Verify auto-register → auto-provision ordering and integration."""

    @patch("handler._auto_provision_tenant_github_app_secret")
    @patch("handler._get_events_log")
    @patch("handler._get_signature")
    def test_installation_created_calls_provision_after_register(
        self, mock_sig, mock_log, mock_provision
    ):
        """installation.created event: DDB write happens first, then SM provision."""
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        payload = {
            "action": "created",
            "installation": {"id": 12345, "account": {"login": "neworg"}},
            "sender": {"id": 999, "login": "installer"},
        }
        body = json.dumps(payload)
        event = {
            "headers": {
                "x-github-event": "installation",
                "content-type": "application/json",
                "x-hub-signature-256": "sha256=fake",
            },
            "body": body,
            "isBase64Encoded": False,
        }

        with patch("handler._auto_register_installation", return_value="neworg") as mock_register:
            result = handler(event, None)

        assert result["statusCode"] == 200
        # auto_register called first, then provision
        mock_register.assert_called_once_with(12345, "neworg")
        mock_provision.assert_called_once_with("neworg", 12345)

    @patch("handler._auto_provision_tenant_github_app_secret")
    @patch("handler._get_events_log")
    @patch("handler._get_signature")
    def test_installation_created_no_provision_when_register_fails(
        self, mock_sig, mock_log, mock_provision
    ):
        """When DDB auto-register returns None, SM provision is NOT called."""
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_log.return_value.log_event = MagicMock()

        from handler import handler

        payload = {
            "action": "created",
            "installation": {"id": 12345, "account": {"login": "failorg"}},
            "sender": {"id": 999, "login": "installer"},
        }
        body = json.dumps(payload)
        event = {
            "headers": {
                "x-github-event": "installation",
                "content-type": "application/json",
                "x-hub-signature-256": "sha256=fake",
            },
            "body": body,
            "isBase64Encoded": False,
        }

        with patch("handler._auto_register_installation", return_value=None):
            result = handler(event, None)

        assert result["statusCode"] == 200
        # Provision should NOT be called when register fails
        mock_provision.assert_not_called()

    @patch("handler._auto_provision_tenant_github_app_secret")
    @patch("handler._get_metrics")
    @patch("handler._get_rate_limiter")
    @patch("handler._get_identity_resolver")
    @patch("handler._get_events_log")
    @patch("handler._get_signature")
    def test_self_heal_path_calls_provision(
        self, mock_sig, mock_log, mock_resolver, mock_rate, mock_metrics, mock_provision
    ):
        """Self-heal (unknown_installation during issue_comment) calls provision after register."""
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_log.return_value.log_event = MagicMock()
        mock_metrics.return_value.record_rejected = MagicMock()
        mock_metrics.return_value.flush = MagicMock()

        # First resolve: unknown_installation. After register: still fails (for simplicity)
        mock_resolver.return_value.resolve.side_effect = [
            (None, "unknown_installation"),
            (None, "unknown_installation"),
        ]

        from handler import handler

        payload = {
            "action": "created",
            "issue": {"number": 1},
            "comment": {"body": "@agent-developer hello"},
            "installation": {"id": 77777},
            "repository": {"full_name": "selfhealorg/repo", "owner": {"login": "selfhealorg"}},
            "organization": {"login": "selfhealorg"},
            "sender": {"id": 888, "login": "user"},
        }
        body = json.dumps(payload)
        event = {
            "headers": {
                "x-github-event": "issue_comment",
                "content-type": "application/json",
                "x-hub-signature-256": "sha256=fake",
            },
            "body": body,
            "isBase64Encoded": False,
        }

        with patch("handler._auto_register_installation", return_value="selfhealorg") as mock_reg:
            handler(event, None)

        # Provision was called after register
        mock_reg.assert_called_once_with(77777, "selfhealorg")
        mock_provision.assert_called_once_with("selfhealorg", 77777)
