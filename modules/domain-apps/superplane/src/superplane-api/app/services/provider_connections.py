"""Provider-connection service — the routes' single path to U7's decisions.

Issue #5053 (U7b), EPIC #4910. R7 server half, acceptances 1, 2, 3 and 5.

## What this module is for

U7 (#5294) shipped ``superplane_contracts.connections``: the rules for who may
delegate a provider credential, where it may be used, how its health is reported,
and how rotation and disablement behave. Those rules decide nothing on their own
because nothing calls them. This module is what calls them.

It deliberately contains **no copy of those rules**. Every authorization answer and
every lifecycle transition here is produced by asking the contract
(``authorize_delegation``, ``activate``, ``rotate``, ``disable``).
This module's job is the part the contract cannot do: load the server-held evidence
out of the database, convert rows into the contract's frozen types, and persist what
the contract returns. A second implementation of the decisions is how the routes and
the contract drift, and drift in this specific area is a cross-workspace credential
exposure rather than a cosmetic inconsistency.

Ownership comes from the trusted ADP vault evidence port, never from the caller
registering a reference or from organization membership. Every credential mutation
requires a verified principal, an explicit workspace grant and fresh vault ownership.
Caller-supplied validation readings require an independently verified attestation
bound to the exact credential, organization and workspace. Missing adapters fail
closed; live vault/provider acceptance remains separate.

## Timestamps

The contract refuses naive timestamps, because a bare local time from an unknown host
is not a fact two implementations can compare. SQLite returns naive datetimes for
``DateTime(timezone=True)`` columns, so every value crossing into a contract type goes
through ``_as_utc``. Without it this service would raise ``ContractViolation`` on the
read path under the test database while appearing correct against PostgreSQL.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from superplane_contracts.connections import (
    ConnectionState,
    ConnectionStatus,
    CredentialReference,
    Decision,
    RotationResult,
    ValidationReport,
    WorkspaceBinding,
    activate,
    disable,
    rotate,
)

from app.models.credential import CredentialRegistry
from app.models.provider_connection import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_PENDING,
    ProviderConnection,
    ProviderConnectionBinding,
)
from app.models.workspace import Workspace

logger = logging.getLogger(__name__)


# Storage strings <-> the contract's enum. Two explicit maps rather than
# `ConnectionStatus(value)` so an unrecognized stored value raises here, at the
# boundary, instead of becoming a status no downstream check recognizes — which in
# practice reads as "not disabled", the permissive direction.
_STATUS_TO_CONTRACT: dict[str, ConnectionStatus] = {
    STATUS_PENDING: ConnectionStatus.PENDING,
    STATUS_ACTIVE: ConnectionStatus.ACTIVE,
    STATUS_DISABLED: ConnectionStatus.DISABLED,
}

_STATUS_FROM_CONTRACT: dict[ConnectionStatus, str] = {
    contract: stored for stored, contract in _STATUS_TO_CONTRACT.items()
}


class ConnectionNotFound(Exception):
    """No connection with this id in this organization.

    Raised instead of returning ``None`` so a caller cannot forget to check. The
    router turns it into a 404 carrying no tenant detail: distinguishing "exists but
    is another org's" from "does not exist" would make the endpoint an existence
    oracle for other tenants' connections.
    """


class CredentialNotRegistered(Exception):
    """The reference is not in this organization's credential registry."""


class CredentialProviderMismatch(Exception):
    """The reference and registry do not name the connection provider."""


# The single binding-denial string, matching the contract's own choice to keep one
# constant per denial class rather than a message per branch — a branch that became
# more informative than its siblings turns the check into an enumeration oracle.
_NOT_BOUND_REASON = "credential is not bound to this workspace"


def _as_utc(value: datetime) -> datetime:
    """Return `value` as a timezone-aware UTC datetime.

    A naive value is *assumed* UTC rather than rejected, because that is what the
    storage layer writes: these columns are `DateTime(timezone=True)` populated by
    `func.now()`, and SQLite simply drops the offset on the way back out. Assuming
    UTC for a value this service itself wrote in UTC is accurate; the assumption is
    recorded here rather than hidden so it is not mistaken for parsing an arbitrary
    client-supplied local time.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def granted_permissions(request_state_grant: object | None) -> frozenset[str]:
    """Only an explicit server-held grant can confer credential permissions."""
    if request_state_grant is None:
        return frozenset()
    permissions = getattr(request_state_grant, "permissions", frozenset())
    # `Permission` is a `StrEnum`, so `str()` yields the wire value the contract
    # compares against. Converted explicitly rather than passed through, because the
    # contract refuses a set containing non-`str` members.
    return frozenset(str(permission) for permission in permissions)


def to_reference(connection: ProviderConnection) -> CredentialReference:
    """The stored reference as the contract's type.

    ``CredentialReference.__post_init__`` refuses an ARN, so a row that somehow held
    one fails here rather than being published.
    """
    return CredentialReference(
        credential_id=connection.adp_credential_id,
        service=connection.credential_service,
        label=connection.credential_label,
    )


def to_binding(binding: ProviderConnectionBinding) -> WorkspaceBinding:
    """The stored binding as the contract's type."""
    return WorkspaceBinding(
        credential_id=binding.adp_credential_id,
        workspace_id=str(binding.workspace_id),
        bound_by=binding.bound_by,
        bound_at=_as_utc(binding.bound_at),
    )


