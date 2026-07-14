"""Marker signature verification (cred-binding S5, Issue #3179).

Verifies the `adp-sig` HMAC-SHA256 signature in correlation markers before
trusting marker-borne `root_human_id`/`is_human_rooted` in Rule-4 position
(marker-only, cross-channel first hop — no pointer).

Unsigned or forged markers in Rule-4 position confer no root-human authority
and are treated as a new bot-rooted chain (fail-closed).

Key rotation: accepts the CURRENT or PREVIOUS secret version (7-day grace
window during rotation). This ensures in-flight markers signed with the old
key are still accepted during rollover.

Signing input format (must match worker's marker_signing.py):
    f"{correlation_id}:{root_human_id}:{is_human_rooted}:{invocation_id}:{chain_depth}"
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Cached verification keys (current + previous) — loaded once per Lambda
# execution environment (cold start). Warm invocations reuse cached values.
_verification_keys: list[bytes] | None = None
_keys_loaded: bool = False


def _load_verification_keys() -> list[bytes]:
    """Load the HMAC verification key(s) from Secrets Manager.

    Returns a list of keys to try (current first, then previous version if
    available). Returns empty list if the secret ARN env var is unset or the
    secret cannot be read (graceful degradation — verification skipped).
    """
    global _verification_keys, _keys_loaded

    if _keys_loaded:
        return _verification_keys or []

    _keys_loaded = True

    secret_arn = os.environ.get("MARKER_SIGNING_KEY_SECRET_ARN", "")
    if not secret_arn:
        logger.debug(
            "MARKER_SIGNING_KEY_SECRET_ARN not set; marker verification disabled"
        )
        _verification_keys = []
        return []

    keys: list[bytes] = []

    try:
        from common.secrets import _get_client

        client = _get_client()

        # Fetch current version
        resp = client.get_secret_value(SecretId=secret_arn, VersionStage="AWSCURRENT")
        current_key = resp["SecretString"].encode("utf-8")
        keys.append(current_key)

        # Attempt to fetch previous version for rotation grace (7-day window).
        # If no previous version exists, this is fine — single key only.
        try:
            resp_prev = client.get_secret_value(
                SecretId=secret_arn, VersionStage="AWSPREVIOUS"
            )
            prev_key = resp_prev["SecretString"].encode("utf-8")
            if prev_key != current_key:
                keys.append(prev_key)
        except Exception:
            # No previous version — expected for fresh secrets
            pass

        logger.info(
            "Marker verification keys loaded (%d key(s)) from %s",
            len(keys),
            secret_arn,
        )
    except Exception as exc:
        logger.warning(
            "Failed to load marker verification keys from %s: %s. "
            "Marker verification disabled (graceful degradation).",
            secret_arn,
            exc,
        )
        keys = []

    _verification_keys = keys
    return keys


def _compute_expected_signature(key: bytes, marker: dict[str, Any]) -> str:
    """Compute the expected HMAC-SHA256 signature for a parsed marker.

    Uses the same canonical signing input format as the worker's
    marker_signing.py to ensure round-trip fidelity.
    """
    # Reconstruct the signing input from parsed marker fields.
    # is_human_rooted: the parsed marker coerces to bool, but the signing
    # input uses lowercase "true"/"false" string.
    correlation_id = marker.get("correlation_id") or ""
    root_human_id = marker.get("root_human_id") or ""

    # is_human_rooted is a bool after parse_marker() coercion
    is_human_rooted_val = marker.get("is_human_rooted")
    if isinstance(is_human_rooted_val, bool):
        is_human_rooted_str = "true" if is_human_rooted_val else "false"
    else:
        is_human_rooted_str = (
            str(is_human_rooted_val).lower() if is_human_rooted_val else "false"
        )

    invocation_id = marker.get("invocation_id") or ""
    chain_depth = marker.get("chain_depth")
    chain_depth_str = str(chain_depth) if chain_depth is not None else ""

    signing_input = (
        f"{correlation_id}:{root_human_id}:{is_human_rooted_str}"
        f":{invocation_id}:{chain_depth_str}"
    )

    sig = hmac.new(key, signing_input.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def verify_marker(marker: dict[str, Any]) -> bool | None:
    """Verify the HMAC-SHA256 signature on a parsed correlation marker.

    Args:
        marker: The dict returned by parse_marker() — must include the
                'signature' field (adp-sig value, base64url, no padding).

    Returns:
        True:  signature is valid (current or previous key matches)
        False: signature is present but INVALID (forged or corrupted)
        None:  verification cannot be performed (no keys loaded, or marker
               has no signature field). Caller decides policy:
               - Rules 1-3 (pointer present): None is fine, pointer is authoritative
               - Rule 4 (marker only): None means unsigned → fail-closed
    """
    keys = _load_verification_keys()

    sig_value = marker.get("signature")
    if not sig_value:
        # No signature field in marker — unsigned marker
        if keys:
            # Keys are available but marker is unsigned → caller applies policy
            return None
        else:
            # No keys loaded → verification disabled → pass through
            return None

    if not keys:
        # Signature present but no keys loaded — cannot verify.
        # Graceful degradation: treat as unverifiable (None).
        return None

    # Try each key (current first, then previous for rotation grace)
    for key in keys:
        expected = _compute_expected_signature(key, marker)
        if hmac.compare_digest(sig_value, expected):
            return True

    # No key matched — signature is forged or corrupted
    return False


def reset_key_cache() -> None:
    """Reset the cached verification keys (for testing only)."""
    global _verification_keys, _keys_loaded
    _verification_keys = None
    _keys_loaded = False
