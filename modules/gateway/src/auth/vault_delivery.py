"""Operation-bound credential delivery — Issue #5528 (Wave 6 / w6-05).

One credential, one recipient, one operation. This module decides whether a
specific executor, running a specific job attempt for a specific workspace, may
receive the value of a specific credential — and refuses everything else.

What this replaces
------------------
The previous credential-delivery attempt was cluster-wide secret replication,
withdrawn in #5046: it copied a value into a namespace-visible Kubernetes Secret
with no run binding, no expiry and no run-tied revocation, and its success flag
reported a delivery it had not performed. ``superplane_contracts.delivery`` keeps
``EXTERNAL_SECRETS_REPLICATION_RETIRED`` as an asserted constant so that pattern
cannot be re-extended. Delivery here is recipient-bound instead.

Why nothing in the request is trusted
-------------------------------------
A delivery request names a job, attempt, workspace, credential and recipient. None
of that authorizes anything: it is the *claim* being checked. Authority is
re-derived from the Gateway's own records every time —

* the credential is resolved EXACTLY by id within the caller's tenant
  (:func:`~src.auth.vault_evidence.resolve_exact_credential`, never the
  ranking resolver, which would happily return a different credential);
* the credential must carry an active delegation to the named workspace;
* it must not be expired;
* and the AUTHENTICATED caller must be the named recipient — presenting another
  executor's id is refused, which is what makes the lease recipient-bound rather
  than recipient-labelled.

Refusals are uniform
--------------------
Every denial raises :class:`DeliveryRefusedError` with one of a small set of fixed
reasons that do not reveal whether the credential exists, belongs to another
tenant, or is merely undelegated. A refusal that distinguished those cases would be
an enumeration oracle over other tenants' vaults. The contract's registry entry
declares ``refusal_exceptions=("DeliveryRefused",)``, so a refusal must be this
exception type and never an ``AttributeError`` or a bare ``PermissionError``.

The value's path through this module
------------------------------------
Fetched at the moment of delivery and returned to the caller. It is never written
to a table, never attached to any model, and never placed in a log record or a
refusal message — including when a provider error nests it inside another
structure. The value is carried in :class:`DeliveredSecret`, whose ``repr`` is
redacted, so an accidental log of the container cannot leak the contents.

The limit this module must state out loud
-----------------------------------------
Expiring or revoking a delivery does not revoke the provider's key. A long-lived
key already handed to a running workload stays usable at the provider until it is
revoked there and existing sessions are terminated. An operator who believes
disabling a delivery contained a leak will skip the step that actually does, so
:data:`REVOCATION_LIMITATION` travels with every revocation answer rather than
living only in this docstring.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.vault import CredentialValidationEvidence, CredentialWorkspaceDelegation, UserCredential
from src.shared.services.secrets_manager import SecretsManagerHelper

from .operation_contract import identity_contract
from .vault_evidence import resolve_exact_credential

logger = logging.getLogger(__name__)

# Mirrored from `superplane_contracts.delivery.REVOCATION_LIMITATION`. Mirrored
# rather than imported for the reason the domain's own `models/credential.py`
# mirrors the secret regexes: the Gateway does not depend on the contract package,
# and a hard import would make this module unusable without it. A drift test in
# `tests/auth/test_vault_delivery.py` compares this against the contract's text
# when that package is importable, so the copy cannot silently diverge.
REVOCATION_LIMITATION = (
    "A delivery lease bounds this executor's access, not the credential itself. A "
    "long-lived provider key already delivered to a running workload stays usable "
    "until it is revoked at the provider and existing sessions are terminated; "
    "expiring or disabling the lease here does not revoke it."
)

# The permission required to RECEIVE a credential for use, pinned as a string for
# the same packaging reason. `superplane_contracts.delivery` explains the split:
# obtaining a credential for use is `workspace:provision`, while registering or
# rotating one is `workspace:renew_credential`. A request presenting the management
# permission is refused below — managing a credential is not authority to use it.
DELIVERY_PERMISSION = "workspace:provision"
CREDENTIAL_MANAGEMENT_PERMISSION = "workspace:renew_credential"

# One reason per denial class, deliberately coarse. See the module docstring.
_REFUSED = "credential delivery refused"
_REVOKED = "credential no longer admits work"


class DeliveryRefusedError(PermissionError):
    """A well-formed delivery request the Gateway will not serve.

    A ``PermissionError`` subclass so it maps onto the contract's
    ``DeliveryRefused`` (also a ``PermissionError``) at the client boundary, and so
    a caller handling authorization failures uniformly catches this too.

    The message is one of the fixed constants above. It never carries the
    credential id, the tenant, or any provider detail: an exception string reaches
    logs and agent transcripts, and a refusal that explained itself precisely would
    let a caller map another tenant's vault by reading the differences.
    """


class OperationAuthorityUnavailableError(RuntimeError):
    """Executor storage could not establish current operation authority."""


class DeliveredSecret:
    """A credential value that refuses to render itself.

    Mirrors ``superplane_contracts.delivery.SecretMaterial`` and for the same
    reasons: not a dataclass, because a generated ``repr`` would print the value,
    which is the whole problem; not a ``str`` subclass, because every string
    operation would then produce an unprotected ``str`` and the protection would
    end at the first ``+`` or ``%``.

    ``reveal()`` is the single accessor, named to be conspicuous at a call site and
    in review. Pickling is refused: serialisation is how a value ends up in a queue
    message, a cache or a cross-process result, and nothing here needs it.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("delivered secret must be a non-empty string")
        self._value = value

    def reveal(self) -> str:
        """Return the raw value. The only accessor, deliberately conspicuous."""
        return self._value

    def __repr__(self) -> str:
        return "DeliveredSecret([REDACTED])"

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        # Without this, an explicit format spec could bypass the redacted __str__.
        return repr(self)

    def __reduce__(self):
        raise TypeError("delivered secret cannot be serialized")

    def __eq__(self, other: object) -> bool:
        # So a test can compare without revealing the value in an assertion message.
        return isinstance(other, DeliveredSecret) and self._value == other._value

    # Deliberately unhashable: a hashable secret can be a dict key, and dict keys
    # get logged when the dict does.
    __hash__ = None  # type: ignore[assignment]


