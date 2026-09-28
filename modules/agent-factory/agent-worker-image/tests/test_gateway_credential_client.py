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

    @patch("lib.gateway_credential_client.urlopen")
    def test_legacy_url_unchanged_by_4343(self, mock_urlopen, monkeypatch):
        """The shared-secret transport is untouched by the #4343 SigV4 fix.

        Legacy mode talks to the gateway pod directly (no API Gateway, no ALB
        deny in front of it), so its URL shape must not shift.
        """
        monkeypatch.setenv("VAULT_GATEWAY_URL", "http://gateway:8080")
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "key")

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"access_key_id": "AK"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = GatewayCredentialClient()
        client.assume_role(user_id="u", agent_id="a", task_id="t")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "http://gateway:8080/internal/v1/credential-assume-role"


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
        # Issue #4343: the signed URL must target /internal/... (API Gateway
        # /internal/{proxy+} -> internal-plane ALB), NOT /agent/internal/...
        # (/agent/{proxy+} -> edge ALB, which 403s /internal/* per #4010).
        assert "/internal/v1/credential-assume-role" in call_args[0][1]
        assert "/agent" not in call_args[0][1]

        # Verify the request has SigV4 headers
        req = mock_urlopen.call_args[0][0]
        assert "AWS4-HMAC-SHA256" in req.get_header("Authorization")
        assert req.get_header("X-amz-date") == "20260531T120000Z"
        # Should NOT have the legacy header
        assert req.get_header("X-internal-api-key") is None

    @patch("lib.gateway_credential_client.urlopen")
    @patch("lib.gateway_credential_client._sigv4_sign_request")
    def test_base_url_has_no_agent_prefix(self, mock_sign, mock_urlopen, monkeypatch):
        """SigV4 internal calls address /internal/... directly (issue #4343).

        The /agent prefix routed these through the edge ALB, where #4010's
        edge-internal-deny patch answers 403 "Not available from the edge" for
        any /internal/* path.
        """
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
        assert req.full_url == "https://api-gw.example.com/internal/v1/credential-assume-role"

    @patch("lib.gateway_credential_client.urlopen")
    @patch("lib.gateway_credential_client._sigv4_sign_request")
    def test_all_sigv4_endpoints_target_internal_route(
        self, mock_sign, mock_urlopen, monkeypatch
    ):
        """Every endpoint on this client is /internal/* — none may carry /agent.

        All three consumers share one _base_url, so this pins the whole surface:
        the token gatekeeper (#4272) plus both credential paths. A regression on
        any of them is a dead agent run (mint) or a dead linked-account read.
        """
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://api-gw.example.com/dev")
        monkeypatch.setenv("ADP_MESSAGE_ID", "msg-1")

        mock_sign.return_value = {"Content-Type": "application/json"}

        def _resp(body: bytes):
            mock_resp = MagicMock()
            mock_resp.read.return_value = body
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        client = GatewayCredentialClient()

        mock_urlopen.return_value = _resp(b'{"value": "v"}')
        client.raw_read(user_id="u", agent_id="a", task_id="t", service="aws")
        assert (
            mock_urlopen.call_args[0][0].full_url
            == "https://api-gw.example.com/dev/internal/v1/credential-raw-read"
        )

        mock_urlopen.return_value = _resp(b'{"token": "ghs_x", "expires_at": "2026-08-28T12:00:00Z"}')
        client.github_installation_token(
            installation_id=555001, repo_owner="acme", repo_name="app"
        )
        assert (
            mock_urlopen.call_args[0][0].full_url
            == "https://api-gw.example.com/dev/internal/v1/github-installation-token"
        )

        mock_urlopen.return_value = _resp(b'{"access_key_id": "AK"}')
        client.assume_role(user_id="u", agent_id="a", task_id="t")
        assert (
            mock_urlopen.call_args[0][0].full_url
            == "https://api-gw.example.com/dev/internal/v1/credential-assume-role"
        )

    @patch("lib.gateway_credential_client.urlopen")
    @patch("lib.gateway_credential_client._sigv4_sign_request")
    def test_trailing_slash_on_endpoint_does_not_double(
        self, mock_sign, mock_urlopen, monkeypatch
    ):
        """A doubled slash would match no API Gateway route."""
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://api-gw.example.com/dev/")

        mock_sign.return_value = {"Content-Type": "application/json"}

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"access_key_id": "AK"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = GatewayCredentialClient()
        client.assume_role(user_id="u", agent_id="a", task_id="t")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api-gw.example.com/dev/internal/v1/credential-assume-role"

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


