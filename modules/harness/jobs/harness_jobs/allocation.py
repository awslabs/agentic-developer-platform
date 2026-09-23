"""The allocation claim: who may create into an allocation, and when.

Issue #5529 (w6-06), EPIC #4910, Wave 6.

This module exists because of an import direction. `inventory` owns the allocation's
membership, its seal and the cleanup verdict, and it imports `execution` for the budget
disposition and the audit writer. So `execution` cannot import `inventory` -- and
`execution` is the module that actually contacts the provider, which is the only place
where refusing still prevents spend.

Everything both sides need to agree about therefore lives here, below both of them:

* how an allocation is named and serialized (`allocation_id_for`, `lock_allocation`);
* whether it is closed (`sealed_revision`);
* whether a planned call can CREATE something (`call_effect`);
* whether any creating call recorded against the allocation is still unaccounted for
  (`creating_calls_unaccounted_for`).

Nothing here raises a domain refusal. Each function answers a question and the caller
decides what its own refusal is called, because `inventory` refuses with
`OperationRefused` and `execution` refuses with `ProviderCallRefused`, and a shared
module that picked one of them would make the other's callers catch the wrong type.

## The invariant the two callers enforce together

Sealing an allocation used to withdraw only the ability to RECORD membership, never the
authority to create. Two separately approved operations can name one allocation, so
after operation A sealed, operation B could still record an intent and invoke the
provider; B's membership write was then refused by the seal. The result was a created,
billing resource that no inventory named, while A's sealed membership and its ABSENT
report authorized the release of the budget that would have paid for it. Every single
check passed, and the money was gone in the one direction this package exists to
prevent.

One rule on each side, both taken under `lock_allocation`, close it:

1. **A creating call may not be recorded or dispatched into a sealed allocation**
   (`execution.record_intent`, `OperationExecutor._record`,
   `OperationExecutor._execute_provider`). Checked again immediately before the provider
   hook runs, because that is the last moment at which refusing costs nothing.
2. **An allocation may not be sealed while a creating call recorded against it is
   unaccounted for** (`inventory.seal_allocation`, re-derived by
   `inventory._completeness`). Unaccounted means the call may have happened and nobody
   knows, or it succeeded and produced a handle membership does not name.

Rule 1 alone leaves the window where B's intent is committed and its provider call is in
flight: A would seal membership that cannot include what B is creating. Rule 2 closes
that window using B's own durable intent row rather than by holding a lock across
provider I/O -- a lock held across a call that may take minutes would make sealing block
on an unrelated provider's latency, and a lock is lost on a crash while the intent row
is not.

So whichever order the two attempt: if the intent commits first, the seal is refused
until that call is settled AND its handle is in membership; if the seal commits first,
the creation is refused before the provider is contacted. Both succeeding is
unreachable, and that pair of outcomes is what
`test_no_interleaving_permits_both_a_seal_and_a_later_provider_creation` asserts.
"""

from __future__ import annotations

from .effects import (
    CallEffect as CallEffect,
)
from .effects import (
    call_effect as call_effect,
)
from .effects import (
    may_create as may_create,
)
from .identity import MAX_ALLOCATION_ID_LENGTH, ContractViolation
from .store import Connection, _record

__all__ = [
    "CallEffect",
    "MAX_ALLOCATION_ID_LENGTH",
    "allocation_id_for",
    "approved_allocation",
    "bounded_text",
    "call_effect",
    "creating_calls_unaccounted_for",
    "lock_allocation",
    "sealed_revision",
]

# How many provider-call intents one allocation's accounting check will read. Bounded
# for the reason `MAX_INVENTORY_RESOURCES` is bounded: this query runs on the release
# path, and an unbounded read there is a way to make sealing expensive enough to stop
# answering. Reaching the limit is treated as "cannot account for these calls" rather
# than as "none found", so the bound fails in the retaining direction.
_MAX_ACCOUNTED_CALLS = 1024


def bounded_text(value: object, what: str, limit: int) -> str:
    """A non-blank, bounded, NUL-free string, or a refusal.

    NUL is rejected explicitly: PostgreSQL `text` cannot store it, so a value
    containing one fails at the driver with an error about encoding rather than about
    which field was wrong.

    Lives here, below both `execution` and `inventory`, because both now validate the
    same allocation identifier and two copies of a bound are two bounds.
    """
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or "\x00" in value
    ):
        raise ContractViolation(
            f"{what} must be a non-empty string of at most {limit} characters"
        )
    return value


