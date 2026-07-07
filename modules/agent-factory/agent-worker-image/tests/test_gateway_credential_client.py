"""Unit tests for gateway_credential_client.py.

Issue #1103: Tests SigV4 mode (IRSA) and legacy shared-secret mode.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.gateway_credential_client import (  # noqa: E402
    GatewayCredentialClient,
    GatewayCredentialError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Clear both SigV4 and legacy env vars by default."""
    monkeypatch.delenv("ADP_GATEWAY_ENDPOINT", raising=False)
    monkeypatch.delenv("VAULT_GATEWAY_URL", raising=False)
    monkeypatch.delenv("VAULT_INTERNAL_API_KEY", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# is_configured tests
# ---------------------------------------------------------------------------


class TestIsConfigured:
    def test_not_configured_when_empty(self):
        client = GatewayCredentialClient()
        assert client.is_configured is False

    def test_configured_legacy_mode(self, monkeypatch):
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "secret")
        client = GatewayCredentialClient()
        assert client.is_configured is True

    def test_configured_sigv4_mode(self, monkeypatch):
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://api-gw.example.com")
        client = GatewayCredentialClient()
        assert client.is_configured is True

    def test_sigv4_mode_does_not_require_api_key(self, monkeypatch):
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://api-gw.example.com")
        # No VAULT_INTERNAL_API_KEY set
        client = GatewayCredentialClient()
        assert client.is_configured is True

    def test_legacy_mode_requires_both(self, monkeypatch):
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        # No API key
        client = GatewayCredentialClient()
        assert client.is_configured is False


# ---------------------------------------------------------------------------
# Legacy mode tests
# ---------------------------------------------------------------------------


class TestLegacyMode:
    @patch("lib.gateway_credential_client.urlopen")
    def test_assume_role_sends_api_key_header(self, mock_urlopen, monkeypatch):
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "test-key-123")

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"access_key_id": "AKIA...", "secret_access_key": "s3cr3t", "session_token": "tok"}
        ).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = GatewayCredentialClient()
        result = client.assume_role(
            user_id="user-1", agent_id="dev", task_id="task-1"
        )

        assert result["access_key_id"] == "AKIA..."
        # Verify the request used the API key header
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.get_header("X-internal-api-key") == "test-key-123"
        assert "Authorization" not in req.headers

    @patch("lib.gateway_credential_client.urlopen")
    def test_raw_read_includes_scope_header(self, mock_urlopen, monkeypatch):
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key")

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"value": "cred-val"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = GatewayCredentialClient()
        client.raw_read(
            user_id="u", agent_id="a", task_id="t", service="aws"
        )

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("X-agent-scopes") == "credential:raw-read"


# ---------------------------------------------------------------------------
# SigV4 mode tests
# ---------------------------------------------------------------------------


class TestSigV4Mode:
    @patch("lib.gateway_credential_client.urlopen")
    @patch("lib.gateway_credential_client._sigv4_sign_request")
    def test_assume_role_uses_sigv4_headers(
        self, mock_sign, mock_urlopen, monkeypatch
    ):
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://api-gw.example.com")

        mock_sign.return_value = {
            "Content-Type": "application/json",
            "Authorization": "AWS4-HMAC-SHA256 Credential=AKIA.../us-east-1/execute-api/aws4_request",
            "X-Amz-Date": "20260531T120000Z",
        }

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"access_key_id": "AKIA...", "secret_access_key": "s", "session_token": "t"}
        ).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = GatewayCredentialClient()
        result = client.assume_role(
            user_id="user-1", agent_id="dev", task_id="task-1"
        )

        assert result["access_key_id"] == "AKIA..."
        # Verify SigV4 signing was called
        mock_sign.assert_called_once()
        call_args = mock_sign.call_args
        assert call_args[0][0] == "POST"  # method
        assert "/agent/internal/v1/credential-assume-role" in call_args[0][1]

        # Verify the request has SigV4 headers
        req = mock_urlopen.call_args[0][0]
        assert "AWS4-HMAC-SHA256" in req.get_header("Authorization")
        assert req.get_header("X-amz-date") == "20260531T120000Z"
        # Should NOT have the legacy header
        assert req.get_header("X-internal-api-key") is None

    @patch("lib.gateway_credential_client.urlopen")
    @patch("lib.gateway_credential_client._sigv4_sign_request")
    def test_base_url_appends_agent_prefix(self, mock_sign, mock_urlopen, monkeypatch):
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://api-gw.example.com")

        mock_sign.return_value = {"Content-Type": "application/json"}

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"provenance_id": "p-1"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = GatewayCredentialClient()
        client.assume_role(user_id="u", agent_id="a", task_id="t")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api-gw.example.com/agent/internal/v1/credential-assume-role"

    @patch("lib.gateway_credential_client._sigv4_sign_request")
    def test_sigv4_sign_raises_error_propagates(self, mock_sign, monkeypatch):
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://api-gw.example.com")

        mock_sign.side_effect = GatewayCredentialError("No AWS credentials available for SigV4 signing")

        client = GatewayCredentialClient()
        with pytest.raises(GatewayCredentialError, match="SigV4 signing"):
            client.assume_role(user_id="u", agent_id="a", task_id="t")


