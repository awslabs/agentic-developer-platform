"""The durable operation store: admit, read, and transition.

Issue #5525 (w6-02), EPIC #4910, Wave 6.

## The one property this module exists for

**Admission and enqueue commit together, or neither happens.** `admit()` writes the
operation row and its outbox row inside one transaction against one database. The
commit is the durability point: before it, a crash leaves nothing at all; after it,
the operation exists and will be delivered. There is no reachable state where an
operation was accepted but has no outbox row, or has an outbox row but no admission.

That is also the whole of the no-distributed-transactions requirement (#5524 §3.1).
There is exactly one participant, so there is nothing to coordinate -- no two-phase
protocol, no saga, no compensating-transaction coordinator. The steps that *do* cross
a process boundary (the budget reserve/confirm hooks before admission, and delivery
after it) are outside this transaction and are made safe by idempotency and
reconciliation instead. Do not add a coordinator.

## Why retries go through the constraint rather than a pre-check

`admit()` attempts the INSERT and interprets the unique violation. It does not first
SELECT to see whether the key is taken. A pre-check has a window -- two concurrent
identical requests both find nothing and both insert -- and the constraint does not.
So the conflict *is* the detection, which is the pattern `record_handle`
(`services/provider_handles.py:283-342`) already establishes in the domain app.

On conflict there are exactly two answers, and collapsing them would be a defect:

* **same key, same payload digest** -> return the existing operation, with
  `created=False`. The retry landed; it did not write twice.
* **same key, different payload digest** -> refuse. This is the case that matters:
  a caller resubmitting under a key that already passed approval, with a larger
  envelope. Silently returning the original would tell the caller its *new* request
  was accepted, which is worse than an error.

## What this module deliberately does not do

* **No lease, fence or cancellation logic.** #5527 (w6-04). `attempt_id` and the
  `version` column are stored so that story has identity to fence, but nothing here
  issues a lease.
* **No approval check.** #5526 (w6-03). `admit()` requires a principal that already
  carries the permission; whether an *approval* is current, unexpired and unconsumed
  is a separate authority this store does not adjudicate.
* **No connection management.** Every function takes a connection or a pool handed
  in. This module reads no DSN and holds no credential, so it cannot be the place a
  database URL leaks from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .identity import (
    ContractViolation,
    OperationBinding,
    OperationRefused,
    OperationRequest,
    OperationState,
    ResolvedPrincipal,
    decode_payload,
    payload_digest,
)
from .schema import check_schema_version

__all__ = [
    "AdmittedOperation",
    "ConcurrentUpdate",
    "OperationRecord",
    "OperationRefused",
    "OperationStore",
]

# PostgreSQL's unique-violation SQLSTATE. Matched on the code rather than on the
# exception's class name or message: asyncpg, psycopg and SQLAlchemy each wrap it
# differently, and the message is localized and version-dependent. The code is in the
# standard and does not move.
UNIQUE_VIOLATION = "23505"


class ConcurrentUpdate(RuntimeError):
    """A state transition lost its optimistic-concurrency check.

    Raised when an UPDATE matched no row because the `version` moved under the
    writer. Recoverable by re-reading and deciding again -- which is why it is
    distinct from `OperationRefused`: the caller was allowed to do this, it just
    needs a fresh read. Collapsing the two would make a benign race look like a
    permission problem.
    """


@dataclass(frozen=True)
class OperationRecord:
    """One operation as the store holds it.

    Frozen. A mutable record is what a caller reads instead of asking the store, and
    "the caller acted on a status that had already changed" is a failure mode this
    package is built to prevent. To change state, call `transition()` and use its
    return value.
    """

    operation_id: str
    attempt_id: str
    job_id: str
    org_id: str
    workspace_id: str
    action: str
    idempotency_key: str
    plan_digest: str
    request_payload: str
    contract_version: str
    state: OperationState
    version: int
    detail: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_terminal(self) -> bool:
        """True when the store will report no further change."""
        from .identity import TERMINAL_STATES

        return self.state in TERMINAL_STATES

    def admitted_request(self) -> OperationRequest:
        """The exact request this operation was admitted for.

        This is the method that makes the durability guarantee usable rather than
        merely true: a dispatcher recovering after a restart calls it to obtain the
        request it has to perform. Before the payload was stored, a recovering process
        could identify an operation and could not execute it, which left every accepted
        request unexecutable across exactly the crash the outbox exists to survive.

        Re-validated and re-verified on the way out -- see `_record`. Raises
        `ContractViolation` if the stored payload does not decode, or does not agree
        with the digest committed beside it.
        """
        return decode_payload(self.request_payload)


@dataclass(frozen=True)
class AdmittedOperation:
    """The result of `admit()`.

    ``created`` distinguishes "this call admitted it" from "an identical request had
    already been admitted -- the retry landed, it did not write twice". Carried
    explicitly because the two are indistinguishable from the record alone, and a
    caller that cannot tell them apart cannot report accurately either. Same
    distinction `conclude_operation` draws with its `applied` flag
    (`services/provider_handles.py:499-501`).
    """

    record: OperationRecord
    created: bool


class Connection(Protocol):
    """The slice of a database connection this store uses.

    Narrow and driver-agnostic on purpose: the store is handed an open connection and
    cannot open one, so it is not the place a DSN is read from the environment.
    """

    async def execute(self, query: str, *args: object) -> object: ...
    async def fetchrow(self, query: str, *args: object) -> object: ...
    async def fetch(self, query: str, *args: object) -> object: ...
    async def fetchval(self, query: str, *args: object) -> object: ...

    def transaction(self) -> object: ...

    def is_in_transaction(self) -> bool: ...


_COLUMNS = """
    operation_id, attempt_id, job_id, org_id, workspace_id, action, idempotency_key,
    plan_digest, request_payload, contract_version, state, version, detail,
    created_at, updated_at