def allocation_id_for(record: object) -> str:
    """The allocation this operation acts on, read only from the approved plan.

    From the stored, digest-bound request -- never a worker argument, for the reason
    `execution_plan.admitted_steps` reads descriptors from the plan: the approval bound
    the digest of the entire request, so a value taken from the plan is one a human
    approved. A worker-supplied allocation id would let an executor with a valid lease
    publish a valid report naming somebody else's allocation and collect cleanup
    authority over resources it was never approved to touch.

    Now that the dispatch path also asks which allocation a call creates into, that
    property matters twice: a worker that could name its own allocation could name one
    that is not sealed and create into a sealed one.
    """
    try:
        value = record.admitted_request().parameters["allocation_id"]  # type: ignore[attr-defined]
    except (AttributeError, KeyError, TypeError) as exc:
        raise ContractViolation(
            "an approved allocation id is required; the admitted plan carries none"
        ) from exc
    return bounded_text(value, "allocation_id", MAX_ALLOCATION_ID_LENGTH)


async def approved_allocation(connection: Connection, operation_id: str) -> str | None:
    """The allocation an operation is approved to act on, or `None` if it names none.

    `None` is a real answer, not a failure: most operations are not allocation-bound,
    and an operation whose plan names no allocation has no allocation whose seal could
    govern it. Such a call is also outside every inventory -- nothing can record it as
    membership, and nothing can release budget against it -- so there is no seal to
    withdraw its authority.

    `None` for a missing operation row as well, deliberately: the caller's own write
    already refuses an operation that does not exist (the intent INSERT joins
    `harness_operations`), and answering "no allocation" here keeps the refusal the
    caller reports the one about the missing operation rather than a confusing second
    one about allocations.

    Raises `ContractViolation` when the row exists and its stored payload cannot be
    reconstructed. `store._record` treats that as a request nobody approved, and a
    provider call under a plan this process cannot verify must not be made at all --
    which is stricter than defaulting to "no allocation" and is the safe direction.
    """
    row = await connection.fetchrow(
        "SELECT * FROM harness_operations WHERE operation_id=$1", operation_id
    )
    if row is None:
        return None
    record = _record(row)
    if "allocation_id" not in record.admitted_request().parameters:
        return None
    return allocation_id_for(record)


async def lock_allocation(
    connection: Connection, *, org_id: str, workspace_id: str, allocation_id: str
) -> None:
    """Serialize every write that changes what an allocation contains, or closes it.

    The operation lease lock proves the caller holds AUTHORITY; it does not order these
    writes, because an allocation is not owned by one operation. Nothing in the schema
    stops two separately approved operations in the same workspace from naming the same
    allocation, and with two operations the lease locks are two different locks -- so
    the interleaving the seal exists to prevent was available again:

        operation A: reads no seal exists, is about to insert `disk-late`
        operation B: reads membership (cluster only), commits a seal over it
        a reader:    authorizes release against the sealed cluster-only membership
        operation A: commits `disk-late`

    Every check passed in isolation. The allocation grew after a release was authorized
    over it, which is the whole defect. A lock on the operation cannot close it; the
    contention is between operations.

    So membership writes, the provider-enumeration proof, the seal AND the creation
    claim the dispatch path takes (`execution.record_intent`) all take this lock first,
    keyed by the thing actually being contended -- the allocation -- and always BEFORE
    the lease lock, so two callers taking both can never take them in opposite orders
    and deadlock. That ordering rule is why this function is here rather than beside
    either caller: one lock, one name, one documented order.

    `pg_advisory_xact_lock` over a hashed name, matching `leases.acquire`'s per-tenant
    serialization (`leases.py:532`): transaction-scoped, so it is released by commit or
    rollback without an unlock path that a raised refusal could skip. The tenant is in
    the key because an allocation id is a tenant's own value and two tenants' ids may
    collide; without it one workspace's allocation would serialize against another's.
    """
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"harness-allocation:{org_id}/{workspace_id}/{allocation_id}",
    )


