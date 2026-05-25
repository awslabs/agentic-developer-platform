"""Correlation marker helper for outbound GitHub actions.

Prepends an invisible HTML comment containing correlation context to comment/PR
bodies so downstream webhook handlers can trace action provenance.

Phase 2-d of EPIC #779 (Agent identity + action provenance).
"""

from __future__ import annotations

import os


def prepend_correlation_marker(body: str) -> str:
    """Idempotently prepend HTML correlation marker to a comment/PR body.

    Reads correlation context from ADP_CORRELATION_ID, ADP_ROOT_HUMAN_ID,
    and ADP_IS_HUMAN_ROOTED env vars. No-op if:
      - Marker already present (first 500 bytes scanned)
      - Required env vars are missing

    Args:
        body: The comment or PR body text.

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

    marker = (
        f"<!-- adp-correlation:{corr} "
        f"adp-root-human:{root} "
        f"adp-is-human-rooted:{rooted} -->\n"
    )
    return marker + body