def to_validation(connection: ProviderConnection) -> ValidationReport | None:
    """The four stored readings as the contract's report, or ``None`` if unvalidated.

    ``observed_capacity`` is passed through untouched, including ``0``: the contract
    draws "not measured" (``None``) as a different fact from "measured as zero", and
    coercing either into the other here would erase the distinction at the exact
    boundary the column was made nullable to preserve.
    """
    if connection.validated_at is None:
        return None
    return ValidationReport(
        credential_valid=bool(connection.credential_valid),
        permissions_sufficient=bool(connection.permissions_sufficient),
        quota_available=bool(connection.quota_available),
        observed_capacity=connection.observed_capacity,
        checked_at=_as_utc(connection.validated_at),
        detail=connection.validation_detail,
    )


def to_state(
    connection: ProviderConnection, binding: ProviderConnectionBinding
) -> ConnectionState:
    """Assemble the contract's ``ConnectionState`` from the two rows.

    The binding is required, not optional. ``ConnectionState`` has no representation
    for an unbound connection — a connection whose scope is unknown is exactly what
    must not reach a decision function — so a caller without a binding row gets an
    error from the loader rather than a half-built state here.
    """
    return ConnectionState(
        connection_id=str(connection.id),
        provider=connection.provider,
        reference=to_reference(connection),
        binding=to_binding(binding),
        status=_STATUS_TO_CONTRACT[connection.status],
        validation=to_validation(connection),
        limitation=connection.limitation,
    )


def _apply_validation(
    connection: ProviderConnection, report: ValidationReport | None
) -> None:
    """Write a report's four readings onto the row, or clear them."""
    if report is None:
        connection.credential_valid = None
        connection.permissions_sufficient = None
        connection.quota_available = None
        connection.observed_capacity = None
        connection.validation_detail = ""
        connection.validated_at = None
        return
    connection.credential_valid = report.credential_valid
    connection.permissions_sufficient = report.permissions_sufficient
    connection.quota_available = report.quota_available
    connection.observed_capacity = report.observed_capacity
    connection.validation_detail = report.detail
    connection.validated_at = report.checked_at


def _persist_state(connection: ProviderConnection, state: ConnectionState) -> None:
    """Write a contract-produced state back onto the connection row.

    Only the fields the contract's transitions can change. The reference and the
    binding's credential id are updated because rotation changes them; the binding's
    workspace is not, because no transition in the contract moves a connection
    between workspaces and silently honouring one here would make a workspace change
    reachable through an endpoint that does not advertise it.
    """
    connection.status = _STATUS_FROM_CONTRACT[state.status]
    connection.adp_credential_id = state.reference.credential_id
    connection.credential_service = state.reference.service
    connection.credential_label = state.reference.label
    connection.limitation = state.limitation
    _apply_validation(connection, state.validation)


# ---------------------------------------------------------------------------
# Loading server-held evidence
# ---------------------------------------------------------------------------


