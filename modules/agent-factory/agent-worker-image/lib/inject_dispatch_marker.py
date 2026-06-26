#!/usr/bin/env python3
"""Inject adp-dispatch:<persona> into a comment body's correlation marker.

Issue #2149 (Story 3): The gh-wrapper calls this for cross-issue bot→bot
dispatches — comments where an agent deliberately triggers another persona
via `@agent-X` on a different issue. The webhook parser requires the
`adp-dispatch:<persona>` field to distinguish deliberate dispatch from
incidental @-mention prose in status comments.

Three cases:
  1. Body already has a marker WITHOUT adp-dispatch → inject the field
  2. Body already has a marker WITH adp-dispatch → no-op (idempotent)
  3. Body has no marker → prepend a full marker including adp-dispatch

Usage:
    python3 -m lib.inject_dispatch_marker <persona>

Reads the comment body from stdin, writes the modified body to stdout.
Exit code is always 0 (fail-soft by design — gh-wrapper must never block).
"""

from __future__ import annotations

import logging
import re
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("inject_dispatch_marker")

# Matches the ADP correlation marker HTML comment
_MARKER_RE = re.compile(r"(<!--\s+adp-correlation:[^\n]*?)\s*-->")


def inject_dispatch(body: str, persona: str) -> str:
    """Inject adp-dispatch:<persona> into the body's correlation marker.

    Idempotent: if adp-dispatch is already present, returns body unchanged.
    If no marker exists, prepends a full marker with the dispatch field.
    """
    if not persona:
        return body

    dispatch_field = f"adp-dispatch:{persona}"

    # Case 2: already has dispatch marker — no-op
    if dispatch_field in body[:1000]:
        return body

    # Case 1: has a marker without dispatch — inject the field before -->
    match = _MARKER_RE.search(body[:1000])
    if match:
        # Check if ANY adp-dispatch is already present (different persona)
        if "adp-dispatch:" in match.group(0):
            return body  # Don't override an existing dispatch target
        # Inject before the closing -->
        original_marker = match.group(0)
        new_marker = f"{match.group(1)} {dispatch_field} -->"
        return body.replace(original_marker, new_marker, 1)

    # Case 3: no marker — prepend a full one using env vars
    from lib.correlation_marker import prepend_correlation_marker

    return prepend_correlation_marker(body, dispatch_persona=persona)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        # No persona arg — pass through unchanged
        sys.stdout.write(sys.stdin.read())
        return 0

    persona = argv[1]
    try:
        body = sys.stdin.read()
    except Exception:
        return 0

    try:
        result = inject_dispatch(body, persona)
        sys.stdout.write(result)
    except Exception as exc:
        # Fail-soft: on any error, pass through the original body unchanged
        logger.warning("inject_dispatch_marker failed (non-fatal): %s", exc)
        sys.stdout.write(body)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
