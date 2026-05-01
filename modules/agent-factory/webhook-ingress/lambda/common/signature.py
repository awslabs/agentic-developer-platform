"""HMAC-SHA256 signature verification for webhook payloads.

Used by channel Lambdas to validate that incoming requests genuinely
originated from the expected source (GitHub, Slack, etc.).
"""

import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)


def verify_github_signature(payload_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify X-Hub-Signature-256 header against the webhook secret.

    Args:
        payload_body: Raw request body bytes.
        signature_header: Value of X-Hub-Signature-256 header (e.g. "sha256=abc123...").
        secret: The webhook secret stored in Secrets Manager.

    Returns:
        True if signature is valid, False otherwise.
    """
    if not signature_header or not secret:
        logger.warning("Missing signature header or secret")
        return False

    prefix = "sha256="
    if not signature_header.startswith(prefix):
        logger.warning("Signature header missing sha256= prefix")
        return False

    expected_sig = signature_header[len(prefix):]
    computed_sig = hmac.new(
        secret.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed_sig, expected_sig)
