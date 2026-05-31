"""Unit tests for provenance_client.py.

Issue #1103: Tests SigV4 mode (IRSA) and legacy shared-secret mode.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.provenance_client import post_provenance  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROVENANCE_KWARGS = {
    "actor_user_id": "user-1",
    "triggered_by": None,
    "root_human_id": "user-1",
    "is_human_rooted": True,
    "action_kind": "pr_create",
    "source_event": "worker:entrypoint",
    "correlation_id": "corr-abc",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Clear both SigV4 and legacy env vars by default."""
    monkeypatch.delenv("ADP_GATEWAY_ENDPOINT", raising=False)
    monkeypatch.delenv("VAULT_GATEWAY_URL", raising=False)
    monkeypatch.delenv("VAULT_INTERNAL_API_KEY", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# Unconfigured tests
# ---------------------------------------------------------------------------


class TestUnconfigured:
    def test_returns_none_when_no_env(self):
        result = post_provenance(**PROVENANCE_KWARGS)
        assert result is None

    def test_returns_none_legacy_partial(self, monkeypatch):
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        # No API key
        result = post_provenance(**PROVENANCE_KWARGS)
        assert result is None


# ---------------------------------------------------------------------------
# Legacy mode tests
# ---------------------------------------------------------------------------


class TestLegacyMode:
    @patch("lib.provenance_client.urlopen")
    def test_posts_with_api_key_header(self, mock_urlopen, monkeypatch):
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "legacy-key")

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"provenance_id": "prov-123"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = post_provenance(**PROVENANCE_KWARGS)

        assert result == "prov-123"
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("X-internal-api-key") == "legacy-key"
        assert req.full_url == "http://gateway:8080/internal/v1/provenance"

    @patch("lib.provenance_client.urlopen")
    def test_returns_none_on_http_error(self, mock_urlopen, monkeypatch):
        from urllib.error import HTTPError

        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key")

        mock_urlopen.side_effect = HTTPError("url", 500, "ISE", {}, None)

        result = post_provenance(**PROVENANCE_KWARGS)
        assert result is None


# ---------------------------------------------------------------------------
# SigV4 mode tests
# ---------------------------------------------------------------------------


class TestSigV4Mode:
    @patch("lib.provenance_client.urlopen")
    @patch("lib.provenance_client._sigv4_sign_request")
    def test_posts_with_sigv4_headers(self, mock_sign, mock_urlopen, monkeypatch):
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://api-gw.example.com")

        mock_sign.return_value = {
            "Content-Type": "application/json",
            "Authorization": "AWS4-HMAC-SHA256 Credential=AKIA.../us-east-1/execute-api/aws4_request",
            "X-Amz-Date": "20260531T120000Z",
        }

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"provenance_id": "prov-456"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = post_provenance(**PROVENANCE_KWARGS)

        assert result == "prov-456"
        mock_sign.assert_called_once()
        call_args = mock_sign.call_args
        assert call_args[0][0] == "POST"
        assert "/agent/internal/v1/provenance" in call_args[0][1]

        req = mock_urlopen.call_args[0][0]
        assert "AWS4-HMAC-SHA256" in req.get_header("Authorization")
        assert req.get_header("X-internal-api-key") is None

    @patch("lib.provenance_client.urlopen")
    @patch("lib.provenance_client._sigv4_sign_request")
    def test_sigv4_preferred_over_legacy(self, mock_sign, mock_urlopen, monkeypatch):
        """When both ADP_GATEWAY_ENDPOINT and legacy vars are set, SigV4 wins."""
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://api-gw.example.com")
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "legacy-key")

        mock_sign.return_value = {"Content-Type": "application/json", "Authorization": "AWS4-HMAC-SHA256 ..."}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"provenance_id": "prov-789"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = post_provenance(**PROVENANCE_KWARGS)

        assert result == "prov-789"
        # Should use SigV4, not legacy
        mock_sign.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "api-gw.example.com" in req.full_url

    @patch("lib.provenance_client._sigv4_sign_request")
    def test_returns_none_on_sigv4_failure(self, mock_sign, monkeypatch):
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://api-gw.example.com")
        mock_sign.side_effect = RuntimeError("No AWS credentials")

        result = post_provenance(**PROVENANCE_KWARGS)
        assert result is None
