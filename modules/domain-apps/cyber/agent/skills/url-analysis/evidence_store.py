"""
Evidence persistence — upload screenshots + Evidence envelopes to S3.

Public API:
    upload_screenshot(png_bytes, *, run_id, url_index, shot_index) -> str
    upload_evidence_envelope(evidence, *, run_id, url_index) -> str
    presign(s3_uri, *, expires_in=86400) -> str

Env:
    URL_ANALYSIS_EVIDENCE_BUCKET — deterministic bucket name. If unset, all
    upload functions return "" and the skill falls back to inline base64.

Safety rails:
    - upload_screenshot rejects non-PNG and >5MB payloads
    - upload_evidence_envelope strips image_base64 and rejects >2MB envelopes
    - Only bytes are accepted (never file paths)

Issue #499
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from evidence_schema import Evidence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PNG_MAGIC = b"\x89PNG"
_MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024  # 5 MB
_MAX_ENVELOPE_BYTES = 2 * 1024 * 1024  # 2 MB
_DEFAULT_PRESIGN_EXPIRES = 86400  # 24 hours

# ---------------------------------------------------------------------------
# Lazy S3 client (initialized on first use)
# ---------------------------------------------------------------------------

_s3_client = None


def _get_bucket() -> str | None:
    """Return the bucket name from env, or None if not configured."""
    return os.environ.get("URL_ANALYSIS_EVIDENCE_BUCKET") or None


def _get_s3_client():
    """Lazy-init S3 client with signature v4 (required for presigned URLs)."""
    global _s3_client
    if _s3_client is None:
        region = os.environ.get("AWS_REGION", "us-east-1")
        _s3_client = boto3.client(
            "s3",
            region_name=region,
            config=BotoConfig(signature_version="s3v4"),
        )
    return _s3_client


def _reset_client() -> None:
    """Reset the cached client (for testing)."""
    global _s3_client
    _s3_client = None


# ---------------------------------------------------------------------------
# Key layout helpers
# ---------------------------------------------------------------------------


def _build_key(
    *,
    tenant_id: str,
    issue_number: str | int,
    run_id: str,
    url_index: int,
    filename: str,
) -> str:
    """
    Build the S3 object key following the layout:
    tenant=<tenant_id>/issue=<issue_number>/run=<run_id>/url-<n>/<filename>
    """
    return (
        f"tenant={tenant_id}/issue={issue_number}/"
        f"run={run_id}/url-{url_index}/{filename}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def upload_screenshot(
    png_bytes: bytes,
    *,
    tenant_id: str = "adp-default",
    issue_number: str | int = "0",
    run_id: str,
    url_index: int,
    shot_index: int,
) -> str:
    """
    Upload a screenshot PNG to S3. Returns the s3:// URI, or "" if bucket is
    not configured (graceful fallback to inline base64).

    Raises ValueError if:
    - png_bytes does not start with PNG magic bytes
    - png_bytes exceeds 5 MB
    """
    bucket = _get_bucket()
    if not bucket:
        logger.debug("URL_ANALYSIS_EVIDENCE_BUCKET not set; skipping upload")
        return ""

    # Safety: PNG magic check
    if not png_bytes[:4] == _PNG_MAGIC:
        raise ValueError(
            f"Rejected non-PNG upload: first 4 bytes are {png_bytes[:4]!r}, "
            f"expected PNG magic {_PNG_MAGIC!r}"
        )

    # Safety: size check
    if len(png_bytes) > _MAX_SCREENSHOT_BYTES:
        raise ValueError(
            f"Rejected oversize screenshot: {len(png_bytes)} bytes > "
            f"{_MAX_SCREENSHOT_BYTES} byte limit"
        )

    key = _build_key(
        tenant_id=tenant_id,
        issue_number=issue_number,
        run_id=run_id,
        url_index=url_index,
        filename=f"screenshot-{shot_index}.png",
    )

    s3 = _get_s3_client()
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=png_bytes,
            ContentType="image/png",
        )
    except ClientError as e:
        # Infra-level failure (NoSuchBucket, AccessDenied, etc.) degrades
        # gracefully to the inline-base64 fallback — same contract as
        # URL_ANALYSIS_EVIDENCE_BUCKET being unset. An upload miss must
        # never crash a URL-analysis run.
        logger.warning(
            "S3 upload failed for screenshot; falling back to inline base64: %s",
            e,
        )
        return ""

    uri = f"s3://{bucket}/{key}"
    logger.info("Uploaded screenshot: %s (%d bytes)", uri, len(png_bytes))
    return uri


def upload_evidence_envelope(
    evidence: "Evidence",
    *,
    tenant_id: str = "adp-default",
    issue_number: str | int = "0",
    run_id: str,
    url_index: int,
) -> str:
    """
    Serialize Evidence to JSON (stripping image_base64 from screenshots),
    upload to S3, and return the s3:// URI.

    Returns "" if bucket is not configured.
    Raises ValueError if serialized size exceeds 2 MB.
    """
    bucket = _get_bucket()
    if not bucket:
        logger.debug("URL_ANALYSIS_EVIDENCE_BUCKET not set; skipping upload")
        return ""

    # Serialize with image_base64 excluded to keep the envelope compact
    data = evidence.model_dump(mode="json")
    for screenshot in data.get("screenshots", []):
        screenshot.pop("image_base64", None)

    import json

    json_bytes = json.dumps(data, indent=2, default=str).encode("utf-8")

    # Safety: size check
    if len(json_bytes) > _MAX_ENVELOPE_BYTES:
        raise ValueError(
            f"Rejected oversize evidence envelope: {len(json_bytes)} bytes > "
            f"{_MAX_ENVELOPE_BYTES} byte limit"
        )

    key = _build_key(
        tenant_id=tenant_id,
        issue_number=issue_number,
        run_id=run_id,
        url_index=url_index,
        filename="evidence.json",
    )

    s3 = _get_s3_client()
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json_bytes,
            ContentType="application/json",
        )
    except ClientError as e:
        logger.warning(
            "S3 upload failed for evidence envelope; skipping: %s", e
        )
        return ""

    uri = f"s3://{bucket}/{key}"
    logger.info("Uploaded evidence envelope: %s (%d bytes)", uri, len(json_bytes))
    return uri


def presign(s3_uri: str, *, expires_in: int = _DEFAULT_PRESIGN_EXPIRES) -> str:
    """
    Generate a presigned GET URL for an s3:// URI.

    Args:
        s3_uri: Full s3://bucket/key URI
        expires_in: URL validity in seconds (default 24h, max 24h)

    Returns:
        Presigned HTTPS URL, or "" on error.
    """
    # Empty URI = upstream upload was skipped/failed; return "" so the
    # caller renders the fallback (inline base64) instead of crashing.
    if not s3_uri:
        return ""

    # Clamp to 24h max
    expires_in = min(expires_in, _DEFAULT_PRESIGN_EXPIRES)

    parsed = urlparse(s3_uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Expected s3:// URI, got: {s3_uri}")

    bucket = parsed.netloc
    key = parsed.path.lstrip("/")

    s3 = _get_s3_client()
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in,
        )
        return url
    except ClientError as e:
        logger.error("Failed to generate presigned URL for %s: %s", s3_uri, e)
        return ""
