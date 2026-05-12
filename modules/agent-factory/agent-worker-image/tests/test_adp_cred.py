"""Unit tests for adp-cred CLI.

Issue #137: Vault Phase 4
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure the adp_cred package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adp_cred import client as adp_client  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Set required env vars for every test (legacy mode by default)."""
    monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
    monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "test-key")
    monkeypatch.setenv("ADP_USER_ID", "user-001")
    monkeypatch.setenv("ADP_AGENT_ID", "developer")
    monkeypatch.setenv("ADP_TASK_ID", "task-abc")
    monkeypatch.setenv("ENABLE_USER_CREDENTIALS", "1")
    # Ensure SigV4 mode is not active by default
    monkeypatch.delenv("ADP_GATEWAY_ENDPOINT", raising=False)


class TestCheckEnabled:
    def test_exits_when_disabled(self, monkeypatch):
        monkeypatch.setenv("ENABLE_USER_CREDENTIALS", "0")
        with pytest.raises(SystemExit) as exc_info:
            adp_client._check_enabled()
        assert exc_info.value.code == 1

    def test_passes_when_true(self):
        # Should not raise
        adp_client._check_enabled()

    def test_passes_when_literal_true(self, monkeypatch):
        monkeypatch.setenv("ENABLE_USER_CREDENTIALS", "true")
        adp_client._check_enabled()


class TestGetConfig:
    def test_returns_all_values(self):
        base_url, api_key, user_id, agent_id, task_id, use_sigv4 = adp_client._get_config()
        assert base_url == "http://gateway:8080"
        assert api_key == "test-key"
        assert user_id == "user-001"
        assert agent_id == "developer"
        assert task_id == "task-abc"
        assert use_sigv4 is False

    def test_strips_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080/")
        base_url, _, _, _, _, _ = adp_client._get_config()
        assert base_url == "http://gateway:8080"

    def test_exits_code_2_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("ADP_USER_ID")
        with pytest.raises(SystemExit) as exc_info:
            adp_client._get_config()
        assert exc_info.value.code == 2


class TestListCredentials:
    @patch("adp_cred.client.urlopen")
    def test_calls_correct_endpoint(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'[{"service":"github","label":"PAT","credential_type":"oauth_token"}]'
        )
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = adp_client.list_credentials()

        assert isinstance(result, list)
        assert result[0]["service"] == "github"

        # Verify URL contains user_id
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert "user_id=user-001" in req.full_url
        assert req.get_header("X-internal-api-key") == "test-key"


class TestProxyHttp:
    @patch("adp_cred.client.urlopen")
    def test_mints_correct_request(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"status":200,"headers":{},"body":"ok","provenance_id":"p1"}'
        )
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = adp_client.proxy_http("GET", "https://api.github.com/user", "github")

        assert result["status"] == 200
        # Verify the POST body
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["user_id"] == "user-001"
        assert body["service"] == "github"
        assert body["method"] == "GET"
        assert body["url"] == "https://api.github.com/user"


class TestRawRead:
    @patch("adp_cred.client.urlopen")
    def test_returns_value(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"value":"ghp_secret123456","credential_type":"oauth_token","provenance_id":"p2"}'
        )
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = adp_client.raw_read("github", purpose="gh CLI auth")

        assert result["value"] == "ghp_secret123456"
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["purpose"] == "gh CLI auth"