@dataclass(frozen=True)
class OperationBinding:
    """The operation a delivery is bound to, as the caller asserts it.

    Field names match ``harness_jobs.identity.OperationBinding`` so the two can be
    reconciled without a translation table. ``attempt_id`` is distinct from
    ``operation_id`` because an operation may be attempted more than once.

    Attempt LEASES and FENCES are #5527's (w6-04) scope, not this module's. This
    binds to the identity that story stores rather than minting a competing fence —
    two independent fences over one attempt would each believe it held the lock.
    """

    operation_id: str
    attempt_id: str
    job_id: str
    org_id: str
    workspace_id: str
    provider: str = ""
    provider_account_id: str = ""

    def __post_init__(self) -> None:
        for name in ("operation_id", "attempt_id", "job_id", "org_id", "workspace_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DeliveryRefusedError(_REFUSED)


@dataclass(frozen=True)
class RevocationAnswer:
    """Whether a credential still admits work, plus the limitation of saying no.

    ``limitation`` is a field rather than a docstring because surfacing it is the
    requirement: it is populated whenever work is refused, and
    :data:`REVOCATION_LIMITATION` is the text. Mirrors the contract's
    ``RevocationState``, which raises if a refusal carries no limitation.
    """

    admits_work: bool
    limitation: str = ""

    def __post_init__(self) -> None:
        if not self.admits_work and not self.limitation:
            raise ValueError("a credential that no longer admits work must surface its limitation")


def _expired(credential: UserCredential, now: datetime) -> bool:
    """True when the credential's own expiry has passed.

    Naive stored timestamps are read as UTC: SQLite drops tzinfo on round-trip
    while Postgres preserves it, so comparing a naive value against an aware ``now``
    would raise on one backend and not the other.
    """
    expiry = credential.expires_at
    if expiry is None:
        return False
    return (expiry if expiry.tzinfo is not None else expiry.replace(tzinfo=UTC)) <= now


async def _active_delegation(
    session: AsyncSession,
    *,
    org_id: str,
    credential_id: str,
    workspace_id: str,
) -> CredentialWorkspaceDelegation | None:
    """The active delegation for this credential in this workspace, if any.

    Scoped by tenant as well as credential and workspace. A revoked row is excluded
    here but still exists, which is what lets the caller distinguish a withdrawn
    delegation from one that never existed when diagnosing — without that
    distinction reaching the refusal message.
    """
    return await session.scalar(
        select(CredentialWorkspaceDelegation).where(
            CredentialWorkspaceDelegation.credential_id == credential_id,
            CredentialWorkspaceDelegation.workspace_id == workspace_id,
            CredentialWorkspaceDelegation.org_id == org_id,
            CredentialWorkspaceDelegation.revoked_at.is_(None),
        )
    )


async def _validate_operation_binding(session, binding, authenticated_recipient):
    """Resolve authority from the durable operation and its current execution lease.

    The authenticated run identity is the lease holder, not an encoding of an
    operation/job/attempt. Recheck database-clock liveness on every delivery and
    after secret I/O. Missing executor storage refuses; it never enables fallback
    to a caller-asserted binding. #5535 owns production database composition.
    """
    try:
        async with session.begin_nested():
            result = await session.execute(
                text("""
            SELECT l.fence_token, o.plan_digest, o.request_payload
              FROM harness_operations o
              JOIN harness_operation_leases l ON l.operation_id=o.operation_id
              JOIN harness_approval_consumption a ON a.operation_id=o.operation_id
             WHERE o.operation_id=:operation_id AND o.job_id=:job_id
               AND o.org_id=:org_id AND o.workspace_id=:workspace_id
               AND l.org_id=o.org_id AND l.workspace_id=o.workspace_id
               AND l.attempt_id=:attempt_id AND l.holder=:holder
               AND l.closed_at IS NULL AND l.expires_at > clock_timestamp()
               AND l.runtime_deadline > clock_timestamp()
               AND o.state IN ('pending', 'running')
               AND o.cancel_requested_at IS NULL AND NOT o.cleanup_required
               AND a.reservation_state = 'confirmed'
               AND a.org_id=o.org_id AND a.workspace_id=o.workspace_id
               AND a.plan_digest=o.plan_digest
        """),
                dict(
                    operation_id=binding.operation_id,
                    job_id=binding.job_id,
                    org_id=binding.org_id,
                    workspace_id=binding.workspace_id,
                    attempt_id=binding.attempt_id,
                    holder=authenticated_recipient,
                ),
            )
            row = result.mappings().one_or_none()
    except SQLAlchemyError:
        raise OperationAuthorityUnavailableError("operation authority unavailable") from None
    if row is None or type(row["fence_token"]) is not int or row["fence_token"] < 1:
        raise DeliveryRefusedError(_REFUSED)
    try:
        contract = identity_contract()
    except RuntimeError:
        raise OperationAuthorityUnavailableError("operation contract unavailable") from None
    try:
        reference = contract.admitted_credential_reference(row["request_payload"], row["plan_digest"])
        target = contract.admitted_credential_target(row["request_payload"], row["plan_digest"])
    except ValueError:
        raise DeliveryRefusedError(_REFUSED) from None
    if target != (binding.provider, binding.provider_account_id) or reference[1] != target[0]:
        raise DeliveryRefusedError(_REFUSED)
    return row["fence_token"], row["plan_digest"], reference, target


async def authorize_delivery(
    session: AsyncSession,
    *,
    binding: OperationBinding,
    credential_id: str,
    service: str | None,
    label: str | None,
    recipient: str,
    authenticated_recipient: str,
    granted_permissions: frozenset[str] | set[str],
    now: datetime | None = None,
    expected_authority: tuple | None = None,
) -> UserCredential:
    """Re-derive delivery authority from the Gateway's records, or refuse.

    Returns the resolved credential on success. Raises
    :class:`DeliveryRefusedError` — never returns a partial answer — for a missing,
    foreign, ambiguous, undelegated, revoked or expired reference, for a caller that
    is not the named recipient, and for a caller lacking the delivery permission.

    ``authenticated_recipient`` is the identity the TRANSPORT established;
    ``recipient`` is the identity the request NAMES. Requiring them to match is what
    makes a lease recipient-bound: without it, any authenticated executor could
    request another executor's credential by naming it.

    The operation binding (``operation_id``, ``attempt_id``, ``job_id``) is verified
    against the authenticated principal, not accepted at face value — see
    :func:`_validate_operation_binding`. This closes the F1 foreground finding:
    a caller that replaced any of these fields with an arbitrary string while keeping
    the recipient correct was asserting a binding the transport never verified.
    """
    moment = now or datetime.now(UTC)

    if not isinstance(recipient, str) or not recipient.strip():
        raise DeliveryRefusedError(_REFUSED)
    if not isinstance(authenticated_recipient, str) or not authenticated_recipient.strip():
        raise DeliveryRefusedError(_REFUSED)
    # The transport's identity is the authority; the request's name must match it.
    if recipient != authenticated_recipient:
        raise DeliveryRefusedError(_REFUSED)

    # Validate the full operation binding against the authenticated principal.
    # A mismatch on operation_id, attempt_id, or job_id refuses here before any
    # vault read — the same ordering as the recipient check above.
    authority = await _validate_operation_binding(session, binding, authenticated_recipient)
    if expected_authority is not None and authority != expected_authority:
        raise DeliveryRefusedError(_REFUSED)
    admitted_id, admitted_service, admitted_label = authority[2]
    if credential_id != admitted_id or (service is not None and service != admitted_service) or (label is not None and label != admitted_label):
        raise DeliveryRefusedError(_REFUSED)

    if not isinstance(granted_permissions, set | frozenset) or any(not isinstance(p, str) for p in granted_permissions):
        raise DeliveryRefusedError(_REFUSED)
    # Managing a credential is not authority to use it: a request carrying only the
    # management permission is refused rather than upgraded.
    if DELIVERY_PERMISSION not in granted_permissions:
        raise DeliveryRefusedError(_REFUSED)

    if not isinstance(credential_id, str) or not credential_id.strip():
        raise DeliveryRefusedError(_REFUSED)

    credential = await resolve_exact_credential(
        session,
        org_id=binding.org_id,
        credential_id=credential_id,
        service=admitted_service,
        label=admitted_label,
    )
    if credential is None:
        # Unknown, foreign, or a reference whose metadata disagrees with the stored
        # row. One reason for all three — see the module docstring.
        raise DeliveryRefusedError(_REFUSED)

    delegation = await _active_delegation(
        session,
        org_id=binding.org_id,
        credential_id=credential.id,
        workspace_id=binding.workspace_id,
    )
    if delegation is None:
        raise DeliveryRefusedError(_REFUSED)

    if _expired(credential, moment):
        raise DeliveryRefusedError(_REVOKED)

    return credential


async def revocation_state(
    session: AsyncSession,
    *,
    binding: OperationBinding,
    credential_id: str,
    now: datetime | None = None,
) -> RevocationAnswer:
    """Whether this credential still admits work for this workspace.

    Always carries :data:`REVOCATION_LIMITATION` when the answer is no, because the
    operator reading it needs to know that a "no" here does not contain an already
    delivered provider key.
    """
    moment = now or datetime.now(UTC)
    credential = await resolve_exact_credential(session, org_id=binding.org_id, credential_id=credential_id)
    if credential is None:
        return RevocationAnswer(admits_work=False, limitation=REVOCATION_LIMITATION)
    delegation = await _active_delegation(
        session,
        org_id=binding.org_id,
        credential_id=credential.id,
        workspace_id=binding.workspace_id,
    )
    if delegation is None or _expired(credential, moment):
        return RevocationAnswer(admits_work=False, limitation=REVOCATION_LIMITATION)
    return RevocationAnswer(admits_work=True)


async def _validated_version(session, binding, credential_id, version_id):
    """Read current provider validation, including on the post-fetch path.

    Validation is scoped to the exact tenant/workspace/version. Its independent
    readings must permit provisioning; a future observation is not evidence.
    """
    row = await session.scalar(
        select(CredentialValidationEvidence)
        .where(
            CredentialValidationEvidence.org_id == binding.org_id,
            CredentialValidationEvidence.workspace_id == binding.workspace_id,
            CredentialValidationEvidence.credential_id == credential_id,
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise DeliveryRefusedError(_REFUSED)
    checked = row.checked_at.replace(tzinfo=UTC) if row.checked_at.tzinfo is None else row.checked_at
    if (
        not version_id
        or row.validated_version_id != version_id
        or not row.provider_account_id
        or row.provider_account_id != binding.provider_account_id
        or not row.credential_valid
        or not row.permissions_sufficient
        or not row.quota_available
        or checked > datetime.now(UTC)
    ):
        raise DeliveryRefusedError(_REFUSED)
    return (
        row.id,
        row.validated_version_id,
        row.provider_account_id,
        checked,
        row.credential_valid,
        row.permissions_sufficient,
        row.quota_available,
        row.observed_capacity,
        row.detail,
    )


async def deliver_credential(
    session: AsyncSession,
    sm: SecretsManagerHelper,
    *,
    binding: OperationBinding,
    credential_id: str,
    service: str | None,
    label: str | None,
    recipient: str,
    authenticated_recipient: str,
    granted_permissions: frozenset[str] | set[str],
    refresh_executor: Callable[[], Awaitable[frozenset[str] | set[str]]],
    preflight_only: bool = False,
    now: datetime | None = None,
) -> tuple[UserCredential, DeliveredSecret | None]:
    """Authorize, then fetch and hand over the credential value.

    Authorization is re-derived TWICE around the fetch, following the established
    pattern in ``internal/credential_routes.py``'s materialize endpoint (which
    re-checks its binding before and after reading the secret). The second check
    exists because the fetch is an ``await``: a revocation committed during it would
    otherwise be raced by a request that had already passed the only check.

    The returned value is wrapped in :class:`DeliveredSecret` so it cannot be
    rendered into a log line by accident, and nothing here persists it.

    ``preflight_only`` is a server-selected mode of the separate preflight route.
    It exercises the same authorization and post-I/O refreshes while reading only
    provider version metadata, returning no secret and recording no delivery.
    """
    moment = now or datetime.now(UTC)
    authority = await _validate_operation_binding(session, binding, authenticated_recipient)
    credential = await authorize_delivery(
        session,
        binding=binding,
        credential_id=credential_id,
        service=service,
        label=label,
        recipient=recipient,
        authenticated_recipient=authenticated_recipient,
        granted_permissions=granted_permissions,
        expected_authority=authority,
        now=moment,
    )

    snapshot = (
        credential.id,
        credential.secret_arn,
        credential.service,
        credential.label,
        credential.user_id,
        credential.team_id,
        credential.credential_type,
    )

    # Pin the version that is AWSCURRENT at this moment. A delivery must hand the
    # caller exactly the version that was current when authorization passed; if
    # rotation advanced AWSCURRENT between the authorize call and the get_secret_value
    # call, the caller would receive bytes from a version that was never validated.
    #
    # An absent or ambiguous current version refuses here rather than falling back to
    # unversioned: "I could not establish the version" must not silently degrade into
    # "hand whatever is current", which is the unversioned semantics.
    try:
        version_id = await asyncio.to_thread(sm.current_version_id, credential.secret_arn)
    except Exception:
        raise DeliveryRefusedError(_REFUSED) from None
    if not version_id:
        logger.warning("Cannot determine current version for credential %s; refusing delivery", credential.id)
        raise DeliveryRefusedError(_REFUSED) from None

    validation = await _validated_version(session, binding, credential.id, version_id)

    try:
        # Synchronous boto3 call offloaded to a thread, matching every other
        # Secrets Manager call site in the vault (see `vault_service.py`).
        # `get_secret_at_version` returns (value, actual_version): we verify the
        # actual version matches below (F3 fix — rotation-during-fetch check).
        if preflight_only:
            value, served_version = None, version_id
        else:
            value, served_version = await asyncio.to_thread(sm.get_secret_at_version, credential.secret_arn, version_id)
    except Exception:
        # The provider's error is NOT propagated: a Secrets Manager error body can
        # echo the value or the ARN, and this exception string reaches logs and
        # agent transcripts. `from None` severs the cause so the original is not
        # re-raised inside a traceback either.
        logger.warning("Secret fetch failed during delivery for credential %s", credential.id)
        raise DeliveryRefusedError(_REFUSED) from None

    # Verify the version we requested is the version we got. Secrets Manager should
    # return what we asked for, but a mismatch here would mean we are about to deliver
    # bytes from a different version than the one we verified existence of.
    if served_version != version_id:
        logger.warning("Secret version mismatch during delivery")
        raise DeliveryRefusedError(_REVOKED) from None

    # Re-verify the version is still AWSCURRENT after the fetch.  If rotation landed
    # during the fetch window, this refuses to deliver bytes from a version that is
    # no longer the validated current version.
    try:
        post_version_id = await asyncio.to_thread(sm.current_version_id, credential.secret_arn)
    except Exception:
        raise DeliveryRefusedError(_REFUSED) from None
    if post_version_id != version_id:
        logger.warning("Version rotated during delivery for credential %s; refusing", credential.id)
        raise DeliveryRefusedError(_REVOKED) from None

    if await _validated_version(session, binding, credential.id, version_id) != validation:
        raise DeliveryRefusedError(_REVOKED)
    granted_permissions = await refresh_executor()

    # Refresh the approved reference, live lease/fence and vault delegation after
    # provider I/O. Snapshot comparison also rejects a changed admission digest.
    current = await authorize_delivery(
        session,
        binding=binding,
        credential_id=snapshot[0],
        service=service,
        label=label,
        recipient=recipient,
        authenticated_recipient=authenticated_recipient,
        granted_permissions=granted_permissions,
        expected_authority=authority,
        now=datetime.now(UTC),
    )
    if (current.id, current.secret_arn, current.service, current.label, current.user_id, current.team_id, current.credential_type) != snapshot:
        raise DeliveryRefusedError(_REVOKED)

    if preflight_only:
        return credential, None

    if not isinstance(value, str) or not value:
        raise DeliveryRefusedError(_REFUSED)

    # Audit records the DELIVERY, never the value. Fields are all identifiers.
    logger.info(
        "Delivered credential id=%s org=%s workspace=%s operation=%s attempt=%s recipient=%s",
        credential.id,
        binding.org_id,
        binding.workspace_id,
        binding.operation_id,
        binding.attempt_id,
        recipient,
    )
    return credential, DeliveredSecret(value)
