"""The production `provider_authority` adapter over `harness_jobs`.

Issue #5535 (Superplane W6), EPIC #4910.

Implements the `ProviderAuthorityValidator` Protocol declared at
`app/services/provider_authority.py:35`. That module's docstring states the
requirement this adapter has to meet: "A future trusted adapter must verify B's
live authority and resolve its authenticated executor and exact operation context.
The opaque request value is never persisted or treated as proof on its own."

## Resolve, then compare — never echo

The consumer (`provider_handles._verify_authority`) requires the returned binding's
`handle` to equal `replace(handle, provider_reference=None)` field for field. It is
worth being precise about why that does *not* make this an echo, because an earlier
revision of `app/composition.py` concluded it did, and left the port permanently
uncomposed on that basis:

> `_verify_authority` requires the resolved binding to equal the request handle
> field for field, so an adapter cannot synthesize the missing values by echoing
> the request — that is precisely the "create a binding from the supplied handle
> alone" the port forbids.

The equality check is the consumer's way of asking "did you agree with what I
presented?", and the only safe way to answer it is to resolve the authoritative
values independently and *compare*. That is what `_authorized_handle` does. It
returns the presented identity only after every field that carries authority has
been checked against server-held state, and returns `None` on any disagreement —
never a corrected handle, because a validator that silently substituted the real
allocation would authorize a call against an allocation the caller did not ask
about.

The fields divide into three kinds, and each is handled differently:

* **Resolved from the approved plan.** `allocation_id` is read by
  `allocation_id_for(record)` from the digest-bound admitted request — a value a
  human approved — and compared. The harness explains why that source and no
  other (`allocation.py:119-124`): a worker-supplied allocation id "would let an
  executor with a valid lease publish a valid report naming somebody else's
  allocation and collect cleanup authority over resources it was never approved
  to touch."
* **Resolved from the live lease.** `workspace` is compared against
  `lease.workspace_id`, and the presented `submitter_id` against `lease.holder`.
  Both come off the lease row this process just locked, so they are the
  database's answer about the present, not the caller's claim.
* **Bound but not independently derivable.** `provider`, `resource_name`,
  `idempotency_key` and `operation` describe *what call to make*, not *whose
  authority to make it under*. When the approved plan declares them they are
  compared like the first kind; when it does not, they are carried through
  unchecked and that is stated rather than hidden. This is safe for one specific
  reason: none of them widens authority. The allocation, the workspace and the
  holder — the three values that decide what may be touched and by whom — are all
  in the first two kinds. What the last kind does affect is the durable pre-call
  record, and `record_handle` persists it under a unique idempotency key before
  the provider call, with `conclude_operation` re-verifying the same triple
  afterwards.

## `run_id`, which the lease does not have a field for

Derived by `harness_execution_authority.run_id_for` from
`(operation_id, fence_token)`. See that module's docstring for why the fence *is*
the run identity and why deriving it from server state rather than from the request
is what makes a recovery takeover refuse the previous attempt's conclusion.

## Why this never raises

`NONE_MEANS_UNVERIFIED`. `_verify_authority` catches every exception and answers
503 "B operation authority is unavailable", so a raise would report a caller's
lack of authority as an outage — a 503 where the truth is a refusal, telling the
caller to retry something no retry will make permissible. Every refusal path here
returns `None`; `asyncio.CancelledError` is a `BaseException` and still propagates.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from superplane_contracts import ProviderHandle, Submitter

from app.adapters.harness_execution_authority import (
    MAX_IDENTIFIER_LENGTH,
    run_id_for,
)
from app.services.provider_authority import VerifiedProviderAuthority

logger = logging.getLogger(__name__)

# Plan parameter names that, when the approved plan declares them, are compared
# against the presented handle. Absent from a plan is normal and is not a refusal
# — see the module docstring on the third kind of field. Present and disagreeing
# is always a refusal.
_PLAN_BOUND_HANDLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("provider", "provider"),
    ("resource_name", "resource_name"),
    ("idempotency_key", "idempotency_key"),
    ("operation", "operation"),
)


class HarnessProviderAuthority:
    """Verifies a presented operation authority against a live harness lease."""

    def __init__(self, execution_authority: Any) -> None:
        self._authority = execution_authority

    async def resolve(
        self, authority: str, *, submitter: Submitter, handle: ProviderHandle
    ) -> VerifiedProviderAuthority | None:
        """The server-resolved authority context, or `None` meaning unverified."""
        try:
            return await self._resolve(authority, submitter=submitter, handle=handle)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Unverified, not unavailable. Logged without the authority value and
            # without the exception message: a harness refusal names the operation
            # and tenant it refused for, and this path is reached by callers who
            # named neither.
            logger.info("a provider authority could not be verified", exc_info=False)
            return None

    async def _resolve(
        self, authority: str, *, submitter: Submitter, handle: ProviderHandle
    ) -> VerifiedProviderAuthority | None:
        if not isinstance(submitter, Submitter) or not isinstance(
            handle, ProviderHandle
        ):
            return None

        resolved = await self._authority.resolve_execution(authority)
        if resolved is None:
            return None

        lease = resolved.lease
        identity = _authorized_handle(
            handle,
            record=resolved.record,
            allocation_id=resolved.allocation_id,
            lease=lease,
        )
        if identity is None:
            return None

        # The presented submitter must BE the lease holder. Compared rather than
        # trusted, and compared against the locked lease row rather than against
        # the principal this process built, so the check is against what the
        # database says holds the operation now.
        if submitter.submitter_id != lease.holder:
            return None
        if submitter.workspaces and identity.workspace not in submitter.workspaces:
            # A submitter presenting a workspace outside its own grant. The
            # consumer checks this too (`_authorize_workspace`); both check it
            # because each guards a different mistake, and this one is free.
            return None

        expires_at = lease.expires_at
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            return None
        if expires_at.utcoffset() is None:
            return None
        if expires_at <= datetime.now(UTC):
            # Lapsed between `lock_lease` and here. Refused rather than returned
            # with `active=True`: the consumer re-checks expiry immediately before
            # and after its commit, and handing it an already-lapsed window would
            # make the first of those checks the one that catches this, one layer
            # further from where it was known.
            return None

        run_id = run_id_for(lease)
        identifiers = (lease.operation_id, run_id, lease.attempt_id)
        if not all(
            isinstance(value, str)
            and value.strip()
            and len(value) <= MAX_IDENTIFIER_LENGTH
            for value in identifiers
        ):
            # Mirrors the consumer's own bound check. Refused here as well so a
            # value that cannot be persisted is never reported as verified.
            return None

        return VerifiedProviderAuthority(
            operation_id=lease.operation_id,
            run_id=run_id,
            attempt_id=lease.attempt_id,
            submitter_id=lease.holder,
            handle=identity,
            expires_at=expires_at,
            active=True,
        )


def _authorized_handle(
    handle: ProviderHandle,
    *,
    record: Any,
    allocation_id: str,
    lease: Any,
) -> ProviderHandle | None:
    """The presented identity, if every authority-carrying field agrees.

    Returns the handle *without* its provider reference, which is the form the
    consumer compares against (`replace(handle, provider_reference=None)`). A
    presented reference is dropped rather than refused: the reference is the
    provider's own answer and arrives legitimately on a recovery report, and it
    carries no authority, so it is not this function's business.

    `None` on any disagreement, and never a corrected handle. Correcting would
    authorize a call against an allocation or workspace the caller did not name,
    which is the same class of defect as honouring a caller-supplied tenant.
    """
    identity = replace(handle, provider_reference=None)

    if identity.allocation_id != allocation_id:
        # The approved plan binds a different allocation than the caller named.
        return None
    if identity.workspace != lease.workspace_id:
        return None

    parameters = _admitted_parameters(record)
    if parameters is None:
        # The stored payload did not decode or disagreed with its digest. The
        # harness treats that as a request nobody approved, and a provider call
        # under a plan this process cannot verify must not be authorized at all.
        return None

    for parameter, field_name in _PLAN_BOUND_HANDLE_FIELDS:
        declared = parameters.get(parameter)
        if declared is None:
            continue
        presented = getattr(identity, field_name)
        # `OperationKind` is `str`-valued, so its wire form compares directly
        # against a plan parameter without converting either side.
        presented_value = getattr(presented, "value", presented)
        if str(declared) != str(presented_value):
            return None

    return identity


def _admitted_parameters(record: Any) -> dict[str, str] | None:
    """The digest-bound parameters of the approved request, or `None`.

    Read through `admitted_request()`, which re-validates the stored payload
    against the digest committed beside it, so these are parameters a human
    approved rather than whatever is currently in the column.
    """
    try:
        request = record.admitted_request()
    except Exception:
        return None
    parameters = getattr(request, "parameters", None)
    return dict(parameters) if isinstance(parameters, dict) else None
