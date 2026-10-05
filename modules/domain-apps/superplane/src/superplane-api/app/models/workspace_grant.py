"""Workspace grant — the server-held record that a principal may act on a workspace.

Issue #5055 (U14), implementing R6's authorization model (issue #5044, U9).

WHY A NEW TABLE
---------------
Before this, authority over a workspace was the caller's *organization*: the JWT
carried an org id as its subject and every handler filtered on
``Workspace.org_id == org_id`` alone. That makes every org-mate equivalent, so
any holder of any of the org's API keys reached every workspace in it — including
``POST /workspaces/{id}/kubeconfig``, which returns live cluster credentials.
There was no schema anywhere capable of expressing "this principal may use this
workspace", so there was nothing to check against and no way to write the check.

This table is that missing record. The org filter stays as a *precondition* —
a grant referencing another org's workspace is refused — but it stops being the
authority.

WHY PERMISSIONS ARE A STRING COLUMN AND NOT A ROLE
--------------------------------------------------
The permission vocabulary belongs to ``superplane_auth.policy`` and is
deliberately not this service's role names. Storing a role name here would mean
re-deriving permissions from it at every read, and a role rename would silently
widen access. So the grant stores the permission set it was created with, and
``policy.expand_permissions`` closes it over the implication rules at decision
time.

Unrecognized stored values are DROPPED at read time rather than passed through
(see ``permissions()``). A value this build does not understand grants nothing:
a downgrade that removes a permission must not leave a grant asserting authority
this code cannot reason about.

REVOCATION
----------
``revoked_at`` is nullable and is checked at the moment an operation runs, not
only at admission. That is what makes R6's "re-checked at the operation" real:
authority is re-read from this table per operation, so a grant revoked between
sign-in and execution refuses the operation rather than riding the session.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Principal kinds. Mirrors the token's verified account type rather than
# inventing a third vocabulary: a grant is held by a human or by a service
# account, and the two are never interchangeable (R5 acc. 6-7 — a service
# token's ability to authenticate delegates no user's authority to it).
PRINCIPAL_HUMAN = "human"
PRINCIPAL_SERVICE = "service"
VALID_PRINCIPAL_TYPES = (PRINCIPAL_HUMAN, PRINCIPAL_SERVICE)


class WorkspaceGrantRecord(Base):
    """A principal's authority over one workspace."""

    __tablename__ = "workspace_grants"
    __table_args__ = (
        # One grant row per (workspace, principal). Two rows would make the
        # effective permission set depend on row order, which is a silent
        # widening: the union of two partial grants is not what either says.
        UniqueConstraint(
            "workspace_id", "principal", name="uq_workspace_grants_workspace_principal"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False, index=True
    )
    # Denormalized from the workspace deliberately. The decision path compares
    # this against both the workspace's stored org and the token's verified org
    # claim, so a grant written against the wrong org is caught by a mismatch
    # rather than by trusting a join to have been correct.
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )
    # The verified token subject. Opaque; never parsed for meaning.
    principal: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    principal_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PRINCIPAL_HUMAN
    )
    # Space-separated permission values from `superplane_auth.policy.Permission`.
    permissions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def permission_values(self) -> tuple[str, ...]:
        """Raw stored permission strings, whitespace-normalized."""
        return tuple(v for v in (self.permissions or "").split() if v)
