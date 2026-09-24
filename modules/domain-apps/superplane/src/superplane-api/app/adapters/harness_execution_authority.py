"""Resolving a presented operation authority into a live, server-held lease.

Issue #5535 (Superplane W6), EPIC #4910.

This module is the piece `harness_jobs` deliberately refuses to ship. Both
`InventoryAuthority.authenticate` and the `provider_authority` port need an
opaque string to become an `ExecutionGrant`, and the harness declares that seam
as a *required* injected callable with no default, giving the reason in
`inventory.py:602`: "a package that could mint the credential it checks is a
package whose authority check is decorative." So the resolution is the composer's,
and it is here.

## What the presented string is, and what it is not

It is a **reference**, not a credential. It names which operation the caller
claims to be acting under. It is not proof of anything on its own, which is the
property `app/services/provider_authority.py` states as "the opaque request value
is never persisted or treated as proof on its own".

The proof is the conjunction of two things this process already holds:

1. **who the caller is** — `operation_authority_source.acting_principal()`, built
   by the request layer from an already-verified token and an already-bound
   organization. Never from a request body.
2. **who the database says holds the lease right now** — read from
   `harness_operation_leases` under `lock_lease`, which requires the row to be
   unclosed, unexpired and inside its runtime deadline *as judged by the
   database's own `clock_timestamp()`*.

A grant is produced only when those two agree. `ExecutionGrant.__post_init__`
enforces the agreement itself — it refuses unless
`(principal.org_id, principal.workspace_id, principal.subject)` equals
`(lease.org_id, lease.workspace_id, lease.holder)` — so the check cannot be
skipped by a mistake in this module. Construction is the check.

The consequence worth stating plainly: knowing an operation id buys nothing. A
caller who presents another tenant's operation id resolves no record (the store's
reads are tenant-scoped in the WHERE clause), and a caller who presents an
operation in their own tenant that they do not hold the lease on fails the
holder comparison. There is no branch in which the string alone is sufficient.

## Where `run_id` comes from

`app/services/provider_authority.py` binds four identifiers —
`operation_id`, `run_id`, `attempt_id`, `submitter_id` — and `ExecutionLease`
carries no `run_id`. An earlier revision of `app/composition.py` read that gap as
a blocker and left the port permanently uncomposed, recording "harness_jobs has no
source for run_id".

The gap is real; the conclusion was wrong. The lease row *does* identify a run,
under a different name. `leases.py:578` advances `fence_token` on every grant and
every takeover, in the same statement that stamps the holder, so the pair
`(operation_id, fence_token)` names exactly one granted execution of one
operation and can never name a second. That is a run identity by any definition
that matters: it is minted server-side, it is durable, it is monotonic, and it
changes precisely when the thing it identifies changes.

So `run_id` is derived from the live lease row (`_run_id`) and never from the
caller. The derivation being *stable* is what makes it usable: `conclude_operation`
re-verifies `(operation_id, run_id, attempt_id)` against the values persisted at
`record_handle` time (`provider_handles.py:556-560`), and a value that varied per
read would refuse every conclusion. The derivation being *fence-derived* is what
makes it safe: when recovery takes the lease over, the fence advances, the derived
`run_id` changes, and the recorded-binding comparison refuses — which is the
correct outcome, because the new attempt must not conclude an operation whose
pre-call record belongs to the previous one.

## Why every failure is `None`

`resolve` and `read` both declare `NONE_MEANS_UNVERIFIED`, and the harness's own
`_grant` does the same thing for the same reason: a distinguishing answer here is
a probe. "No such operation", "not your operation" and "your fence is stale" would
let a caller enumerate another tenant's operations by reading which refusal came
back. One answer for all of them.

That also means this module must not let an exception escape. The consumer turns a
raise into a 503 — `provider_handles._verify_authority` catches everything and
reports "B operation authority is unavailable" — so an exception would report a
*refusal* as an outage and invite a retry of something no retry can make
permissible. Nothing is caught that must not be: `asyncio.CancelledError` is a
`BaseException` and propagates.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.adapters.operation_authority_source import (
    PROVISION_PERMISSION,
    acting_principal,
)

logger = logging.getLogger(__name__)

# The harness's own bound on an identifier it will accept, and the domain's bound
# on the three it persists (`provider_handles.py:271`, `models/provider_handle.py`
# `String(255)`). Enforced here so a derived value that would be truncated by the
# column is refused rather than silently shortened into a different identity.
MAX_IDENTIFIER_LENGTH = 255


class ResolvedExecution:
    """One resolved operation: its record, its live lease and its principal.

    Not a dataclass, and deliberately not frozen-by-convention-only: it holds the
    harness's own objects and exists to keep the three together, because every
    check downstream needs to compare across all three. Separating them is how a
    check ends up comparing a handle against a record whose lease belongs to a
    different attempt.
    """

    __slots__ = ("grant", "record", "allocation_id")

    def __init__(self, grant: Any, record: Any, allocation_id: str) -> None:
        self.grant = grant
        self.record = record
        self.allocation_id = allocation_id

    @property
    def lease(self) -> Any:
        return self.grant.lease

    @property
    def principal(self) -> Any:
        return self.grant.principal


def run_id_for(lease: Any) -> str:
    """The run identity of one lease grant. See the module docstring.

    `operation_id:fence_token`, readable rather than hashed, because this value is
    persisted in `provider_operations.authority_run_id` and read by an operator
    reconciling a stuck operation by hand. A digest would hide which attempt a row
    belongs to at exactly the moment that is the question being asked.
    """
    return f"{lease.operation_id}:{lease.fence_token}"


class HarnessExecutionAuthority:
    """Turns an operation-authority reference into an `ExecutionGrant`.

    Composed with the harness connection seam and an `OperationStore`. Holds no
    connection: `app/adapters/harness_connection.py` owns the pool, and this class
    acquires per call so a long-lived adapter never pins one.
    """

    def __init__(self, connect: Any, store: Any = None) -> None:
        self._connect = connect
        self._store = store

    async def authenticate(self, authority: str) -> Any:
        """The harness's `InventoryAuthority.authenticate` seam.

        Raises on every failure, because that is the shape the harness declares
        for this callable and `inventory._grant` converts a raise into its own
        `None`. The *ports* answer `None`; this seam answers the harness, and the
        harness does the conversion. Doing it twice would mean the ports' `None`
        no longer distinguished "unverified" from "this callable is broken".
        """
        resolved = await self.resolve_execution(authority)
        if resolved is None:
            from harness_jobs.identity import OperationRefused

            raise OperationRefused("the presented operation authority is not live")
        return resolved.grant

    async def resolve_execution(self, authority: object) -> ResolvedExecution | None:
        """The operation, lease and principal behind `authority`, or `None`.

        `None` for every failure — see the module docstring. The sequence is fixed
        and each step narrows what the next one may act on:

        1. the authority must be a usable reference at all;
        2. the *authenticated* caller must exist, hold the provisioning permission
           and be scoped to a workspace. Read from the contextvar, so nothing the
           request body carries reaches this;
        3. the operation record must exist **under that caller's tenant**, which
           `store.get` enforces in SQL rather than by filtering afterwards;
        4. the lease must be held *now*, by this caller, at the token the row
           currently carries. `lock_lease` asks the database, inside a
           transaction, against `clock_timestamp()`;
        5. `ExecutionGrant` must accept the pair, which re-checks (4)'s identity
           agreement independently of anything decided here.
        """
        operation_id = _reference(authority)
        if operation_id is None:
            return None

        caller = acting_principal()
        if caller is None:
            # No authenticated context. The boot-time capability probe arrives
            # here, which is why it observes a refusal without a database: this
            # returns before `self._connect` is touched.
            return None

        try:
            return await self._resolve(operation_id, caller)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Reported as unverified, not raised. The message is dropped: a
            # harness refusal names the operation and the tenant it refused for,
            # and this answer is returned to a caller who may have named neither.
            logger.info(
                "an operation authority did not resolve to a live lease (%s)",
                type(Exception).__name__,
                exc_info=False,
            )
            return None

    async def _resolve(
        self, operation_id: str, caller: Any
    ) -> ResolvedExecution | None:
        from harness_jobs.allocation import allocation_id_for
        from harness_jobs.execution_rpc import ExecutionGrant
        from harness_jobs.identity import ResolvedPrincipal
        from harness_jobs.leases import lock_lease, read_lease
        from harness_jobs.store import OperationStore

        store = self._store or OperationStore()

        principal = ResolvedPrincipal(
            org_id=caller.org_id,
            workspace_id=caller.workspace_id,
            subject=caller.subject,
            # The permission this port requires, and the only one asserted. Not
            # the caller's full permission set: `ExecutionGrant` checks
            # `may_provision` and nothing here needs a wider claim, so a wider
            # one would only widen what a later edit could rely on.
            permissions=frozenset({PROVISION_PERMISSION}),
        )
        if not principal.may_provision:
            # `may_provision` reads `REQUIRED_PERMISSION` from the harness, and
            # `PROVISION_PERMISSION` is this module's spelling of it. Checked
            # rather than assumed equal, so a divergence refuses instead of
            # constructing a grant whose permission the harness does not honour.
            return None

        async with self._connect() as connection:
            async with connection.transaction():
                record = await store.get(connection, principal, operation_id)
                if record is None:
                    # Absent, or another tenant's. One answer for both, in SQL.
                    return None
                lease = await read_lease(connection, operation_id=operation_id)
                if lease is None:
                    return None
                if not await lock_lease(connection, lease):
                    # Closed, lapsed, taken over, or held at a different token
                    # than the row now carries. The database decides, against its
                    # own clock, inside this transaction.
                    return None
                allocation_id = allocation_id_for(record)

        try:
            grant = ExecutionGrant(principal=principal, lease=lease)
        except Exception:
            # The identity comparison `ExecutionGrant` makes independently. A
            # caller who is not the holder lands here.
            return None

        if len(run_id_for(lease)) > MAX_IDENTIFIER_LENGTH:
            # The derived run identity would not survive the column that persists
            # it. Refused rather than truncated: a truncated run id compares equal
            # to a different run's.
            return None
        return ResolvedExecution(
            grant=grant, record=record, allocation_id=allocation_id
        )


def _reference(authority: object) -> str | None:
    """The operation this authority names, or `None` if it names nothing usable.

    Shape-checked only. No decoding, no signature, no expiry: the string carries
    no authority to verify, so there is nothing here that could be got wrong in a
    way that granted something. What makes the reference safe is the two
    independent facts the caller must also satisfy, not this function.
    """
    if not isinstance(authority, str):
        return None
    value = authority.strip()
    if not value or len(value) > MAX_IDENTIFIER_LENGTH:
        return None
    if "\x00" in value:
        return None
    return value