# ---------------------------------------------------------------------------
# Invocation ID tests (Issue #3176)
# ---------------------------------------------------------------------------


class TestInvocationId:
    """Test invocation_id from ADP_MESSAGE_ID is included in request bodies."""

    @patch("lib.gateway_credential_client.urlopen")
    def test_assume_role_includes_invocation_id(self, mock_urlopen, monkeypatch):
        """When ADP_MESSAGE_ID is set, assume_role payload includes invocation_id."""
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key")
        monkeypatch.setenv("ADP_MESSAGE_ID", "msg-run-001")

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"access_key_id": "AK", "secret_access_key": "SK", "session_token": "ST"}
        ).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = GatewayCredentialClient()
        client.assume_role(user_id="u", agent_id="a", task_id="t")

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["invocation_id"] == "msg-run-001"

    @patch("lib.gateway_credential_client.urlopen")
    def test_assume_role_omits_invocation_id_when_unset(self, mock_urlopen, monkeypatch):
        """When ADP_MESSAGE_ID is not set, invocation_id absent from payload."""
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key")
        monkeypatch.delenv("ADP_MESSAGE_ID", raising=False)

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"access_key_id": "AK", "secret_access_key": "SK", "session_token": "ST"}
        ).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = GatewayCredentialClient()
        client.assume_role(user_id="u", agent_id="a", task_id="t")

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert "invocation_id" not in body

    @patch("lib.gateway_credential_client.urlopen")
    def test_raw_read_includes_invocation_id(self, mock_urlopen, monkeypatch):
        """When ADP_MESSAGE_ID is set, raw_read payload includes invocation_id."""
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key")
        monkeypatch.setenv("ADP_MESSAGE_ID", "msg-raw-002")

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"value": "cred-val"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = GatewayCredentialClient()
        client.raw_read(user_id="u", agent_id="a", task_id="t", service="aws")

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["invocation_id"] == "msg-raw-002"

    @patch("lib.gateway_credential_client.urlopen")
    def test_raw_read_omits_invocation_id_when_empty(self, mock_urlopen, monkeypatch):
        """When ADP_MESSAGE_ID is empty, invocation_id absent from payload."""
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key")
        monkeypatch.setenv("ADP_MESSAGE_ID", "")

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"value": "cred-val"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = GatewayCredentialClient()
        client.raw_read(user_id="u", agent_id="a", task_id="t", service="aws")

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert "invocation_id" not in body

    @patch("lib.gateway_credential_client.urlopen")
    @patch("lib.gateway_credential_client._sigv4_sign_request")
    def test_sigv4_mode_includes_invocation_id(
        self, mock_sign, mock_urlopen, monkeypatch
    ):
        """SigV4 mode also sends invocation_id when ADP_MESSAGE_ID is set."""
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://api-gw.example.com")
        monkeypatch.setenv("ADP_MESSAGE_ID", "msg-sigv4-003")

        mock_sign.return_value = {"Content-Type": "application/json"}

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"access_key_id": "AK", "secret_access_key": "SK", "session_token": "ST"}
        ).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = GatewayCredentialClient()
        client.assume_role(user_id="u", agent_id="a", task_id="t")

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["invocation_id"] == "msg-sigv4-003"


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @patch("lib.gateway_credential_client.urlopen")
    def test_http_error_wrapped(self, mock_urlopen, monkeypatch):
        from urllib.error import HTTPError

        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key")

        mock_urlopen.side_effect = HTTPError(
            "http://gateway:8080/internal/v1/credential-assume-role",
            403,
            "Forbidden",
            {},
            None,
        )

        client = GatewayCredentialClient()
        with pytest.raises(GatewayCredentialError, match="HTTP 403"):
            client.assume_role(user_id="u", agent_id="a", task_id="t")

    @patch("lib.gateway_credential_client.urlopen")
    def test_url_error_wrapped(self, mock_urlopen, monkeypatch):
        from urllib.error import URLError

        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key")

        mock_urlopen.side_effect = URLError("Connection refused")

        client = GatewayCredentialClient()
        with pytest.raises(GatewayCredentialError, match="Cannot reach gateway"):
            client.assume_role(user_id="u", agent_id="a", task_id="t")