async def sealed_revision(
    connection: Connection,
    *,
    org_id: str,
    workspace_id: str,
    allocation_id: str,
) -> str | None:
    """The revision this allocation was sealed over, or `None` if it is still open.

    Read by `inventory` to decide completeness and by `execution` to decide whether a
    creating call is still authorized. One query, so the two cannot drift into
    disagreeing about what "closed" means.
    """
    _, quarantined = await allocation_epoch(
        connection,
        org_id=org_id,
        workspace_id=workspace_id,
        allocation_id=allocation_id,
    )
    if quarantined:
        return "quarantined"
    return await connection.fetchval(
        "SELECT sealed_revision FROM harness_allocation_seal WHERE org_id=$1 "
        "AND workspace_id=$2 AND allocation_id=$3",
        org_id,
        workspace_id,
        allocation_id,
    )


async def creating_calls_unaccounted_for(
    connection: Connection,
    *,
    org_id: str,
    workspace_id: str,
    allocation_id: str,
    known: set[tuple[str, str]] | frozenset[tuple[str, str]],
) -> tuple[str, ...]:
    """Creating calls against this allocation that membership cannot account for.

    Rule 2 of the module docstring, and the half that makes rule 1 sufficient. Returns
    a description per offending call, so a refusal can name what is outstanding rather
    than only that something is.

    Two conditions, and they are the allocation-wide form of what `_completeness`
    already required of the reading operation's own calls:

    * **the call may have happened and nobody knows.** An `intended` row means the
      provider was about to be contacted or was contacted and the reply was never heard;
      `unresolved`, or an `unknown` outcome, means the same thing after somebody tried
      to settle it. `ProviderCall.may_have_happened` is the same predicate
      (`execution.py:248`), spelled against the stored columns so no lease-bound
      reconstruction of another attempt's row is needed.
    * **the call succeeded and produced a handle membership does not name.** This is the
      window between a provider returning a reference and the executor recording it as
      membership. Sealing inside that window produces a sealed inventory that omits a
      resource which certainly exists.

    Allocation-wide rather than operation-wide, because the seal is allocation-wide.
    Scoped by tenant as well as allocation: an allocation id is a tenant's own value.

    A call from ANY operation counts, including the caller's own -- the sealer's own
    plan is already required to be complete, so its own rows are settled, and a rule
    that excluded them would be a rule about who is asking rather than about what
    exists.

    The read is bounded (`_MAX_ACCOUNTED_CALLS`); reaching the bound is reported as an
    unaccounted call of its own, so the limit retains budget instead of quietly
    certifying an allocation nobody counted.
    """
    rows = await connection.fetch(
        """
        SELECT idempotency_key, operation_id, operation_kind, stage, outcome,
               provider, provider_ref
          FROM harness_provider_call_intent
         WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3
         ORDER BY created_at
         LIMIT $4
        """,
        org_id,
        workspace_id,
        allocation_id,
        _MAX_ACCOUNTED_CALLS,
    )
    outstanding: list[str] = []
    for row in rows:
        if not may_create(row["operation_kind"], provider=row["provider"]):
            continue
        stage = str(row["stage"])
        # execution._outcome_detail stores an optional provider explanation after
        # the enum. It must not hide a successful creation from membership checks.
        outcome = str(row["outcome"] or "").split(":", 1)[0]
        key = str(row["idempotency_key"])
        if stage in ("intended", "unresolved") or outcome == "unknown":
            outstanding.append(f"{key} ({stage}/{outcome or 'no outcome'})")
            continue
        reference = row["provider_ref"]
        if (
            outcome == "succeeded"
            and reference
            and (str(row["provider"]), str(reference)) not in known
        ):
            outstanding.append(f"{key} (created {reference}, not enumerated)")
    if len(rows) == _MAX_ACCOUNTED_CALLS:
        outstanding.append(
            f"more than {_MAX_ACCOUNTED_CALLS} recorded calls; this allocation cannot "
            "be accounted for within the read bound"
        )
    return tuple(outstanding)


async def allocation_epoch(
    connection: Connection, *, org_id: str, workspace_id: str, allocation_id: str
) -> tuple[int, bool]:
    """Durable provider-activity cutoff and unresolved contradiction state."""
    row = await connection.fetchrow(
        "SELECT generation, quarantined FROM harness_allocation_epoch "
        "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3",
        org_id,
        workspace_id,
        allocation_id,
    )
    return (int(row["generation"]), bool(row["quarantined"])) if row else (0, False)