class TestMaterialize:
    @patch("adp_cred.client.urlopen")
    def test_returns_url(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"materialize_url":"https://s3.presigned/x","expires_at":"2026-01-01T00:00:00Z","provenance_id":"p3"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = adp_client.materialize("ssh-key-prod")

        assert "presigned" in result["materialize_url"]


class TestCLIEntryPoint:
    """Test the __main__.py entry point via subprocess."""

    def test_list_subcommand(self, monkeypatch, tmp_path):
        """Verify `adp-cred list` runs and calls the right function."""
        # We test the module invocation pattern
        env = {
            **os.environ,
            "VAULT_GATEWAY_URL": "http://gateway:8080",
            "VAULT_INTERNAL_API_KEY": "key",
            "ADP_USER_ID": "u1",
            "ADP_AGENT_ID": "dev",
            "ADP_TASK_ID": "t1",
            "ENABLE_USER_CREDENTIALS": "1",
            "PYTHONPATH": os.path.join(os.path.dirname(__file__), ".."),
        }
        # This will fail to connect but we verify exit code 1 (connection error),
        # not 2 (missing env)
        result = subprocess.run(
            [sys.executable, "-m", "adp_cred", "list"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Should fail with connection error (exit 1), not env error (exit 2)
        assert result.returncode != 2

    def test_missing_env_exits_2(self):
        """When required env vars missing, exits with code 2."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("ADP_")}
        env.pop("VAULT_GATEWAY_URL", None)
        env.pop("VAULT_INTERNAL_API_KEY", None)
        env["ENABLE_USER_CREDENTIALS"] = "1"
        env["PYTHONPATH"] = os.path.join(os.path.dirname(__file__), "..")
        result = subprocess.run(
            [sys.executable, "-m", "adp_cred", "list"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 2

    def test_disabled_exits_1(self, monkeypatch):
        """When ENABLE_USER_CREDENTIALS is off, exits with code 1."""
        env = {
            **os.environ,
            "VAULT_GATEWAY_URL": "http://gateway:8080",
            "VAULT_INTERNAL_API_KEY": "key",
            "ADP_USER_ID": "u1",
            "ADP_AGENT_ID": "dev",
            "ADP_TASK_ID": "t1",
            "ENABLE_USER_CREDENTIALS": "0",
            "PYTHONPATH": os.path.join(os.path.dirname(__file__), ".."),
        }
        result = subprocess.run(
            [sys.executable, "-m", "adp_cred", "list"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 1


# ---------------------------------------------------------------------------
# SigV4 mode tests (Issue #575)
# ---------------------------------------------------------------------------


class TestSigV4Config:
    """Test _get_config with ADP_GATEWAY_ENDPOINT set (SigV4 mode)."""

    def test_sigv4_mode_when_gateway_endpoint_set(self, monkeypatch):
        """ADP_GATEWAY_ENDPOINT triggers SigV4 mode; VAULT_* not required."""
        monkeypatch.setenv(
            "ADP_GATEWAY_ENDPOINT", "https://abc123.execute-api.us-east-1.amazonaws.com/dev"
        )
        monkeypatch.delenv("VAULT_GATEWAY_URL", raising=False)
        monkeypatch.delenv("VAULT_INTERNAL_API_KEY", raising=False)

        base_url, api_key, user_id, agent_id, task_id, use_sigv4 = adp_client._get_config()

        assert use_sigv4 is True
        assert api_key is None
        assert base_url == "https://abc123.execute-api.us-east-1.amazonaws.com/dev/agent"
        assert user_id == "user-001"

    def test_legacy_mode_when_no_gateway_endpoint(self):
        """Without ADP_GATEWAY_ENDPOINT, uses legacy mode."""
        base_url, api_key, user_id, agent_id, task_id, use_sigv4 = adp_client._get_config()

        assert use_sigv4 is False
        assert api_key == "test-key"
        assert base_url == "http://gateway:8080"

    def test_sigv4_mode_missing_user_id_exits(self, monkeypatch):
        """SigV4 mode still requires ADP_USER_ID."""
        monkeypatch.setenv(
            "ADP_GATEWAY_ENDPOINT", "https://abc123.execute-api.us-east-1.amazonaws.com/dev"
        )
        monkeypatch.delenv("ADP_USER_ID")
        monkeypatch.delenv("VAULT_GATEWAY_URL", raising=False)
        monkeypatch.delenv("VAULT_INTERNAL_API_KEY", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            adp_client._get_config()
        assert exc_info.value.code == 2


class TestSigV4Request:
    """Test SigV4 signed request path."""

    @patch("adp_cred.client._sigv4_request")
    def test_list_credentials_uses_sigv4(self, mock_sigv4, monkeypatch):
        """When ADP_GATEWAY_ENDPOINT is set, list_credentials uses SigV4."""
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://gw.example.com/dev")
        monkeypatch.delenv("VAULT_GATEWAY_URL", raising=False)
        monkeypatch.delenv("VAULT_INTERNAL_API_KEY", raising=False)
        mock_sigv4.return_value = [{"service": "github", "label": "PAT"}]

        result = adp_client.list_credentials()

        assert result == [{"service": "github", "label": "PAT"}]
        mock_sigv4.assert_called_once()
        call_url = mock_sigv4.call_args[0][1]
        assert "/agent/internal/v1/user-credentials" in call_url

    @patch("adp_cred.client._sigv4_request")
    def test_raw_read_uses_sigv4(self, mock_sigv4, monkeypatch):
        """When ADP_GATEWAY_ENDPOINT is set, raw_read uses SigV4."""
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://gw.example.com/dev")
        monkeypatch.delenv("VAULT_GATEWAY_URL", raising=False)
        monkeypatch.delenv("VAULT_INTERNAL_API_KEY", raising=False)
        mock_sigv4.return_value = {
            "value": "secret",
            "credential_type": "oauth_token",
            "provenance_id": "p1",
        }

        result = adp_client.raw_read("github", purpose="test")

        assert result["value"] == "secret"
        call_url = mock_sigv4.call_args[0][1]
        assert "/agent/internal/v1/credential-raw-read" in call_url
        call_body = mock_sigv4.call_args[0][2]
        assert call_body["user_id"] == "user-001"
        assert call_body["purpose"] == "test"

    @patch("adp_cred.client.urlopen")
    def test_legacy_mode_still_uses_shared_secret(self, mock_urlopen):
        """Without ADP_GATEWAY_ENDPOINT, uses legacy shared-secret path."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'[{"service":"github"}]'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        adp_client.list_credentials()

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.get_header("X-internal-api-key") == "test-key"
        assert "/internal/v1/user-credentials" in req.full_url


class TestSigV4Signing:
    """Test that _sigv4_request produces correct SigV4 headers."""

    @patch("botocore.session.get_session")
    @patch("adp_cred.client.urlopen")
    def test_sigv4_adds_authorization_header(self, mock_urlopen, mock_session, monkeypatch):
        """SigV4 request includes Authorization: AWS4-HMAC-SHA256 header."""
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        # Mock botocore credentials
        mock_creds = MagicMock()
        mock_creds.access_key = "AKIAIOSFODNN7EXAMPLE"
        mock_creds.secret_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        mock_creds.token = None
        mock_frozen = MagicMock()
        mock_frozen.access_key = "AKIAIOSFODNN7EXAMPLE"
        mock_frozen.secret_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        mock_frozen.token = None
        mock_creds.get_frozen_credentials.return_value = mock_frozen
        mock_session.return_value.get_credentials.return_value = mock_creds

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = adp_client._sigv4_request(
            "POST",
            "https://abc.execute-api.us-east-1.amazonaws.com/dev/agent/internal/v1/credential-raw-read",
            {"user_id": "u1"},
        )

        assert result == {"ok": True}
        # Verify the request was made with SigV4 Authorization header
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        auth_header = req.get_header("Authorization")
        assert auth_header is not None
        assert "AWS4-HMAC-SHA256" in auth_header
