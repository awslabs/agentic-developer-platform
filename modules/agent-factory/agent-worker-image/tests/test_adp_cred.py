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
    """Set required env vars for every test."""
    monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
    monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "test-key")
    monkeypatch.setenv("ADP_USER_ID", "user-001")
    monkeypatch.setenv("ADP_AGENT_ID", "developer")
    monkeypatch.setenv("ADP_TASK_ID", "task-abc")
    monkeypatch.setenv("ENABLE_USER_CREDENTIALS", "1")


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
        base_url, api_key, user_id, agent_id, task_id = adp_client._get_config()
        assert base_url == "http://gateway:8080"
        assert api_key == "test-key"
        assert user_id == "user-001"
        assert agent_id == "developer"
        assert task_id == "task-abc"

    def test_strips_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080/")
        base_url, _, _, _, _ = adp_client._get_config()
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
        mock_resp.read.return_value = b'[{"service":"github","label":"PAT","credential_type":"oauth_token"}]'
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
        mock_resp.read.return_value = b'{"status":200,"headers":{},"body":"ok","provenance_id":"p1"}'
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
        mock_resp.read.return_value = b'{"value":"ghp_secret123456","credential_type":"oauth_token","provenance_id":"p2"}'
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
