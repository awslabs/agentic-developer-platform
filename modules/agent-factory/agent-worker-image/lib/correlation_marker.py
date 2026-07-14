"""Correlation marker helper for outbound GitHub actions.

Prepends an invisible HTML comment containing correlation context to comment/PR
bodies so downstream webhook handlers can trace action provenance.

Phase 2-d of EPIC #779 (Agent identity + action provenance).
"""

from __future__ import annotations

import logging
import os

from lib.marker_signing import compute_signature

logger = logging.getLogger(__name__)


def prepend_correlation_marker(body: str, *, dispatch_persona: str | None = None) -> str:
    """Idempotently prepend HTML correlation marker to a comment/PR body.

    Reads correlation context from ADP_CORRELATION_ID, ADP_ROOT_HUMAN_ID,
    ADP_IS_HUMAN_ROOTED, ADP_MESSAGE_ID, and ADP_CHAIN_DEPTH env vars. No-op if:
      - Marker already present (first 500 bytes scanned)
      - Required env vars are missing (ADP_CORRELATION_ID, ADP_ROOT_HUMAN_ID)

    Args:
        body: The comment or PR body text.
        dispatch_persona: Optional. When set, includes ``adp-dispatch:<persona>``
            in the marker, signaling an intentional cross-issue bot→bot dispatch.
            Status/boilerplate comments MUST NOT pass this parameter (issue #2149).

    Returns:
        Body with marker prepended, or original body unchanged.
    """
    # Idempotency: don't double-prepend
    if "<!-- adp-correlation:" in body[:500]:
        return body

    corr = os.environ.get("ADP_CORRELATION_ID")
    root = os.environ.get("ADP_ROOT_HUMAN_ID")
    rooted = os.environ.get("ADP_IS_HUMAN_ROOTED", "false")

    if not corr or not root:
        return body  # Nothing to inject — fail-safe

    # Optional lineage fields (issue #1696): invocation ID and chain depth.
    # These enable cross-channel lineage inheritance by the inbound webhook.
    invocation = os.environ.get("ADP_MESSAGE_ID", "")
    chain_depth = os.environ.get("ADP_CHAIN_DEPTH", "")

    # Build marker — single-line, lenient regex-parsed by the webhook Lambda.
    # Missing optional fields are simply omitted (backward-compatible).
    parts = [
        f"adp-correlation:{corr}",
        f"adp-root-human:{root}",
        f"adp-is-human-rooted:{rooted}",
    ]
    if invocation:
        parts.append(f"adp-invocation:{invocation}")
    if chain_depth:
        parts.append(f"adp-chain-depth:{chain_depth}")

    # Issue #2149: dispatch marker for intentional cross-issue bot→bot triggers.
    if dispatch_persona:
        parts.append(f"adp-dispatch:{dispatch_persona}")

    # Issue #3178 (cred-binding S4): HMAC-SHA256 signature over marker fields.
    # Ships dark — S5 verification is separate. Graceful degradation: if key
    # is unavailable, marker is written unsigned (no crash).
    sig = compute_signature(
        correlation_id=corr,
        root_human_id=root,
        is_human_rooted=rooted,
        invocation_id=invocation,
        chain_depth=chain_depth,
    )
    if sig:
        parts.append(f"adp-sig:{sig}")

    marker = f"<!-- {' '.join(parts)} -->\n"
    return marker + body
