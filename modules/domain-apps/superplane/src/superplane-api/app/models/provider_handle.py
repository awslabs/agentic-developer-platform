"""Durable provider-operation records — issue #5054 (U11c), EPIC #4910.

This is the persistence half of the handle/reconciliation contract U11 (#5049)
defined. That contract decides what an ambiguous provider outcome *means*; it
deliberately holds no storage, so its central rule — a handle exists before the
operation can be lost — is unenforceable without this table.

## Why a row has to exist before the provider call

`superplane_contracts.handles.authorize_provider_call` refuses a call whose
`HandleRecord` is not durable, and `HandleRecord` will not accept `durable=True`
without a `confirmed_at` instant. That instant is what this module supplies: it is
set from the committed write, so the only way a caller obtains one is for a row to
have actually reached storage. An in-process flag would satisfy the contract's
type and none of its requirement.

The crash this survives can happen *during* the call, which is why ordering is
the whole point rather than a nicety. A record written after the response leaves
exactly the window the bug lives in: a resource created by a call whose answer
never came back, and no record that the call was ever made.

## Why the primary key is the idempotency identity rather than a surrogate id

The question this table is asked after a crash is "did I already start this
operation?", and the asker has only what it could compute *before* the call — the
idempotency key. A surrogate UUID would be generated per insert, so a retry after
a lost response would mint a second id and insert a second row for one operation,
which is the duplicate this exists to prevent. Keying on the idempotency identity
makes a duplicate insert a primary-key conflict the receiver can recognise as
"already recorded" rather than silently accept as new work.

## Why that identity is (workspace, idempotency_key) and not the key alone

The key alone was the first form of this table, and it was wrong in a way that
only shows up with more than one tenant: uniqueness was global while authority is
per workspace, so the two domains did not cover the same ground. Because the
primary-key conflict *is* the duplicate detection, a key already held by another
tenant read as "you already recorded this" to a caller that had recorded nothing.

That is not a contrived collision, because these keys are derived, low-entropy
strings rather than UUIDs. `adapters/aws.go:103`, `lambda.go:86` and
`nebius.go:95` all build `sp-<cloud>-<gpu>-<count>`, and `migration/eks_join.py`
passes a cluster name verbatim. None of those inputs contains a tenant, so two
workspaces each provisioning one A100 on AWS both compute `sp-aws-a100-1`. The
second one to arrive would be refused a row, and because the durability rule is
honest — no row means no `confirmed_at`, and no `confirmed_at` means
`authorize_provider_call` refuses — its provisioning would fail on a reason it
had no way to act on, for work another tenant was doing. The 201-vs-409 answer
also reported whether a key was in use in a workspace the caller cannot read,
which is the cross-tenant inference the rest of this module is built to avoid.

Scoping the identity to the workspace keeps every property the key-alone form
had: a repeat within a workspace still conflicts, and a recovery caller can still
address its operation with only what it held before the call, because it always
knows its own workspace. The workspace comes from the caller's authenticated
grant, so it cannot be used to reach across the boundary it establishes.

``provider_reference`` is nullable for the same ordering reason it is `None` on
the contract's `ProviderHandle`: SkyPilot's `request_id` or EC2's instance id does
not exist yet at the moment the row must be written. It is filled in on
conclusion, and a row that never gets one is exactly the case a recovery read
needs to find.

## Why the concluded state is stored rather than derived

``state`` records whether the operation is still in flight, and the *terminal*
states carry the reconciliation result that closed them. It is not derived from
"has a provider_reference" because the unresolved case has no reference and is
also not in flight — it is an operation whose provider could not be consulted,
which must stay on the books. Collapsing that into either "open" or "closed" is
how "we could not check" becomes "there is nothing there", and retaining it is
R15 acceptance 7's requirement that unresolved exposure is not cleared.

## Workspace binding

``workspace`` is part of the primary key and is validated against the caller's
authenticated grant before any row is written or read: a request naming a
workspace the credential does not cover is refused with nothing persisted. So a
body-supplied workspace grants nothing, and the scoped read path can filter on
this column knowing every value in it passed that check — a recovery caller sees
only operations for workspaces its credential covers. See
``app/services/provider_handles.py`` for the check itself.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ProviderAllocation(Base):
    """Serialization anchor for this allocation's durable evidence, not B lifecycle."""

    __tablename__ = "provider_allocations"
    workspace: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("workspaces.id"), primary_key=True
    )
    allocation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id"), nullable=False
    )


