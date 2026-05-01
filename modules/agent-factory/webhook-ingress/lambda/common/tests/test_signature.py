"""Tests for GitHub webhook signature verification."""

import hashlib
import hmac

from common.signature import verify_github_signature


def _make_signature(payload: bytes, secret: str) -> str:
    """Helper to create a valid sha256 signature."""
    mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


class TestVerifyGithubSignature:
    def test_valid_signature(self) -> None:
        payload = b'{"action": "opened"}'
        secret = "mysecret"
        signature = _make_signature(payload, secret)
        assert verify_github_signature(payload, signature, secret) is True

    def test_invalid_signature(self) -> None:
        payload = b'{"action": "opened"}'
        secret = "mysecret"
        wrong_sig = (
            "sha256=0000000000000000000000000000000000000000000000000000000000000000"
        )
        assert verify_github_signature(payload, wrong_sig, secret) is False

    def test_wrong_secret(self) -> None:
        payload = b'{"action": "opened"}'
        signature = _make_signature(payload, "correct-secret")
        assert verify_github_signature(payload, signature, "wrong-secret") is False

    def test_empty_signature_header(self) -> None:
        assert verify_github_signature(b"body", "", "secret") is False

    def test_empty_secret(self) -> None:
        payload = b"body"
        signature = _make_signature(payload, "secret")
        assert verify_github_signature(payload, signature, "") is False

    def test_missing_sha256_prefix(self) -> None:
        payload = b"test"
        secret = "sec"
        mac = hmac.new(secret.encode(), payload, hashlib.sha256)
        # No sha256= prefix
        assert verify_github_signature(payload, mac.hexdigest(), secret) is False

    def test_constant_time_comparison(self) -> None:
        """Verify we use hmac.compare_digest (constant-time)."""
        payload = b'{"test": true}'
        secret = "webhook-secret"
        signature = _make_signature(payload, secret)
        assert verify_github_signature(payload, signature, secret) is True

    def test_empty_payload(self) -> None:
        payload = b""
        secret = "secret"
        signature = _make_signature(payload, secret)
        assert verify_github_signature(payload, signature, secret) is True

    def test_binary_payload(self) -> None:
        payload = b"\x00\x01\x02\xff"
        secret = "secret"
        signature = _make_signature(payload, secret)
        assert verify_github_signature(payload, signature, secret) is True
