"""Parse ADP correlation markers from GitHub comment/PR bodies.

Issue #1696: Single source of truth for parsing the `adp-*` HTML comment marker.
The format is produced by the worker's `correlation_marker.py` and consumed here
by the webhook Lambda to inherit cross-channel lineage.

Marker format (single-line HTML comment, fields space-separated):
    <!-- adp-correlation:{id} adp-root-human:{id} adp-is-human-rooted:{bool}
         adp-invocation:{id} adp-chain-depth:{n} -->

Parsing is lenient: each field is extracted independently via regex. A missing
field results in None for that key. This ensures backward compatibility with
old markers that lack adp-invocation/adp-chain-depth.
"""

from __future__ import annotations

import re
from typing import Any

# Regex patterns for each marker field — lenient, order-independent.
_MARKER_RE = re.compile(r"<!--\s+adp-correlation:")
_FIELD_PATTERNS: dict[str, re.Pattern] = {
    "correlation_id": re.compile(r"adp-correlation:([^\s]+)"),
    "root_human_id": re.compile(r"adp-root-human:([^\s]+)"),
    "is_human_rooted": re.compile(r"adp-is-human-rooted:([^\s]+)"),
    "invocation_id": re.compile(r"adp-invocation:([^\s]+)"),
    "chain_depth": re.compile(r"adp-chain-depth:([^\s]+)"),
    # Issue #2149: Explicit dispatch marker — distinguishes deliberate bot→bot
    # dispatch from incidental @agent-X prose in status comments.
    "dispatch_persona": re.compile(r"adp-dispatch:([^\s]+)"),
}


def parse_marker(text: str) -> dict[str, Any] | None:
    """Parse the ADP correlation marker from a comment or PR body.

    Scans the first 1000 bytes for the marker (markers are always prepended).
    Returns a dict with parsed fields, or None if no valid marker found.

    Returned dict keys:
        - correlation_id: str | None
        - root_human_id: str | None
        - is_human_rooted: bool
        - invocation_id: str | None  (the producing run's ADP_MESSAGE_ID)
        - chain_depth: int | None
        - dispatch_persona: str | None  (issue #2149: target persona for
          deliberate bot→bot dispatch; None when the comment is prose)

    Returns None if no marker is present or if required fields
    (correlation_id) are missing.
    """
    if not text:
        return None

    # Only scan the first 1000 bytes — marker is always at the top
    scan_region = text[:1000]

    if not _MARKER_RE.search(scan_region):
        return None

    result: dict[str, Any] = {}

    for field, pattern in _FIELD_PATTERNS.items():
        match = pattern.search(scan_region)
        if match:
            raw_value = match.group(1).rstrip("-->").rstrip()
            result[field] = raw_value
        else:
            result[field] = None

    # Required field: correlation_id must be present for a valid marker
    if not result.get("correlation_id"):
        return None

    # Type coercions
    rooted_str = result.get("is_human_rooted")
    result["is_human_rooted"] = rooted_str == "true" if rooted_str else False

    depth_str = result.get("chain_depth")
    if depth_str is not None:
        try:
            result["chain_depth"] = int(depth_str)
        except (ValueError, TypeError):
            result["chain_depth"] = None

    return result


def has_valid_marker(text: str) -> bool:
    """Check if text contains a valid ADP correlation marker.

    Quick check without full parsing — useful for the bot-guard gate.
    """
    if not text:
        return False
    return bool(_MARKER_RE.search(text[:1000]))