async def load(
    db: AsyncSession, *, org_id: uuid.UUID, connection_id: uuid.UUID, lock: bool = True
) -> tuple[ProviderConnection, ProviderConnectionBinding]:
    """Load a connection and its binding, scoped to the organization.

    The ``org_id`` filter is not decoration: without it a connection id from another
    tenant would resolve, and the binding check downstream would then be comparing
    another organization's records.

    A connection with no binding row raises ``ConnectionNotFound`` rather than
    returning a partial result. Such a row cannot be authorized for use by anything
    — ``authorize_use`` denies a ``None`` binding — so surfacing it as "not found" is
    both accurate to the caller and refuses to build a state whose scope is unknown.
    """
    result = await db.execute(
        select(ProviderConnection)
        .execution_options(populate_existing=True)
        .with_for_update(read=not lock)
        .where(
            ProviderConnection.id == connection_id,
            ProviderConnection.org_id == org_id,
        )
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise ConnectionNotFound(str(connection_id))

    bound = await db.execute(
        select(ProviderConnectionBinding)
        .execution_options(populate_existing=True)
        .where(ProviderConnectionBinding.connection_id == connection.id)
    )
    binding = bound.scalar_one_or_none()
    if binding is None:
        logger.error(
            "provider connection %s has no binding row; refusing to authorize it",
            connection.id,
        )
        raise ConnectionNotFound(str(connection_id))
    return connection, binding


async def assert_credential_registered(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    credential_id: str,
    provider: str,
    credential_service: str,
    lock: bool = False,
) -> None:
    """Require the reference to exist in this organization's credential registry.

    This is only a reference-existence and org-integrity check. The domain registry
    has no vault ownership information and grants no authority; the router must
    also resolve current ownership through the trusted vault evidence port.
    """
    query = (
        select(CredentialRegistry)
        .where(
            CredentialRegistry.org_id == org_id,
            CredentialRegistry.adp_credential_id == credential_id,
            CredentialRegistry.status == "Active",
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.order_by(CredentialRegistry.id).with_for_update()
    rows = (await db.execute(query)).scalars().all()
    if not rows:
        raise CredentialNotRegistered(credential_id)
    if credential_service != provider or any(row.provider != provider for row in rows):
        raise CredentialProviderMismatch()


async def workspace_in_org(
    db: AsyncSession, *, org_id: uuid.UUID, workspace_id: uuid.UUID
) -> bool:
    """True when the workspace belongs to this organization."""
    result = await db.execute(
        select(Workspace.id).where(
            Workspace.id == workspace_id, Workspace.org_id == org_id
        )
    )
    return result.first() is not None


# ---------------------------------------------------------------------------
# The two checks — both delegated to the contract
# ---------------------------------------------------------------------------


def check_binding(
    *, workspace_id: str, state: ConnectionState, binding: ProviderConnectionBinding
) -> Decision:
    """The exact-workspace-binding half of acceptance 2, for a management operation.

    Deliberately NOT ``authorize_use``, and the difference is a bug I shipped in the
    first version of these routes. ``authorize_use`` is the admission gate: it bundles
    the binding comparison with ``admits_new_work()``, so it denies a PENDING
    connection. The contract separates those two answers on purpose —
    ``allows_renewal()`` exists beside ``admits_new_work()`` because "a PENDING
    connection legitimately needs its credential renewable while it is being brought
    up" — and using the admission gate to guard rotation strands precisely the
    connection rotation exists to rescue: one registered against a credential that
    never validates cannot be activated, so if it also cannot be rotated its only
    remaining transition is disablement.

    So the lifecycle term is asked of the contract separately, per route, with the
    method that fits the operation, and this function is the binding term alone.

    The two other terms ``authorize_use`` checks — that the binding names the
    connection's own credential, and that it agrees with the state's binding — are not
    dropped: ``ConnectionState.__post_init__`` refuses a mismatched reference/binding
    pair at construction, and ``to_state`` builds the state from this same row, so any
    state reaching here has already been refused if they disagreed.
    """
    bound = to_binding(binding)
    if not workspace_id or bound.workspace_id != workspace_id:
        return Decision(allowed=False, reason=_NOT_BOUND_REASON)
    if bound.credential_id != state.reference.credential_id:
        return Decision(allowed=False, reason=_NOT_BOUND_REASON)
    return Decision(allowed=True)


# ---------------------------------------------------------------------------
# Lifecycle — every transition produced by the contract, then persisted
# ---------------------------------------------------------------------------


async def _commit_authorized(
    db: AsyncSession, verify_authority: Callable[[], Awaitable[None]]
) -> None:
    """Validate current evidence before writes and after any flush lock waits."""
    try:
        await verify_authority()
        await db.flush()
        await verify_authority()
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def register(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    reference: CredentialReference,
    provider: str,
    owner_principal: str,
    bound_by: str,
    verify_authority: Callable[[], Awaitable[None]],
) -> tuple[ProviderConnection, ProviderConnectionBinding]:
    """Record a connection and its single workspace binding.

    Created PENDING, never ACTIVE: a reference that has not been validated must admit
    no work, and ``ConnectionState`` refuses an ACTIVE status without a passing
    report, so this ordering is the contract's rather than a convention.

    The router supplies ``owner_principal`` from verified vault evidence after
    checking the caller and workspace grant. This historical owner is not reused
    as live authority on subsequent mutations. ``bound_by`` records the verified
    caller who performed the binding, including an explicitly authorized delegate.
    """
    connection = ProviderConnection(
        org_id=org_id,
        provider=provider,
        adp_credential_id=reference.credential_id,
        credential_service=reference.service,
        credential_label=reference.label,
        owner_principal=owner_principal,
        status=STATUS_PENDING,
    )
    db.add(connection)
    await db.flush()

    binding = ProviderConnectionBinding(
        connection_id=connection.id,
        adp_credential_id=reference.credential_id,
        workspace_id=workspace_id,
        bound_by=bound_by,
    )
    db.add(binding)
    await _commit_authorized(db, verify_authority)
    await db.refresh(connection)
    await db.refresh(binding)
    logger.info(
        "registered provider connection %s for workspace %s (provider %s)",
        connection.id,
        workspace_id,
        provider,
    )
    return connection, binding


async def record_activation(
    db: AsyncSession,
    *,
    connection: ProviderConnection,
    binding: ProviderConnectionBinding,
    report: ValidationReport,
    verify_authority: Callable[[], Awaitable[None]],
) -> ConnectionState:
    """Activate a connection against a validation report, via the contract.

    ``activate`` refuses a report whose credential did not validate, so ACTIVE always
    means "checked and working" rather than "somebody called activate". Raising out of
    the contract rather than storing an optimistic status is the point.
    """
    state = activate(to_state(connection, binding), report)
    _persist_state(connection, state)
    await _commit_authorized(db, verify_authority)
    await db.refresh(connection)
    return state


async def record_failed_validation(
    db: AsyncSession,
    *,
    connection: ProviderConnection,
    report: ValidationReport,
    verify_authority: Callable[[], Awaitable[None]],
) -> None:
    """Store a report that did NOT establish a usable credential.

    Preserve all four readings and stop admitting work by returning an ACTIVE
    connection to PENDING. Leaving ACTIVE beside a failing report would violate
    the connection contract and make the next read fail instead of exposing the
    new health evidence.
    """
    _apply_validation(connection, report)
    if connection.status == STATUS_ACTIVE:
        connection.status = STATUS_PENDING
    await _commit_authorized(db, verify_authority)
    await db.refresh(connection)


async def record_rotation(
    db: AsyncSession,
    *,
    connection: ProviderConnection,
    binding: ProviderConnectionBinding,
    replacement: CredentialReference,
    replacement_validation: ValidationReport,
    rotated_at: datetime,
    rotated_by: str,
    verify_authority: Callable[[], Awaitable[None]],
) -> RotationResult:
    """Switch onto an already-validated replacement, atomically (acceptance 5).

    The route holds a database row lock across authorization and this transaction.
    The contract's ``rotate`` signature cannot
    be called without ``replacement_validation``, and it returns one new state rather
    than a sequence with an interval in which the connection references nothing. The
    superseded reference comes back in the result for the caller to revoke as a
    deliberate later step — this module has no operation that deletes it, so no
    sequence expressible here produces the dead window acceptance 5 forbids.

    The binding row is updated to the replacement credential in the same transaction
    as the connection, because the two are compared against each other on every
    subsequent ``authorize_use``; committing one without the other would leave a
    connection whose own binding denies it.
    """
    result = rotate(
        to_state(connection, binding),
        replacement=replacement,
        replacement_validation=replacement_validation,
        rotated_at=rotated_at,
        rotated_by=rotated_by,
    )
    _persist_state(connection, result.connection)
    binding.adp_credential_id = result.connection.binding.credential_id
    binding.bound_by = result.connection.binding.bound_by
    binding.bound_at = result.connection.binding.bound_at
    await _commit_authorized(db, verify_authority)
    await db.refresh(connection)
    await db.refresh(binding)
    logger.info(
        "rotated provider connection %s onto a validated replacement; the superseded "
        "credential remains registered for separate revocation",
        connection.id,
    )
    return result


async def record_disablement(
    db: AsyncSession,
    *,
    connection: ProviderConnection,
    binding: ProviderConnectionBinding,
    verify_authority: Callable[[], Awaitable[None]],
) -> ConnectionState:
    """Disable a connection and persist the limitation the contract attaches.

    The limitation is stored, not reconstructed from a constant at read time, so the
    response and any later read report the same text. Acceptance 5 is about an
    operator *seeing* what disablement did not accomplish: it does not revoke
    credentials already delivered to running workloads.
    """
    state = disable(to_state(connection, binding))
    _persist_state(connection, state)
    await _commit_authorized(db, verify_authority)
    await db.refresh(connection)
    return state