class ProviderOperation(Base):
    """One provider operation, recorded before it can be lost.

    One row per idempotency identity *within a workspace*. The row is written in
    the "recorded" state before the provider call and updated in place on
    conclusion, so the pre-call form is always the form a post-crash
    reconciliation finds.
    """

    __tablename__ = "provider_operations"

    # The identity chosen before the call, scoped to the owning workspace. Both
    # columns are the primary key so a duplicate record attempt within a workspace
    # conflicts, while the same derived key in another workspace is a different
    # operation — see the module docstring on why the key alone was wrong.
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)

    # `OperationKind` from the contract, stored as its string value. The contract's
    # enums are `str`-valued precisely so the stored form is stable when a member
    # is inserted; storing an ordinal would silently re-map existing rows.
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)

    # What the caller asked the provider to name the thing. Chosen locally, so
    # available before the call, and what a later re-check queries by.
    resource_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # The authoritative allocation this operation belongs to. Accounting reads
    # allocation membership from the allocation record, never from observed keys.
    allocation_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # The owning workspace, validated against the caller's grant before the row is
    # written or read. Part of the primary key so the uniqueness domain matches the
    # authorization domain; also indexed for the workspace-scoped recovery read.
    workspace: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("workspaces.id"),
        primary_key=True,
        nullable=False,
        index=True,
    )
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id"), nullable=False
    )

    # B's verified binding, fixed at creation. Opaque authority tokens are never stored.
    authority_operation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    authority_run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    authority_attempt_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # The provider's own identifier, absent until the provider answers. A row that
    # never receives one is what a recovery read is looking for.
    provider_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Lifecycle state: see `app/services/provider_handles.py` for the values and
    # why unresolved is neither open nor closed.
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # The `ReconcileResult` that concluded this operation, absent while in flight.
    # Retained so a conclusion can be re-reported idempotently: the second report
    # compares against this rather than re-deciding.
    reconcile_result: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # What the provider said, and which identifier the query used. A query by the
    # wrong identity can be answered confidently and still be about the wrong
    # resource, so both are retained as the evidence for the conclusion.
    provider_presence: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    observation_queried_by: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    # Why an operation could not be resolved, for the operator who has to go and
    # look. An unresolved operation that names no reason is unactionable.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The instant the pre-call record committed. This is the value returned as the
    # contract's `confirmed_at`, which is why it is set from the write rather than
    # supplied by the caller.
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    concluded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    conflicts: Mapped[list["ProviderReferenceConflict"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan"
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace", "allocation_id"],
            ["provider_allocations.workspace", "provider_allocations.allocation_id"],
        ),
        # The recovery read is "unconcluded operations for these workspaces",
        # which filters on both columns together.
        Index("ix_provider_operations_workspace_state", "workspace", "state"),
    )


class ProviderReferenceConflict(Base):
    """Every conflicting provider ID survives even if the reporter loses the409."""

    __tablename__ = "provider_reference_conflicts"
    workspace: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    provider_reference: Mapped[str] = mapped_column(String(255), primary_key=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_presence: Mapped[str | None] = mapped_column(String(32))
    provider_state: Mapped[str | None] = mapped_column(Text)
    observation_queried_by: Mapped[str | None] = mapped_column(String(255))
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace", "idempotency_key"],
            ["provider_operations.workspace", "provider_operations.idempotency_key"],
        ),
    )


class ProviderAllocationResource(Base):
    """Monotonic resource membership from the trusted allocation inventory source."""

    __tablename__ = "provider_allocation_resources"
    workspace: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("workspaces.id"), primary_key=True
    )
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id"), nullable=False
    )
    allocation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    inventory_revision: Mapped[str] = mapped_column(String(255), nullable=False)
