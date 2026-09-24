"""Compare every provider handle field to the approved request and protected run.

An omitted plan field cannot authorize an arbitrary provider target. The actual
ADP invocation identity comes from Gateway verification, separately from its lease
fence and original admission identity.
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
)
from app.services.provider_authority import VerifiedProviderAuthority

logger = logging.getLogger(__name__)

# All provider target fields must be bound by the approved request.
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

        resolved = await self._authority.resolve_submitter(
            authority, submitter=submitter, workspace=handle.workspace
        )
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
        if identity.workspace not in submitter.workspaces:
            # A submitter presenting a workspace outside its own grant. The
            # consumer checks this too (`_authorize_workspace`); both check it
            # because each guards a different mistake, and this one is free.
            return None

        expires_at = min(lease.expires_at, lease.runtime_deadline, resolved.not_after)
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

        run_id = resolved.run_id
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
            return None
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
