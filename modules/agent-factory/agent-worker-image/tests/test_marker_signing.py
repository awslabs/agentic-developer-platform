"""Unit tests for lib/marker_signing.py (cred-binding S4, Issue #3178)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.marker_signing import compute_signature, reset_key_cache


@pytest.fixture(autouse=True)
def _clear_key_cache():
    """Reset the module-level key cache before each test."""
    reset_key_cache()
    yield
    reset_key_cache()


class TestComputeSignature:
    """Tests for compute_signature()."""

    def test_returns_none_when_env_var_not_set(self):
        """No signing when ADP_MARKER_SIGNING_KEY_SECRET is not set."""
        env = {"AWS_REGION": "us-east-1"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ADP_MARKER_SIGNING_KEY_SECRET", None)
            result = compute_signature("corr-1", "user-1", "true", "inv-1", "0")
        assert result is None

    @patch("lib.marker_signing.boto3")
    def test_returns_signature_when_key_available(self, mock_boto3):
        """Produces a valid base64url signature when the key is loadable."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {
            "SecretString": "test-signing-key-32bytes-long!!!"
        }

        env = {
            "ADP_MARKER_SIGNING_KEY_SECRET": "adp/dev/webhook-ingress/marker-signing-key",
            "AWS_REGION": "us-east-1",
        }
        with patch.dict(os.environ, env, clear=False):
            result = compute_signature("corr-1", "user-1", "true", "inv-1", "0")

        assert result is not None
        # Verify it's valid base64url (no padding)
        assert "=" not in result
        assert "+" not in result
        assert "/" not in result
        # Verify round-trip: decode and compare with expected HMAC
        key = b"test-signing-key-32bytes-long!!!"
        expected_input = "corr-1:user-1:true:inv-1:0"
        expected_sig = hmac.new(key, expected_input.encode("utf-8"), hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode("ascii")
        assert result == expected_b64

    @patch("lib.marker_signing.boto3")
    def test_signing_input_matches_spec(self, mock_boto3):
        """Signing input is exactly '{corr}:{root}:{rooted}:{invocation}:{depth}'."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {"SecretString": "my-key"}

        env = {
            "ADP_MARKER_SIGNING_KEY_SECRET": "adp/dev/webhook-ingress/marker-signing-key",
            "AWS_REGION": "us-east-1",
        }
        with patch.dict(os.environ, env, clear=False):
            result = compute_signature(
                correlation_id="abc-123",
                root_human_id="user-xyz",
                is_human_rooted="false",
                invocation_id="msg-456",
                chain_depth="3",
            )

        # Manually compute expected
        key = b"my-key"
        signing_input = "abc-123:user-xyz:false:msg-456:3"
        expected_sig = hmac.new(key, signing_input.encode("utf-8"), hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode("ascii")
        assert result == expected_b64

    @patch("lib.marker_signing.boto3")
    def test_empty_optional_fields_in_signing_input(self, mock_boto3):
        """Missing optional fields are empty strings in the signing input."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {"SecretString": "key123"}

        env = {
            "ADP_MARKER_SIGNING_KEY_SECRET": "adp/dev/webhook-ingress/marker-signing-key",
            "AWS_REGION": "us-east-1",
        }
        with patch.dict(os.environ, env, clear=False):
            result = compute_signature(
                correlation_id="c1",
                root_human_id="u1",
                is_human_rooted="true",
                invocation_id="",
                chain_depth="",
            )

        # Signing input with empty optionals: "c1:u1:true::"
        key = b"key123"
        signing_input = "c1:u1:true::"
        expected_sig = hmac.new(key, signing_input.encode("utf-8"), hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode("ascii")
        assert result == expected_b64

    @patch("lib.marker_signing.boto3")
    def test_base64url_encoding_no_padding(self, mock_boto3):
        """Output is base64url with no '=' padding characters."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {"SecretString": "k"}

        env = {
            "ADP_MARKER_SIGNING_KEY_SECRET": "adp/dev/webhook-ingress/marker-signing-key",
            "AWS_REGION": "us-east-1",
        }
        with patch.dict(os.environ, env, clear=False):
            result = compute_signature("a", "b", "true", "", "")

        assert result is not None
        assert "=" not in result
        # Must be decodable with padding restored
        padded = result + "=" * (4 - len(result) % 4) if len(result) % 4 else result
        decoded = base64.urlsafe_b64decode(padded)
        assert len(decoded) == 32  # SHA-256 is always 32 bytes

    @patch("lib.marker_signing.boto3")
    def test_graceful_degradation_on_client_error(self, mock_boto3):
        """Returns None (no crash) when Secrets Manager call fails."""
        from botocore.exceptions import ClientError

        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
            "GetSecretValue",
        )

        env = {
            "ADP_MARKER_SIGNING_KEY_SECRET": "adp/dev/webhook-ingress/marker-signing-key",
            "AWS_REGION": "us-east-1",
        }
        with patch.dict(os.environ, env, clear=False):
            result = compute_signature("corr", "user", "true", "", "")

        assert result is None

    @patch("lib.marker_signing.boto3")
    def test_key_cached_across_calls(self, mock_boto3):
        """Signing key is fetched from SM only once per process (caching)."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {"SecretString": "cached-key"}

        env = {
            "ADP_MARKER_SIGNING_KEY_SECRET": "adp/dev/webhook-ingress/marker-signing-key",
            "AWS_REGION": "us-east-1",
        }
        with patch.dict(os.environ, env, clear=False):
            compute_signature("c1", "u1", "true", "", "")
            compute_signature("c2", "u2", "false", "i2", "1")

        # Should only call get_secret_value once
        mock_client.get_secret_value.assert_called_once()

    @patch("lib.marker_signing.boto3")
    def test_is_human_rooted_lowercase_bool(self, mock_boto3):
        """Boolean is_human_rooted uses lowercase 'true'/'false' in signing input."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {"SecretString": "key"}

        env = {
            "ADP_MARKER_SIGNING_KEY_SECRET": "adp/dev/webhook-ingress/marker-signing-key",
            "AWS_REGION": "us-east-1",
        }
        with patch.dict(os.environ, env, clear=False):
            sig_true = compute_signature("c", "u", "true", "", "")
            reset_key_cache()

        with patch.dict(os.environ, env, clear=False):
            sig_false = compute_signature("c", "u", "false", "", "")

        # Different boolean values produce different signatures
        assert sig_true != sig_false

        # Verify "true" variant
        key = b"key"
        expected_input = "c:u:true::"
        expected_sig = hmac.new(key, expected_input.encode("utf-8"), hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode("ascii")
        assert sig_true == expected_b64
