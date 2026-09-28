"""Ingestion scope envelope — shared model for SQS message scope field.

Defines the scope that travels with every ingestion message from producer
to consumer. The scope carries tenant/user/project isolation metadata used
by downstream storage writers (S3 prefix routing, S3 Vectors index selection,
Neptune properties, Postgres columns).

Design reference: docs/agent-context/design-1721-tenant-isolation.md §9.1, §9.3.
"""

from __future__ import annotations

import logging
import re
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
        owner_sub: User-level isolation key (canonical users.id UUID; None = not personal).
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


# Explicit shared scope for trusted publishers. Missing consumer scope is refused.
DEFAULT_SCOPE = IngestionScope(
    tenant_id=None,
    owner_sub=None,
    project_id=None,
    visibility="shared",
)


class ScopeValidationError(ValueError):
    """Ownership is absent, contradictory, or cannot form a safe storage prefix."""


def parse_scope(raw: dict[str, Any] | None) -> IngestionScope:
    """Validate an explicit producer scope; missing ownership never means shared."""
    if not isinstance(raw, dict) or not raw or "visibility" not in raw:
        raise ScopeValidationError("scope.visibility must be explicitly provided")
    visibility = raw["visibility"]
    if visibility not in VALID_VISIBILITIES:
        raise ScopeValidationError(f"scope.visibility={visibility!r} is not one of {VALID_VISIBILITIES}")
    identifiers = {}
    for field in ("tenant_id", "owner_sub", "project_id"):
        value = raw.get(field)
        if value in (None, ""):
            identifiers[field] = None
        elif not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@:-]*", value):
            raise ScopeValidationError(f"scope.{field} must be a safe, nonempty identifier")
        else:
            identifiers[field] = value
    if visibility == "tenant" and not identifiers["tenant_id"]:
        raise ScopeValidationError("scope.visibility=tenant but tenant_id is missing")
    if visibility == "personal" and not identifiers["owner_sub"]:
        raise ScopeValidationError("scope.visibility=personal but owner_sub is missing")
    if visibility == "shared" and (identifiers["tenant_id"] or identifiers["owner_sub"]):
        raise ScopeValidationError("shared scope cannot carry tenant or owner restrictions")
    return IngestionScope(visibility=visibility, **identifiers)


def parse_scope_from_env() -> IngestionScope:
    """Validate the same explicit scope passed by the queue worker to children."""
    import os

    return parse_scope(
        {
            "visibility": os.environ.get("INGESTION_SCOPE_VISIBILITY"),
            "tenant_id": os.environ.get("INGESTION_SCOPE_TENANT_ID"),
            "owner_sub": os.environ.get("INGESTION_SCOPE_OWNER_SUB"),
            "project_id": os.environ.get("INGESTION_SCOPE_PROJECT_ID"),
        }
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
    # Also validate direct dataclass callers before interpolating identifiers.
    scope = parse_scope(scope.to_dict())
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
