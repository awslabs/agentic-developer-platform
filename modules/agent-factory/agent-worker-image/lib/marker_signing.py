"""HMAC-SHA256 signing for correlation markers (cred-binding S4, Issue #3178).

Signs the correlation marker fields so downstream verification (S5) can confirm
the marker was produced by an authorized worker, not forged by a malicious actor.

Signing input string spec:
    f"{correlation_id}:{root_human_id}:{is_human_rooted}:{invocation_id}:{chain_depth}"

- `is_human_rooted` is serialized as lowercase "true"/"false"
- Missing optional fields (invocation_id, chain_depth) use empty string
- Signature is base64url-encoded (no padding) HMAC-SHA256

Ships dark: S5 verification is a separate story. Until then, signed markers are
additive — the `adp-sig:` field is ignored by current parsers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Cache the signing key for the lifetime of the process. The worker is a
# single-run pod (one SQS message → one agent run → pod terminates), so
# caching here avoids repeated SM calls within a single run while still
# picking up rotated keys on the next pod launch.
_signing_key: bytes | None = None
_key_loaded: bool = False


def _load_signing_key() -> bytes | None:
    """Load the HMAC signing key from Secrets Manager (once per process).

    Returns None if the secret name env var is unset or the secret cannot be
    read (graceful degradation — markers will be unsigned).
    """
    global _signing_key, _key_loaded

    if _key_loaded:
        return _signing_key

    _key_loaded = True

    secret_id = os.environ.get("ADP_MARKER_SIGNING_KEY_SECRET")
    if not secret_id:
        logger.debug("ADP_MARKER_SIGNING_KEY_SECRET not set; markers will be unsigned")
        return None

    try:
        region = os.environ.get("AWS_REGION", "us-east-1")
        client = boto3.client("secretsmanager", region_name=region)
        resp = client.get_secret_value(SecretId=secret_id)
        key_str = resp["SecretString"]
        _signing_key = key_str.encode("utf-8")
        logger.info("Marker signing key loaded from %s", secret_id)
        return _signing_key
    except (ClientError, KeyError, Exception) as exc:
        logger.warning(
            "Failed to load marker signing key from %s: %s. "
            "Markers will be unsigned (graceful degradation).",
            secret_id,
            exc,
        )
        return None


def compute_signature(
    correlation_id: str,
    root_human_id: str,
    is_human_rooted: str,
    invocation_id: str = "",
    chain_depth: str = "",
) -> str | None:
    """Compute HMAC-SHA256 signature over the canonical marker fields.

    Args:
        correlation_id: The correlation ID.
        root_human_id: The root human ID.
        is_human_rooted: Lowercase "true" or "false".
        invocation_id: Optional invocation/message ID (empty string if absent).
        chain_depth: Optional chain depth (empty string if absent).

    Returns:
        Base64url-encoded (no padding) signature string, or None if signing
        key is unavailable.
    """
    key = _load_signing_key()
    if key is None:
        return None

    # Canonical signing input — order and separator are part of the spec.
    # S5 verification MUST use the same format.
    signing_input = (
        f"{correlation_id}:{root_human_id}:{is_human_rooted}:{invocation_id}:{chain_depth}"
    )

    sig = hmac.new(key, signing_input.encode("utf-8"), hashlib.sha256).digest()

    # base64url encoding without padding (URL-safe, compact for HTML comments)
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def reset_key_cache() -> None:
    """Reset the cached signing key (for testing only)."""
    global _signing_key, _key_loaded
    _signing_key = None
    _key_loaded = False
