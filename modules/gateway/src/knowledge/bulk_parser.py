"""Bulk-upload file parser for the knowledge-assets registry.

Issue #2045: Relocated from agent_context.api.bulk_parser into the gateway.
Original: Issue #1792 (Story C of E10 #1736).
Design reference: docs/agent-context/design-1736-knowledge-asset-registry.md S5.1.

Parses a plain-text file with one asset per line. Supports:
  - Comments (lines starting with #)
  - Simple format: source_ref
  - Extended format: source_ref | display_name | tag1:val1, tag2:val2

Asset type is inferred from source_ref pattern:
  - https://github.com/* or git@github.com:* -> repo
  - s3://* -> doc
  - Any other http(s):// -> url
"""

from __future__ import annotations

import re

from src.knowledge.schemas import BulkPreviewItem, BulkRejectedItem

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FILE_SIZE_BYTES = 1 * 1024 * 1024  # 1 MB
MAX_LINES = 500

# ---------------------------------------------------------------------------
# Type inference patterns (matches type_registry.py source_ref_pattern)
# ---------------------------------------------------------------------------

_GITHUB_PATTERN = re.compile(r"^(https://github\.com/|git@github\.com:)")
_S3_PATTERN = re.compile(r"^s3://")
_HTTP_PATTERN = re.compile(r"^https?://")


def infer_asset_type(source_ref: str) -> str | None:
    """Infer asset_type from source_ref pattern.

    Returns None if no type can be inferred (unsupported protocol).
    """
    if _GITHUB_PATTERN.match(source_ref):
        return "repo"
    if _S3_PATTERN.match(source_ref):
        return "doc"
    if _HTTP_PATTERN.match(source_ref):
        return "url"
    return None


def _parse_tags(tags_str: str) -> dict[str, str]:
    """Parse comma-separated key:value tags.

    Example: "team:platform, priority:high" -> {"team": "platform", "priority": "high"}
    """
    tags: dict[str, str] = {}
    for part in tags_str.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            key, value = part.split(":", 1)
            tags[key.strip()] = value.strip()
        else:
            # Tag without value — use empty string
            tags[part] = ""
    return tags


def parse_bulk_file(
    content: str,
) -> tuple[list[BulkPreviewItem], list[BulkRejectedItem], int, int]:
    """Parse a bulk upload file and return valid items, rejected items, counts.

    Returns:
        (valid_items, rejected_items, total_lines, skipped_comments)
    """
    valid: list[BulkPreviewItem] = []
    rejected: list[BulkRejectedItem] = []
    total_lines = 0
    skipped_comments = 0

    for line_num, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        total_lines += 1

        # Skip empty lines
        if not line:
            skipped_comments += 1
            continue

        # Skip comments
        if line.startswith("#"):
            skipped_comments += 1
            continue

        # Parse line: source_ref | display_name | tags
        parts = [p.strip() for p in line.split("|")]
        source_ref = parts[0]
        display_name: str | None = parts[1] if len(parts) > 1 and parts[1] else None
        tags: dict[str, str] = {}
        if len(parts) > 2 and parts[2]:
            tags = _parse_tags(parts[2])

        # Validate source_ref is non-empty
        if not source_ref:
            rejected.append(
                BulkRejectedItem(
                    line=line_num,
                    source_ref="",
                    reason="Empty source_ref",
                )
            )
            continue

        # Infer asset type
        asset_type = infer_asset_type(source_ref)
        if asset_type is None:
            rejected.append(
                BulkRejectedItem(
                    line=line_num,
                    source_ref=source_ref,
                    reason="Cannot infer asset_type from source_ref",
                )
            )
            continue

        valid.append(
            BulkPreviewItem(
                line=line_num,
                source_ref=source_ref,
                asset_type=asset_type,
                display_name=display_name,
                tags=tags,
            )
        )

    return valid, rejected, total_lines, skipped_comments