class TestAuthorityBrokerIdentity:
    def test_current_proofs_are_signed_on_every_request(self, monkeypatch, tmp_path):
        from unittest.mock import MagicMock, patch
        from lib.gateway_credential_client import GatewayCredentialClient
        monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://gateway.example.test/dev")
        for variable, name in (("ADP_RUN_CREDENTIAL_FILE", "run"), ("ADP_WORKLOAD_TOKEN_FILE", "pod")):
            monkeypatch.setenv(variable, str(tmp_path / name))
            (tmp_path / name).write_text(name + "-proof")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'
        with patch("lib.gateway_credential_client._sigv4_sign_request", side_effect=lambda method, url, headers, data: headers) as signer, patch("lib.gateway_credential_client.build_opener") as opener:
            opener.return_value.open.return_value = response
            client = GatewayCredentialClient()
            for epoch in (1, 2):
                (tmp_path / "run").write_text(f"run-proof-{epoch}")
                assert client._make_request("https://gateway.example.test/dev/internal/v1/credential-assume-role", {}) == {"ok": True}
                assert signer.call_args.args[2]["X-Adp-Run-Credential"] == f"run-proof-{epoch}"
                assert signer.call_args.args[2]["X-Adp-Workload-Token"] == "pod-proof"
            assert opener.call_count == 2

    def test_missing_proof_or_legacy_transport_never_sends(self, monkeypatch):
        from unittest.mock import patch
        from lib.gateway_credential_client import GatewayCredentialClient, GatewayCredentialError
        monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "true")
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://gateway.example.test")
        monkeypatch.delenv("ADP_RUN_CREDENTIAL_FILE", raising=False)
        with patch("lib.gateway_credential_client._sigv4_sign_request") as signer:
            with pytest.raises(GatewayCredentialError, match="identity unavailable"):
                GatewayCredentialClient()._make_request("https://gateway.example.test/internal/v1/credential-assume-role", {})
            monkeypatch.delenv("ADP_GATEWAY_ENDPOINT")
            with pytest.raises(GatewayCredentialError, match="HTTPS and SigV4"):
                GatewayCredentialClient(gateway_url="https://legacy.example.test", api_key="legacy")._make_request("https://legacy.example.test/internal/v1/credential-assume-role", {})
            signer.assert_not_called()


@pytest.mark.parametrize("identity", [None, "review"])
def test_shared_report_credential_sent_only_for_review_mint(monkeypatch, tmp_path, identity):
    proof = tmp_path / "report"
    proof.write_text("adprpt1.test-proof")
    monkeypatch.setenv("ADP_RUN_REPORT_CREDENTIAL_FILE", str(proof))
    monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://gateway.test")
    client = GatewayCredentialClient()
    with patch.object(client, "_make_request", return_value={"token": "test"}) as send:
        client.github_installation_token(installation_id=42, repo_owner="org", repo_name="repo", identity=identity)
    assert send.call_args.kwargs == ({"extra_headers": {"X-Adp-Report-Credential": "adprpt1.test-proof"}} if identity else {})


@pytest.mark.parametrize("endpoint", ["http://gateway.test", "https://gateway.test?redirect=evil"])
def test_shared_review_proof_rejects_insecure_endpoint(monkeypatch, tmp_path, endpoint):
    proof = tmp_path / "report"
    proof.write_text("adprpt1.test-proof")
    monkeypatch.setenv("ADP_RUN_REPORT_CREDENTIAL_FILE", str(proof))
    monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", endpoint)
    with pytest.raises(GatewayCredentialError, match="HTTPS and SigV4"):
        GatewayCredentialClient().github_installation_token(installation_id=42, repo_owner="org", repo_name="repo", identity="review")


def test_shared_review_proof_rejects_redirects(monkeypatch, tmp_path):
    proof = tmp_path / "report"
    proof.write_text("adprpt1.test-proof")
    monkeypatch.setenv("ADP_RUN_REPORT_CREDENTIAL_FILE", str(proof))
    monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://gateway.test")
    with patch("lib.gateway_credential_client._sigv4_sign_request", side_effect=lambda method, url, headers, data: headers), patch("lib.gateway_credential_client.build_opener") as opener:
        opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"token":"test"}'
        GatewayCredentialClient().github_installation_token(installation_id=42, repo_owner="org", repo_name="repo", identity="review")
        assert opener.call_args.args[0].redirect_request(None, None, 302, "redirect", {}, "https://evil.test") is None
