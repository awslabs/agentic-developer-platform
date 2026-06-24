"""Ingestion scope envelope — shared model for SQS message scope field.

Defines the scope that travels with every ingestion message from producer
to consumer. The scope carries tenant/user/project isolation metadata used
by downstream storage writers (S3 prefix routing, S3 Vectors index selection,
Neptune properties, Postgres columns).

Design reference: docs/agent-context/design-1721-tenant-isolation.md §9.1, §9.3.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

log = logging.getLogger(__name__)

# Valid visibility values per design §9.1
VALID_VISIBILITIES = ("shared", "tenant", "personal")


@dataclass(frozen=True)
class IngestionScope:
    """Scope envelope for an ingestion SQS message.

    Attributes:
        tenant_id: Organization-level isolation key (None = shared corpus).
        owner_sub: User-level isolation key (Cognito sub; None = not personal).
        project_id: Project-level grouping (None = unscoped).
        visibility: One of "shared", "tenant", "personal".
    """

    tenant_id: str | None = None
    owner_sub: str | None = None
    project_id: str | None = None
    visibility: str = "shared"

    @property
    def is_shared(self) -> bool:
        """True when scope is shared (default corpus)."""
        return self.visibility == "shared"

    @property
    def is_tenant(self) -> bool:
        """True when scope is tenant-level isolation."""
        return self.visibility == "tenant"

    @property
    def is_personal(self) -> bool:
        """True when scope is personal (user-level isolation)."""
        return self.visibility == "personal"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for JSON encoding."""
        return asdict(self)

    def to_env(self) -> dict[str, str]:
        """Export scope as INGESTION_SCOPE_* environment variables for subprocesses."""
        return {
            "INGESTION_SCOPE_VISIBILITY": self.visibility,
            "INGESTION_SCOPE_TENANT_ID": self.tenant_id or "",
            "INGESTION_SCOPE_OWNER_SUB": self.owner_sub or "",
            "INGESTION_SCOPE_PROJECT_ID": self.project_id or "",
        }


# Default scope for backward compatibility (§9.3):
# Messages without a scope field default to shared visibility.
DEFAULT_SCOPE = IngestionScope(
    tenant_id=None,
    owner_sub=None,
    project_id=None,
    visibility="shared",
)


def parse_scope(raw: dict[str, Any] | None) -> IngestionScope:
    """Parse a scope dict from an SQS message body into an IngestionScope.

    Returns DEFAULT_SCOPE when raw is None or empty (backward compatibility).
    Normalizes unknown visibility values to "shared" for safety.
    """
    if not raw:
        return DEFAULT_SCOPE

    visibility = raw.get("visibility", "shared")
    if visibility not in VALID_VISIBILITIES:
        visibility = "shared"

    tenant_id = raw.get("tenant_id") or None
    owner_sub = raw.get("owner_sub") or None

    # Validate: tenant visibility requires tenant_id
    if visibility == "tenant" and not tenant_id:
        log.warning("scope.visibility=tenant but tenant_id is missing — defaulting to shared")
        return DEFAULT_SCOPE

    # Validate: personal visibility requires owner_sub
    if visibility == "personal" and not owner_sub:
        log.warning("scope.visibility=personal but owner_sub is missing — defaulting to shared")
        return DEFAULT_SCOPE

    return IngestionScope(
        tenant_id=tenant_id,
        owner_sub=owner_sub,
        project_id=raw.get("project_id") or None,
        visibility=visibility,
    )


def parse_scope_from_env() -> IngestionScope:
    """Read scope from INGESTION_SCOPE_* environment variables.

    Used by child processes (ingest-repo.py, etc.) to receive scope
    propagated by sqs-worker.py via environment.
    Returns DEFAULT_SCOPE when env vars are absent (backward-compatible).
    """
    import os

    visibility = os.environ.get("INGESTION_SCOPE_VISIBILITY", "shared")
    if visibility not in VALID_VISIBILITIES:
        visibility = "shared"

    tenant_id = os.environ.get("INGESTION_SCOPE_TENANT_ID") or None
    owner_sub = os.environ.get("INGESTION_SCOPE_OWNER_SUB") or None

    # Validate: tenant visibility requires tenant_id
    if visibility == "tenant" and not tenant_id:
        log.warning("INGESTION_SCOPE_VISIBILITY=tenant but no TENANT_ID — defaulting to shared")
        return DEFAULT_SCOPE

    # Validate: personal visibility requires owner_sub
    if visibility == "personal" and not owner_sub:
        log.warning("INGESTION_SCOPE_VISIBILITY=personal but no OWNER_SUB — defaulting to shared")
        return DEFAULT_SCOPE

    return IngestionScope(
        tenant_id=tenant_id,
        owner_sub=owner_sub,
        project_id=os.environ.get("INGESTION_SCOPE_PROJECT_ID") or None,
        visibility=visibility,
    )


def compute_s3_prefix(scope: IngestionScope, base_prefix: str) -> str:
    """Compute a scoped S3 prefix from scope and a base prefix.

    Routing per design §8.2:
      - shared:   base_prefix unchanged (e.g. "content/wikis")
      - tenant:   "tenants/{tenant_id}/{leaf}" (e.g. "tenants/acme/wikis")
      - personal: "users/{owner_sub}/{leaf}" (e.g. "users/user-abc/wikis")

    The leaf is the last path component of base_prefix (after stripping
    any leading "content/" path segment that is an S3ContentStore artifact).
    """
    # Normalize: strip trailing slash
    base_prefix = base_prefix.rstrip("/")

    if scope.is_shared:
        return base_prefix

    # Extract leaf: the meaningful suffix after the top-level directory.
    # Common base_prefixes: "content/wikis", "content/code-indexes", "sbom",
    # "zoekt-shards". For "content/..." we strip the "content/" prefix to get
    # the artifact type; for others we use the whole path as leaf.
    if base_prefix.startswith("content/") and "/" in base_prefix:
        leaf = base_prefix[len("content/") :]
    else:
        leaf = base_prefix

    if scope.is_tenant:
        return f"tenants/{scope.tenant_id}/{leaf}"
    elif scope.is_personal:
        return f"users/{scope.owner_sub}/{leaf}"

    # Fallback (shouldn't reach here due to validation in parse_scope)
    return base_prefix
