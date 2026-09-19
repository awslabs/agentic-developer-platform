"""Provider connections and their workspace bindings — the durable half of R7.

Issue #5053 (U7b), EPIC #4910. R7 acceptances 1, 2, 3 and 5, server side.

WHAT THESE TWO TABLES ARE FOR, AND WHY THEY ARE TWO
---------------------------------------------------
``superplane_contracts.connections`` decides *whether* an operation is allowed. It
is a pure library: every decision function takes the ownership record and the
binding as arguments and reads no storage. Something has to supply those two
arguments from server-held state, and that is what these tables are.

They are separate tables because they answer the two different questions the
contract insists on keeping apart:

* :class:`ProviderConnection` records *who manages the credential* — the vault's
  recorded owner, and therefore who may rotate or delegate it.
* :class:`ProviderConnectionBinding` records *where the credential may be used* —
  exactly one workspace.

Collapsing them into one row with a nullable workspace column would make the two
questions share a lifetime, and the one thing this contract exists to prevent is
either answer standing in for the other. Keeping them apart also lets the binding
carry its own uniqueness constraint, below, which is the part a code path cannot
forget.

WHY THE BINDING'S UNIQUENESS IS A DATABASE CONSTRAINT
-----------------------------------------------------
The contract's ``WorkspaceBinding`` is "exactly one credential, exactly one
workspace", and it enforces that by *being* a frozen dataclass with two scalar
fields — a binding covering "all of a principal's credentials" cannot be
expressed. That property is real in Python and worth nothing in the database: two
rows binding ``cred-1`` to two different workspaces are perfectly insertable, and
then ``authorize_use`` is called with whichever one the query happened to return
first. Row order would decide a tenant-isolation question.

So the constraint is declared on the table. A second binding for the same
credential fails at the database, in every writer, including a backfill script or
a test fixture that never goes through a route.

WHY THE REFERENCE COLUMN REUSES U13b's VALIDATOR
------------------------------------------------
``validate_adp_credential_id`` (``app/models/credential.py``, issue #5046) already
refuses ARNs, known secret shapes, interior-whitespace and escape-form evasions,
and invisible-character prefixes — a set of rules hardened over several rounds
against specific measured bypasses. This column holds the same kind of value, so it
calls that function rather than growing a second, younger, weaker copy. A mirror
here would be the third copy of those rules in this repository and the first one
nobody had attacked.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.database import Base
from app.models.credential import validate_adp_credential_id

# Lifecycle values, mirroring `superplane_contracts.connections.ConnectionStatus`.
#
# Stored as strings rather than a database enum for the same reason the grant table
# stores permissions as text: adding a status must not require a migration that
# rewrites a type while the old code is still serving. The contract's enum remains
# the vocabulary, and `tests/test_models.py` asserts these values match it, so a
# rename upstream fails a test instead of silently detaching the two.
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

CONNECTION_STATUSES: tuple[str, ...] = (
    STATUS_PENDING,
    STATUS_ACTIVE,
    STATUS_DISABLED,
)


class ProviderConnection(Base):
    """One provider connection: a credential reference plus its vault ownership.

    Holds no secret value and no secret ARN. ``adp_credential_id`` is the vault's
    opaque handle, and the validator on it refuses anything that looks like
    credential material or an address for it.

    ``owner_principal`` is the vault's recorded owner, written from the vault's own
    response and never from a request body. That is what makes the delegation check
    meaningful: a claim supplied by the caller would be the very thing under test,
    asserted by the party being tested.
    """

    __tablename__ = "provider_connections"
    __table_args__ = (
        # One connection row per (org, credential). The same credential reference
        # registered twice in one organization would give two connection ids for
        # one credential, so a rotation through one would leave the other pointing
        # at a superseded reference while still reporting itself active.
        UniqueConstraint(
            "org_id",
            "adp_credential_id",
            name="uq_provider_connections_org_credential",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)

    # The vault's opaque handle. Never an ARN, never a value — see the validator.
    adp_credential_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # The vault's non-secret metadata, carried so a connection listing needs no
    # second vault lookup. Neither field is sufficient to read the credential.
    credential_service: Mapped[str] = mapped_column(String(100), nullable=False)
    credential_label: Mapped[str] = mapped_column(String(255), nullable=False)

    # The vault-recorded owner of the credential. Opaque token subject; never
    # parsed for meaning, and never taken from a request body.
    owner_principal: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=STATUS_PENDING
    )

    # The last validation readings, stored as four separate columns.
    #
    # Four columns rather than one JSON blob or one aggregate flag, because R7
    # acceptance 3 is precisely that these readings stay separate: a valid key says
    # nothing about whether the provider has a free GPU. `observed_capacity` is
    # nullable BECAUSE "not measured" is a different operational fact from
    # "measured as zero", and a non-nullable column defaulting to 0 would erase
    # exactly that distinction at the storage layer.
    credential_valid: Mapped[bool | None] = mapped_column(nullable=True)
    permissions_sufficient: Mapped[bool | None] = mapped_column(nullable=True)
    quota_available: Mapped[bool | None] = mapped_column(nullable=True)
    observed_capacity: Mapped[int | None] = mapped_column(nullable=True)
    validation_detail: Mapped[str] = mapped_column(
        String(1024), nullable=False, default=""
    )
    validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # What disablement did NOT accomplish, surfaced from storage so the disable
    # response carries it rather than reconstructing it from a constant at read
    # time. Acceptance 5 is about an operator SEEING this.
    limitation: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @validates("adp_credential_id")
    def _check_adp_credential_id(self, _key: str, value: str) -> str:
        """Refuse secret material at assignment, before any flush can persist it.

        On the model rather than only in the request schema so the rule holds for
        every writer — a router, a reconciler, a backfill or a fixture — not only
        for traffic that arrives through a validated request body.
        """
        return validate_adp_credential_id(value)

    @validates("status")
    def _check_status(self, _key: str, value: str) -> str:
        """Refuse a status this build cannot reason about.

        An unrecognized status would be neither active nor disabled to every
        downstream check, which in practice reads as "not disabled" — the
        permissive direction — so it is refused at assignment instead.
        """
        if value not in CONNECTION_STATUSES:
            raise ValueError(
                f"status must be one of {', '.join(CONNECTION_STATUSES)}; got {value!r}"
            )
        return value


class ProviderConnectionBinding(Base):
    """The record that one credential may be used by exactly one workspace.

    Separate from :class:`ProviderConnection` because delegation authority and
    usability are different questions (see the module docstring), and because the
    uniqueness rule below belongs to this row.
    """

    __tablename__ = "provider_connection_bindings"
    __table_args__ = (
        # THE constraint. One binding per connection: the credential is usable in
        # exactly one workspace, enforced here so no code path can produce a second
        # row and leave `authorize_use` picking whichever the query returned first.
        UniqueConstraint(
            "connection_id",
            name="uq_provider_connection_bindings_connection",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("provider_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized from the connection, for the same reason `workspace_grants`
    # denormalizes its org: the decision path compares this against the connection's
    # stored reference, so a binding written against the wrong credential is caught
    # by a mismatch rather than by trusting a join to have been right.
    adp_credential_id: Mapped[str] = mapped_column(String(255), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False, index=True
    )
    # The principal who created the binding. Audit evidence for a delegation.
    bound_by: Mapped[str] = mapped_column(String(255), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @validates("adp_credential_id")
    def _check_adp_credential_id(self, _key: str, value: str) -> str:
        """Same refusal as the connection's column; this one is denormalized."""
        return validate_adp_credential_id(value)