"""


def _record(row: object) -> OperationRecord:
    """Build a record from a driver row, verifying the payload against its digest.

    Indexed by column name via mapping access, which every driver row supports; not
    by position, because a column added to `_COLUMNS` in the middle would then
    silently shift every field after it.

    The digest check is the reason both columns exist. `plan_digest` is written once at
    admission and never updated, so it is a witness for `request_payload` rather than a
    duplicate of it: if the two disagree, the payload is not what was admitted, and the
    only safe answer is to refuse rather than to hand a dispatcher a request nobody
    approved. That covers a row altered in place -- by an operator, a bad migration, or
    a future writer that updates one column and not the other -- which is precisely the
    case "we stored the payload, so we can reconstruct it" would otherwise trust
    blindly.

    Checked on every read rather than only before dispatch, because a read is where the
    value enters this process; a check at the dispatch boundary would leave every other
    caller trusting it.
    """
    data = dict(row)  # type: ignore[call-overload]
    stored_payload = data["request_payload"]
    # Decodes and re-validates; raises ContractViolation for a payload this module did
    # not write.
    decoded = decode_payload(stored_payload)
    if payload_digest(decoded) != data["plan_digest"]:
        raise ContractViolation(
            f"the stored request payload for operation {data['operation_id']!r} does "
            "not match the plan digest committed with it; the admitted request cannot "
            "be reconstructed and must not be executed"
        )
    return OperationRecord(
        operation_id=data["operation_id"],
        attempt_id=data["attempt_id"],
        job_id=data["job_id"],
        org_id=data["org_id"],
        workspace_id=data["workspace_id"],
        action=data["action"],
        idempotency_key=data["idempotency_key"],
        plan_digest=data["plan_digest"],
        request_payload=stored_payload,
        contract_version=data["contract_version"],
        state=OperationState(data["state"]),
        version=data["version"],
        detail=data["detail"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


def _is_unique_violation(error: BaseException) -> bool:
    """Whether an exception is a unique-constraint violation.

    Checks `sqlstate`, then `pgcode`, then the DBAPI original's `sqlstate` -- the
    three places the drivers in use put it. Returns False for anything else so a
    genuine failure (a dropped connection, a disk error) is never mistaken for a
    duplicate and answered with a stale record.
    """
    for attribute in ("sqlstate", "pgcode"):
        if getattr(error, attribute, None) == UNIQUE_VIOLATION:
            return True
    original = getattr(error, "orig", None)
    if original is not None and original is not error:
        return _is_unique_violation(original)
    return False


def stored_outcome(value: object) -> str:
    """The `CallOutcome` value from a stored `harness_provider_call_intent.outcome`.

    `execution._outcome_detail` writes one column for both the enum and the provider's
    free text, as `"<enum>: <detail>"` when a detail was given -- deliberately, so the
    machine-readable and human-readable halves cannot disagree about the same call. The
    contract that makes it safe is that the enum comes first, so a prefix match decides
    it. Code branching on the outcome must therefore use this function and never compare
    the raw column, because an equality test against `"succeeded"` silently stops
    matching the moment a provider returns any detail text at all.

    This lives in `store` rather than in `execution` because the three readers that
    branch on it (`execution_plan.confirmed_plan_progress`,
    `inventory._creation_complete`, `allocation.creating_calls_unaccounted_for`) all
    already import this module, while `execution_plan` cannot import `execution` without
    a cycle (`execution` -> `leases`/`recovery` -> `execution_plan`). A single decoder
    shared by the writer's peers is what keeps the "enum first" contract from being
    re-derived, inconsistently, at each call site -- which is exactly how the defect
    this function fixes arose: `allocation` split the prefix and the other two did not.

    Returns `""` for NULL or a non-string, so a missing outcome is falsy and never
    compares equal to a real one.
    """
    if not isinstance(value, str):
        return ""
    return value.split(":", 1)[0]


class OperationStore:
    """Durable create/get/status for operations, plus the admission transaction.

    Holds no connection of its own: every method takes one. That is what lets
    `admit()` be called inside a caller's wider transaction if it has one, and it
    keeps this class free of pool lifecycle -- which is the thing that otherwise
    grows into a second composition root.
    """

    async def ensure_compatible(self, connection: Connection) -> None:
        """Refuse to operate against a schema this code was not written for.

        Called once at startup by whatever composes this store. Not called
        per-operation: that would be a round trip on every request to re-answer a
        question whose answer cannot change without a deployment.
        """
        await check_schema_version(connection)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Admission
    # ------------------------------------------------------------------

    async def admit(
        self,
        connection: Connection,
        principal: ResolvedPrincipal,
        request: OperationRequest,
        *,
        operation_id: str | None = None,
        attempt_id: str | None = None,
        job_id: str | None = None,
    ) -> AdmittedOperation:
        """Admit one operation and enqueue its dispatch, atomically.

        Returns the admitted (or already-admitted) operation. Raises
        `OperationRefused` when the principal lacks the permission, or when the same
        idempotency key is reused with a changed payload.

        The transaction spans exactly the two INSERTs. `identity.py` has already
        validated and bounded the request by the time it is constructed, so no
        validation happens inside the transaction -- holding a transaction open
        across work that can be done outside it is how a store's write throughput
        becomes a caller's input-validation cost.
        """
        binding = OperationBinding.issue(
            principal,
            request,
            operation_id=operation_id,
            attempt_id=attempt_id,
            job_id=job_id,
        )
        try:
            async with connection.transaction():  # type: ignore[attr-defined]
                row = await connection.fetchrow(
                    f"""
                    INSERT INTO harness_operations (
                        operation_id, attempt_id, job_id, org_id, workspace_id, action,
                        idempotency_key, plan_digest, request_payload,
                        contract_version, state
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    RETURNING {_COLUMNS}
                    """,
                    binding.operation_id,
                    binding.attempt_id,
                    binding.job_id,
                    binding.org_id,
                    binding.workspace_id,
                    binding.action,
                    binding.idempotency_key,
                    binding.plan_digest,
                    binding.request_payload,
                    binding.contract_version,
                    OperationState.PENDING.value,
                )
                # (3b) in #5524 §3.1, in the same transaction as (3a) above. If this
                # statement or the commit fails, the operation row goes with it --
                # which is the property that makes "admitted but never dispatched"
                # unreachable rather than merely unlikely.
                #
                # The payload and job/attempt identity are written here too, not looked
                # up at delivery time: a worker is handed a description, and a worker
                # that had to read `harness_operations` would need read access to every
                # operation's full record.
                await connection.execute(
                    """
                    INSERT INTO harness_dispatch_outbox (
                        operation_id, org_id, workspace_id, job_id, attempt_id,
                        action, request_payload
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                    """,
                    binding.operation_id,
                    binding.org_id,
                    binding.workspace_id,
                    binding.job_id,
                    binding.attempt_id,
                    binding.action,
                    binding.request_payload,
                )
        except Exception as error:
            if not _is_unique_violation(error):
                # Not a duplicate: a real failure. Propagated unchanged, because
                # answering a dropped connection with a stale record would report an
                # operation as admitted when nothing was committed.
                raise
            return await self._resolve_conflict(connection, binding)
        return AdmittedOperation(record=_record(row), created=True)

    async def _resolve_conflict(
        self, connection: Connection, binding: OperationBinding
    ) -> AdmittedOperation:
        """Decide what a unique violation on admission means.

        Reached only after the constraint fired, so the existing row is committed and
        this read cannot race the writer that created it.
        """
        row = await connection.fetchrow(
            f"""
            SELECT {_COLUMNS} FROM harness_operations
            WHERE org_id = $1 AND workspace_id = $2 AND idempotency_key = $3
            """,
            binding.org_id,
            binding.workspace_id,
            binding.idempotency_key,
        )
        if row is None:
            # The constraint fired but the row is not visible under this tenant. The
            # remaining ways to get here are a collision on the `operation_id`
            # primary key or on the `job_id` unique constraint -- in either case a
            # caller-supplied identifier already belonging to some other operation,
            # possibly another tenant's. Refused without saying which, and without
            # disclosing that it exists: a distinguishable answer is itself the
            # cross-tenant disclosure, and naming the colliding column would let a
            # caller probe for live job ids one guess at a time.
            raise OperationRefused(
                "the operation could not be admitted under the resolved tenant"
            )
        existing = _record(row)
        if existing.plan_digest != binding.plan_digest:
            # The case this check exists for. Refused rather than honoured, and
            # refused rather than silently answered with the original: the caller
            # asked for something different and must learn that it was not accepted.
            raise OperationRefused(
                f"idempotency key {binding.idempotency_key!r} was already admitted "
                "with a different payload; a retry may not change the request. "
                "Use a new idempotency key for a different operation."
            )
        if existing.action != binding.action:
            # Defence in depth: the digest covers `action`, so a differing action
            # should already have been caught above. Kept because the two fields are
            # checked for different reasons -- the digest protects the payload, this
            # protects against a future digest change that stops covering the action
            # -- and a teardown answered with a provision is destructive.
            raise OperationRefused(
                "idempotency key was already admitted for a different action"
            )
        return AdmittedOperation(record=existing, created=False)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get(
        self,
        connection: Connection,
        principal: ResolvedPrincipal,
        operation_id: str,
    ) -> OperationRecord | None:
        """One operation, or None.

        Tenant-scoped **in the WHERE clause**, not by filtering after the read. An
        operation belonging to another tenant returns None -- the same answer as one
        that does not exist -- because a distinguishable "exists but forbidden" reply
        confirms the existence of another tenant's operation to a caller who should
        not learn it.
        """
        row = await connection.fetchrow(
            f"""
            SELECT {_COLUMNS} FROM harness_operations
            WHERE operation_id = $1 AND org_id = $2 AND workspace_id = $3
            """,
            operation_id,
            principal.org_id,
            principal.workspace_id,
        )
        return None if row is None else _record(row)

    async def get_by_idempotency_key(
        self,
        connection: Connection,
        principal: ResolvedPrincipal,
        idempotency_key: str,
    ) -> OperationRecord | None:
        """The operation admitted under a key, or None. Tenant-scoped as `get`.

        Lets a caller that lost its response find out what happened without
        re-admitting -- the read half of idempotency. Without it, a caller whose
        response was lost has to retry the write to learn the outcome.
        """
        row = await connection.fetchrow(
            f"""
            SELECT {_COLUMNS} FROM harness_operations
            WHERE org_id = $1 AND workspace_id = $2 AND idempotency_key = $3
            """,
            principal.org_id,
            principal.workspace_id,
            idempotency_key,
        )
        return None if row is None else _record(row)

    async def list_for_tenant(
        self,
        connection: Connection,
        principal: ResolvedPrincipal,
        *,
        limit: int = 50,
    ) -> tuple[OperationRecord, ...]:
        """Recent operations for the principal's tenant, newest first.

        ``limit`` is clamped rather than trusted: an unbounded list is a way to turn
        one request into an arbitrarily expensive read, and clamping is a smaller
        surprise than refusing a large value.
        """
        bounded = max(1, min(int(limit), 200))
        rows = await connection.fetch(
            f"""
            SELECT {_COLUMNS} FROM harness_operations
            WHERE org_id = $1 AND workspace_id = $2
            ORDER BY created_at DESC, operation_id DESC
            LIMIT $3
            """,
            principal.org_id,
            principal.workspace_id,
            bounded,
        )
        return tuple(_record(row) for row in rows)  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    async def transition(
        self,
        connection: Connection,
        operation_id: str,
        *,
        expected_version: int,
        state: OperationState,
        detail: str | None = None,
    ) -> OperationRecord:
        """Move an operation to ``state``, if it is still at ``expected_version``.

        Raises `ConcurrentUpdate` when the version moved -- the caller must re-read
        and decide again. Version-checked rather than last-write-wins because a stale
        `running` overwriting a terminal `succeeded` is how a finished operation gets
        retried, and for these operations a retry is duplicated cloud spend.

        Not tenant-scoped, deliberately: the callers are the delivery loop and the
        executor-report path, which act under the operation's own resolved identity
        rather than a request principal. Whether a *reporter* is entitled to speak
        for an operation is the report-authority question, and that is #5529's
        (w6-06) -- this method is not a substitute for it and must not be exposed
        directly to a caller-supplied principal.
        """
        if not isinstance(state, OperationState):
            raise ContractViolation("state must be an OperationState")
        row = await connection.fetchrow(
            f"""
            UPDATE harness_operations
               SET state = $1,
                   detail = $2,
                   version = version + 1,
                   updated_at = now()
             WHERE operation_id = $3 AND version = $4
            RETURNING {_COLUMNS}
            """,
            state.value,
            detail,
            operation_id,
            expected_version,
        )
        if row is None:
            # Zero rows means either the operation is gone or its version moved. Both
            # are resolved the same way -- re-read -- so they are not distinguished
            # here, and doing so would need a second query whose answer could already
            # be stale by the time it returned.
            raise ConcurrentUpdate(
                f"operation {operation_id} is not at version {expected_version}; "
                "re-read before transitioning"
            )
        return _record(row)


def request_digest(request: OperationRequest) -> str:
    """Re-exported so a caller can compute a digest without importing `identity`.

    Present because the API layer needs it to answer "is this the same request?"
    before calling `admit`, and reaching past the store into `identity` for one
    function is how a boundary starts leaking.
    """
    return payload_digest(request)
