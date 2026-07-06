"""Asset type registry — config-driven mapping from asset_type to ingestion steps.

Issue #1797 (Story H of E10 #1736).
Design reference: docs/agent-context/design-1736-knowledge-asset-registry.md §6.4.

Adding a new asset type is a config + handler change, not a schema migration.
Each entry specifies:
  - steps: ordered list of ingestion pipeline steps
  - timeout: maximum wall-clock seconds for the full pipeline
  - source_ref_pattern: regex that source_ref must match at registration time
  - requires_github_app: whether the type needs a GitHub App installation
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# ASSET_TYPE_REGISTRY — single source of truth for type→steps mapping
# ---------------------------------------------------------------------------
# Consumed by:
#   1. API registration (validate source_ref against pattern)
#   2. dispatch_ingestion() (look up steps for SQS message)
#   3. Worker (route to handler based on content_type in message)
#
# Adding a new type requires:
#   1. An entry here (steps, timeout, pattern)
#   2. A handler script (ingest-<type>.py) in images/ingestion/
#   3. Zero schema migration — asset_type is VARCHAR, metadata is JSONB
# ---------------------------------------------------------------------------

ASSET_TYPE_REGISTRY: dict[str, dict[str, Any]] = {
    "repo": {
        "steps": ["s3_upload", "cgc", "deepwiki", "graphrag"],
        "timeout": 900,
        "source_ref_pattern": r"^(https://github\.com/|git@github\.com:)",
        "requires_github_app": True,
    },
    "url": {
        "steps": ["s3_upload", "graphrag"],
        "timeout": 600,
        "source_ref_pattern": r"^https?://",
        "requires_github_app": False,
    },
    "doc": {
        "steps": ["s3_upload", "graphrag"],
        "timeout": 300,
        "source_ref_pattern": r"^s3://",
        "requires_github_app": False,
    },
}


# ---------------------------------------------------------------------------
# Repo source_ref normalization (Issue #2864)
# ---------------------------------------------------------------------------
# GitHub org/repo slug: one path segment for org, one for repo. Word chars,
# dots and dashes only (matches GitHub's allowed name characters). No extra
# path segments, no scheme, no whitespace.
_GITHUB_PREFIXES = ("https://github.com/", "git@github.com:")
_SLUG_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def normalize_repo_source_ref(source_ref: str) -> str | None:
    """Normalize a repo source_ref to the canonical GitHub URL form.

    Accepts a full GitHub URL (``https://github.com/<org>/<repo>`` or
    ``git@github.com:<org>/<repo>``, with optional trailing ``/`` or ``.git``)
    OR a bare ``<org>/<repo>`` slug, and returns the canonical
    ``https://github.com/<org>/<repo>``.

    Repeatedly strips known GitHub prefixes so an accidentally double-prefixed
    value (e.g. ``https://github.com/https://github.com/octocat/Hello-World``,
    the corruption in Issue #2864) collapses back to a clean slug.

    Returns ``None`` if the input does not reduce to a valid ``org/repo`` slug,
    so the caller can reject it (HTTP 422).
    """
    candidate = source_ref.strip()

    # Collapse any stack of GitHub prefixes (handles the double-prefix corruption).
    stripped = True
    while stripped:
        stripped = False
        for prefix in _GITHUB_PREFIXES:
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix) :]
                stripped = True

    if candidate.endswith(".git"):
        candidate = candidate[:-4]
    candidate = candidate.rstrip("/")

    if not _SLUG_RE.match(candidate):
        return None

    return f"https://github.com/{candidate}"


def get_type_config(asset_type: str) -> dict[str, Any] | None:
    """Look up the config for an asset type. Returns None if unknown."""
    return ASSET_TYPE_REGISTRY.get(asset_type)


def is_valid_asset_type(asset_type: str) -> bool:
    """Return True if asset_type is registered in ASSET_TYPE_REGISTRY."""
    return asset_type in ASSET_TYPE_REGISTRY


def validate_source_ref(asset_type: str, source_ref: str) -> bool:
    """Validate source_ref against the type's source_ref_pattern.

    Returns True if the source_ref matches, False otherwise.
    Returns False for unknown asset types.
    """
    config = ASSET_TYPE_REGISTRY.get(asset_type)
    if not config:
        return False
    pattern = config.get("source_ref_pattern", "")
    if not pattern:
        return True  # No pattern = accept anything
    return bool(re.match(pattern, source_ref))


def get_steps(asset_type: str) -> list[str]:
    """Return the ingestion steps for an asset type.

    Raises KeyError if asset_type is not in the registry.
    """
    config = ASSET_TYPE_REGISTRY[asset_type]
    return config["steps"]


def list_asset_types() -> list[str]:
    """Return all registered asset type names."""
    return list(ASSET_TYPE_REGISTRY.keys())
