"""Resolve the actual protected ADP run and its current original operation lease.

A lease fence is not a run identity. Gateway verifies the paid producer mapping,
current pod, run and lease; this adapter independently checks the shared store.
An operation reference alone grants nothing, and an unauthenticated probe performs
no network or database access.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.adapters.operation_authority_source import (
    ActingPrincipal,
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

    __slots__ = ("grant", "record", "allocation_id", "run_id", "not_after")

    def __init__(
        self, grant: Any, record: Any, allocation_id: str, run_id: str, not_after: Any
    ) -> None:
        self.grant = grant
        self.record = record
        self.allocation_id = allocation_id
        self.run_id = run_id
        self.not_after = not_after

    @property
    def lease(self) -> Any:
        return self.grant.lease

    @property
    def principal(self) -> Any:
        return self.grant.principal


class HarnessExecutionAuthority:
    """Turns an operation-authority reference into an `ExecutionGrant`.

    Composed with the harness connection seam and an `OperationStore`. Holds no
    connection: `app/adapters/harness_connection.py` owns the pool, and this class
    acquires per call so a long-lived adapter never pins one.
    """

    def __init__(
        self, connect: Any, store: Any = None, *, verify_run: Any = None
    ) -> None:
        self._connect = connect
        self._store = store
        self._verify_run = verify_run

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
            # The unauthenticated capability probe opens no database or transport.
            return None
        try:
            return await self._resolve(operation_id, caller)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.info(
                "an operation authority did not resolve to a live lease", exc_info=False
            )
            return None

    async def resolve_submitter(
        self, authority: object, *, submitter: Any, workspace: str
    ) -> ResolvedExecution | None:
        """Resolve a credential-authenticated machine without a user contextvar.

        The credential grants an exact workspace; its organization comes only
        from the registered workspace row. Gateway must still authenticate that
        submitter as the actual live run before any execution grant is built.
        """
        from superplane_contracts import Submitter

        operation_id = _reference(authority)
        if (
            operation_id is None
            or not isinstance(submitter, Submitter)
            or not isinstance(workspace, str)
            or workspace not in submitter.workspaces
            or self._verify_run is None
        ):
            return None
        try:
            async with self._connect() as connection:
                org_id = await connection.fetchval(
                    "SELECT org_id::text FROM workspaces WHERE id::text=$1", workspace
                )
            if org_id is None:
                return None
            caller = ActingPrincipal(
                subject=submitter.submitter_id,
                org_id=org_id,
                workspace_id=workspace,
                account_type="agent",
            )
            return await self._resolve(operation_id, caller)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.info("a submitter operation authority was refused", exc_info=False)
            return None

    async def _resolve(
        self, operation_id: str, caller: Any
    ) -> ResolvedExecution | None:
        from harness_jobs.allocation import allocation_id_for
        from harness_jobs.execution_rpc import ExecutionGrant
        from harness_jobs.identity import ResolvedPrincipal
        from harness_jobs.leases import lock_lease, read_lease
        from harness_jobs.store import OperationStore

        from datetime import UTC, datetime

        if self._verify_run is None or not caller.workspace_id:
            return None
        verified = await self._verify_run(
            operation_id=operation_id,
            org_id=caller.org_id,
            workspace_id=caller.workspace_id,
            subject=caller.subject,
        )
        if not isinstance(verified, dict):
            return None
        if any(
            verified.get(key) != value
            for key, value in {
                "operation_id": operation_id,
                "org_id": caller.org_id,
                "workspace_id": caller.workspace_id,
                "subject": caller.subject,
                "domain_org_id": caller.org_id,
                "holder": caller.subject,
            }.items()
        ):
            return None
        run_id = verified.get("invocation_id")
        if (
            not isinstance(run_id, str)
            or not run_id
            or len(run_id) > MAX_IDENTIFIER_LENGTH
            or caller.subject != run_id + "#1"
            or verified.get("version") != 1
            or PROVISION_PERMISSION not in verified.get("permissions", ())
        ):
            return None
        deadline = datetime.fromisoformat(verified["not_after"])
        if deadline.tzinfo is None or deadline <= datetime.now(UTC):
            return None
        store = self._store or OperationStore()
        principal = ResolvedPrincipal(
            org_id=caller.org_id,
            workspace_id=caller.workspace_id,
            subject=verified["subject"],
            permissions=frozenset(verified["permissions"]),
        )

        async with self._connect() as connection:
            async with connection.transaction():
                record = await store.get(connection, principal, operation_id)
                if record is None:
                    # Absent, or another tenant's. One answer for both, in SQL.
                    return None
                lease = await read_lease(connection, operation_id=operation_id)
                if lease is None:
                    return None
                if (
                    any(
                        getattr(lease, key) != verified.get(key)
                        for key in (
                            "operation_id",
                            "org_id",
                            "workspace_id",
                            "holder",
                            "attempt_id",
                            "fence_token",
                        )
                    )
                    or record.job_id != verified.get("job_id")
                    or record.attempt_id != verified.get("admission_attempt_id")
                    or record.plan_digest != verified.get("plan_digest")
                ):
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

        return ResolvedExecution(
            grant=grant,
            record=record,
            allocation_id=allocation_id,
            run_id=run_id,
            not_after=deadline,
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
