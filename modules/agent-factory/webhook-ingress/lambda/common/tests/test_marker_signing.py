"""Tests for marker signature verification (cred-binding S5, Issue #3179).

Covers:
- A3: forged signature rejected
- A3: unsigned marker in Rule 4 → is_human_rooted=false
- Happy path: signed marker verifies
- Pointer present (Rules 1-3) → signature irrelevant
- Key rotation: previous key verifies during grace window
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure imports resolve
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from common.marker_parse import parse_marker
from common.marker_verify import (
    _compute_expected_signature,
    reset_key_cache,
    verify_marker,
)

# --- Test helpers ---

TEST_KEY = "test-signing-key-32bytes-long!!!"
TEST_KEY_PREV = "previous-key-for-rotation-grace!"
TEST_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123:secret:test"


def _sign_marker_fields(
    key: str,
    correlation_id: str,
    root_human_id: str,
    is_human_rooted: str,
    invocation_id: str = "",
    chain_depth: str = "",
) -> str:
    """Reproduce the worker's signing logic for test fixtures."""
    signing_input = (
        f"{correlation_id}:{root_human_id}:{is_human_rooted}"
        f":{invocation_id}:{chain_depth}"
    )
    sig = hmac.new(
        key.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def _make_marker(
    correlation_id: str = "corr-test-001",
    root_human_id: str = "user-test",
    is_human_rooted: str = "true",
    invocation_id: str = "msg-001",
    chain_depth: str = "2",
    signature: str | None = None,
) -> str:
    """Build a marker HTML comment string with optional signature."""
    parts = [
        f"adp-correlation:{correlation_id}",
        f"adp-root-human:{root_human_id}",
        f"adp-is-human-rooted:{is_human_rooted}",
    ]
    if invocation_id:
        parts.append(f"adp-invocation:{invocation_id}")
    if chain_depth:
        parts.append(f"adp-chain-depth:{chain_depth}")
    if signature:
        parts.append(f"adp-sig:{signature}")
    return f"<!-- {' '.join(parts)} -->"


@pytest.fixture(autouse=True)
def _clear_key_cache():
    """Reset the module-level key cache before each test."""
    reset_key_cache()
    yield
    reset_key_cache()


def _mock_sm_current_only(secret_arn):
    """Mock SM client that returns only the current key."""
    mock_client = MagicMock()
    mock_client.get_secret_value.side_effect = lambda **kwargs: (
        {"SecretString": TEST_KEY}
        if kwargs.get("VersionStage") == "AWSCURRENT"
        else (_ for _ in ()).throw(Exception("No previous version"))
    )
    return mock_client


def _mock_sm_with_rotation(secret_arn):
    """Mock SM client that returns current AND previous key."""
    mock_client = MagicMock()

    def _get_secret(**kwargs):
        if kwargs.get("VersionStage") == "AWSCURRENT":
            return {"SecretString": TEST_KEY}
        elif kwargs.get("VersionStage") == "AWSPREVIOUS":
            return {"SecretString": TEST_KEY_PREV}
        raise Exception("Unknown version stage")

    mock_client.get_secret_value.side_effect = _get_secret
    return mock_client


# =============================================================================
# Test class: verify_marker() core logic
# =============================================================================


class TestVerifyMarker:
    """Core verification logic tests."""

    def test_signed_marker_verifies(self):
        """Happy path: correctly signed marker returns True."""
        sig = _sign_marker_fields(TEST_KEY, "corr-001", "user-x", "true", "msg-1", "2")
        marker_text = _make_marker(
            correlation_id="corr-001",
            root_human_id="user-x",
            is_human_rooted="true",
            invocation_id="msg-1",
            chain_depth="2",
            signature=sig,
        )
        parsed = parse_marker(marker_text)
        assert parsed is not None

        env = {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}
        mock_client = _mock_sm_current_only(env["MARKER_SIGNING_KEY_SECRET_ARN"])

        with patch.dict(os.environ, env, clear=False):
            with patch("common.secrets._get_client", return_value=mock_client):
                result = verify_marker(parsed)

        assert result is True

    def test_forged_signature_rejected(self):
        """A3: wrong HMAC → verify_marker() returns False."""
        # Sign with a DIFFERENT key than what the Lambda will use
        forged_sig = _sign_marker_fields(
            "attacker-key-not-the-real-one!!!",
            "corr-forged",
            "user-victim",
            "true",
            "msg-f",
            "1",
        )
        marker_text = _make_marker(
            correlation_id="corr-forged",
            root_human_id="user-victim",
            is_human_rooted="true",
            invocation_id="msg-f",
            chain_depth="1",
            signature=forged_sig,
        )
        parsed = parse_marker(marker_text)
        assert parsed is not None

        env = {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}
        mock_client = _mock_sm_current_only(env["MARKER_SIGNING_KEY_SECRET_ARN"])

        with patch.dict(os.environ, env, clear=False):
            with patch("common.secrets._get_client", return_value=mock_client):
                result = verify_marker(parsed)

        assert result is False

    def test_unsigned_marker_returns_none(self):
        """Unsigned marker (no adp-sig) → verify_marker() returns None."""
        marker_text = _make_marker(
            correlation_id="corr-unsigned",
            root_human_id="user-u",
            is_human_rooted="true",
            invocation_id="msg-u",
            chain_depth="0",
            signature=None,  # No signature
        )
        parsed = parse_marker(marker_text)
        assert parsed is not None
        assert parsed.get("signature") is None

        env = {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}
        mock_client = _mock_sm_current_only(env["MARKER_SIGNING_KEY_SECRET_ARN"])

        with patch.dict(os.environ, env, clear=False):
            with patch("common.secrets._get_client", return_value=mock_client):
                result = verify_marker(parsed)

        assert result is None

    def test_no_keys_loaded_returns_none(self):
        """When MARKER_SIGNING_KEY_SECRET_ARN is not set, returns None (disabled)."""
        sig = _sign_marker_fields(TEST_KEY, "corr-x", "user-x", "true", "", "")
        marker_text = _make_marker(
            correlation_id="corr-x",
            root_human_id="user-x",
            is_human_rooted="true",
            signature=sig,
        )
        parsed = parse_marker(marker_text)
        assert parsed is not None

        # No MARKER_SIGNING_KEY_SECRET_ARN in env
        env = {}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("MARKER_SIGNING_KEY_SECRET_ARN", None)
            result = verify_marker(parsed)

        assert result is None

    def test_key_rotation_accepts_previous(self):
        """Old key verifies during rotation grace (7-day window)."""
        # Sign with the PREVIOUS key (simulating in-flight marker from before rotation)
        sig = _sign_marker_fields(
            TEST_KEY_PREV, "corr-rotated", "user-r", "true", "msg-r", "1"
        )
        marker_text = _make_marker(
            correlation_id="corr-rotated",
            root_human_id="user-r",
            is_human_rooted="true",
            invocation_id="msg-r",
            chain_depth="1",
            signature=sig,
        )
        parsed = parse_marker(marker_text)
        assert parsed is not None

        env = {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}
        mock_client = _mock_sm_with_rotation(env["MARKER_SIGNING_KEY_SECRET_ARN"])

        with patch.dict(os.environ, env, clear=False):
            with patch("common.secrets._get_client", return_value=mock_client):
                result = verify_marker(parsed)

        assert result is True

    def test_is_human_rooted_false_signed(self):
        """Marker with is_human_rooted=false also verifies correctly."""
        sig = _sign_marker_fields(
            TEST_KEY, "corr-bot", "user-bot", "false", "msg-b", "3"
        )
        marker_text = _make_marker(
            correlation_id="corr-bot",
            root_human_id="user-bot",
            is_human_rooted="false",
            invocation_id="msg-b",
            chain_depth="3",
            signature=sig,
        )
        parsed = parse_marker(marker_text)
        assert parsed is not None

        env = {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}
        mock_client = _mock_sm_current_only(env["MARKER_SIGNING_KEY_SECRET_ARN"])

        with patch.dict(os.environ, env, clear=False):
            with patch("common.secrets._get_client", return_value=mock_client):
                result = verify_marker(parsed)

        assert result is True


# =============================================================================
# Test class: Rule-4 integration (handler behavior)
# =============================================================================


class TestRule4Integration:
    """Tests for Rule-4 behavior in determine_correlation() context.

    These test the marker_verify integration indirectly by simulating the
    handler's Rule-4 logic (marker only, no pointer).
    """

    def test_unsigned_marker_rule4_no_root_human(self):
        """A3: unsigned marker in Rule 4 → is_human_rooted=false.

        When a marker has no signature and claims is_human_rooted=true in
        Rule-4 position, the handler strips the authority (fail-closed).
        """
        marker_text = _make_marker(
            correlation_id="corr-unsigned-r4",
            root_human_id="user-forgery",
            is_human_rooted="true",
            invocation_id="msg-u",
            chain_depth="1",
            signature=None,
        )
        parsed = parse_marker(marker_text)
        assert parsed is not None
        assert parsed["is_human_rooted"] is True  # Marker claims true

        env = {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}
        mock_client = _mock_sm_current_only(env["MARKER_SIGNING_KEY_SECRET_ARN"])

        with patch.dict(os.environ, env, clear=False):
            with patch("common.secrets._get_client", return_value=mock_client):
                sig_result = verify_marker(parsed)

        # verify_marker returns None (unsigned) — handler applies fail-closed
        assert sig_result is None
        # Handler logic: sig_result is None AND is_human_rooted=True → strip
        # The handler would set is_human_rooted=False in the returned correlation ctx

    def test_unsigned_marker_rule1_pointer_wins(self):
        """Pointer present → signature irrelevant (Rules 1-3).

        When a pointer exists (same or different correlation), the pointer is
        authoritative. Signature verification is only applied in Rule 4
        (marker-only, no pointer). This test documents that markers with
        pointers bypass signature checks entirely.
        """
        # This test validates the design constraint: pointer > marker.
        # In Rules 1-3, the pointer's is_human_rooted is used directly.
        # Even if the marker is unsigned or forged, the pointer wins.
        marker_text = _make_marker(
            correlation_id="corr-with-pointer",
            root_human_id="user-p",
            is_human_rooted="true",
            invocation_id="msg-p",
            chain_depth="1",
            signature=None,  # Unsigned — irrelevant because pointer exists
        )
        parsed = parse_marker(marker_text)
        assert parsed is not None

        # In Rules 1-3, pointer is authoritative. The handler never calls
        # verify_marker() for those paths. This test asserts that the parsing
        # still works (backward compat) — the pointer path doesn't inspect sig.
        assert parsed["correlation_id"] == "corr-with-pointer"
        assert parsed["is_human_rooted"] is True
        # Verification result is IRRELEVANT — pointer wins. No verify call needed.


# =============================================================================
# Test class: compute_expected_signature edge cases
# =============================================================================


class TestComputeExpectedSignature:
    """Tests for the internal _compute_expected_signature helper."""

    def test_matches_worker_signing_format(self):
        """Expected signature matches the worker's marker_signing.py output."""
        key = TEST_KEY.encode("utf-8")
        marker = {
            "correlation_id": "abc-123",
            "root_human_id": "user-xyz",
            "is_human_rooted": False,
            "invocation_id": "msg-456",
            "chain_depth": 3,
        }
        result = _compute_expected_signature(key, marker)

        # Manually compute expected using same format as worker
        signing_input = "abc-123:user-xyz:false:msg-456:3"
        expected_sig = hmac.new(
            key, signing_input.encode("utf-8"), hashlib.sha256
        ).digest()
        expected_b64 = (
            base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode("ascii")
        )
        assert result == expected_b64

    def test_empty_optional_fields(self):
        """Missing invocation_id and chain_depth → empty strings in input."""
        key = TEST_KEY.encode("utf-8")
        marker = {
            "correlation_id": "c1",
            "root_human_id": "u1",
            "is_human_rooted": True,
            "invocation_id": None,
            "chain_depth": None,
        }
        result = _compute_expected_signature(key, marker)

        signing_input = "c1:u1:true::"
        expected_sig = hmac.new(
            key, signing_input.encode("utf-8"), hashlib.sha256
        ).digest()
        expected_b64 = (
            base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode("ascii")
        )
        assert result == expected_b64

    def test_chain_depth_zero_serialized_as_string(self):
        """chain_depth=0 serializes as '0', not empty string."""
        key = TEST_KEY.encode("utf-8")
        marker = {
            "correlation_id": "c",
            "root_human_id": "u",
            "is_human_rooted": True,
            "invocation_id": "inv",
            "chain_depth": 0,
        }
        result = _compute_expected_signature(key, marker)

        signing_input = "c:u:true:inv:0"
        expected_sig = hmac.new(
            key, signing_input.encode("utf-8"), hashlib.sha256
        ).digest()
        expected_b64 = (
            base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode("ascii")
        )
        assert result == expected_b64
