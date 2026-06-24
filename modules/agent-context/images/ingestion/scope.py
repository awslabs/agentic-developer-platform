"""Ingestion scope envelope — shared model for SQS message scope field.

Defines the scope that travels with every ingestion message from producer
to consumer. The scope carries tenant/user/project isolation metadata used
by downstream storage writers (S3 prefix routing, S3 Vectors index selection,
Neptune properties, Postgres columns).

Design reference: docs/agent-context/design-1721-tenant-isolation.md §9.1, §9.3.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


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

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for JSON encoding."""
        return asdict(self)


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

    return IngestionScope(
        tenant_id=raw.get("tenant_id"),
        owner_sub=raw.get("owner_sub"),
        project_id=raw.get("project_id"),
        visibility=visibility,
    )
