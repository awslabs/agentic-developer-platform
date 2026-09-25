"""Cluster membership — one workspace's binding to one cluster (issue #6048).

Superplane's design (`../../DESIGN.md` §3.2) requires cluster ownership and
workspace membership to be separate facts: a cluster belongs to one organization,
but a workspace's own binding to that cluster — its namespace, its bootstrap
registration, its credential scope, its lifecycle state — must be recorded
per-workspace so two workspaces can share a cluster without sharing any of those.

`workspaces.cluster_id` continues to answer "what cluster does this workspace's
UI/CLI show" (a compatibility projection, per DESIGN.md §3.2), but this table is
the entity that answers "is this workspace *currently and exclusively* bound to
this cluster, in this namespace, at this generation" — the fact the bootstrap
reservation and cluster observation authority need and that a single nullable
column cannot express once a cluster has more than one member.

## Why membership state is separate from workspace status

A workspace can be deleted (draining, gone) while its cluster keeps other active
members. `state` here therefore has its own lifecycle (`reserved` -> `active` ->
`removed`) rather than borrowing `Workspace.status`, so removing one member's
membership row never has to reach into another workspace's status field.

## Why (cluster_id, namespace) is unique only among non-removed rows

A removed membership's namespace name may legitimately be reused by nothing —
namespace identity is by UID, never by name (see `workspace_namespace.py`) — but
two *live* memberships must never collide on namespace, since that is the
isolation boundary between tenants sharing one cluster. The partial unique index
in the migration enforces this without blocking history from being retained.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Membership lifecycle states.
#   reserved -> a pre-mutation claim taken before bootstrap touches the cluster
#               (mirrors `superplane_bootstrap.registry`'s reserve/finalize split).
#   active   -> bootstrap finalized; the workspace is a live member.
#   removed  -> the workspace's membership ended (workspace deleted or migrated);
#               the row is retained as a tombstone, never deleted outright.
STATE_RESERVED = "reserved"
STATE_ACTIVE = "active"
STATE_REMOVED = "removed"

VALID_MEMBERSHIP_STATES = (STATE_RESERVED, STATE_ACTIVE, STATE_REMOVED)


class ClusterMembership(Base):
    """One workspace's binding to one cluster, with its own namespace and state."""

    __tablename__ = "cluster_memberships"
    __table_args__ = (
        # One non-removed membership per workspace. A workspace migrating clusters
        # first has its old membership marked `removed`, so this never blocks that
        # explicit, separately authorized operation — it only blocks a workspace
        # silently gaining a second *concurrent* target.
        UniqueConstraint(
            "workspace_id",
            "state",
            name="uq_cluster_memberships_workspace_live",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id"), nullable=False
    )
    # Opaque generation fingerprint, matching `workspace_bootstrap_authority.generation`
    # (64 hex chars). Bound into preview/approval/execution so a stale membership
    # generation can be detected and refused rather than silently reused.
    generation: Mapped[str] = mapped_column(String(64), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    namespace_uid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default=STATE_RESERVED)
    # The operation that created this membership, for replay/idempotency —
    # mirrors `Workspace.operation_id`'s role for workspace creation.
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # Opaque credential reference, never a credential value (see registration.py's
    # `WorkspaceTarget.credential_reference_id` for the same discipline).
    credential_reference_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    removal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
