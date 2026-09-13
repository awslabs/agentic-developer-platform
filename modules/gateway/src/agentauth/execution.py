"""Live execution state: is the run behind this credential still the real one? (#5028)

## Why a verified credential is not enough

:mod:`src.agentauth.run_credential` proves two things: a trusted path minted this
credential, and it has not expired. Neither is liveness. Between minting and use,
all of the following can happen without changing a single byte of the token:

- **The attempt was superseded.** Attempt 1's pod is replaced by attempt 2. The
  attempt-1 credential still verifies, and still names attempt 1, for the rest of
  its TTL. A signature cannot know it is stale.
- **The credential epoch was rotated.** Renewal (AC6) issues epoch N+1; the
  epoch-N token is still cryptographically valid.
- **The flow was cancelled or the execution aborted.** Revocation is an event in
  the store, not a property of a token.
- **The credential leaked to a different workload.** The token says which
  invocation it was issued *for*; it cannot say which pod is *presenting* it.

So the policy must compare the credential against current protected state. This
module is the pure half of that comparison: a record type and a total function
over it. The DynamoDB backend lives in :mod:`src.agentauth.store`, so every
refusal below is reachable in a unit test without AWS.

## Fail closed on absence

An execution the protected store has never heard of is refused, not admitted.
This is the opposite of the usual "no record, no restriction" default and it is
deliberate: the store is the authority on which executions exist, so a missing
record means either the credential names a run that was never dispatched, or the
store is unavailable. Both must refuse. Defaulting to "allow when we cannot
check" would make an outage in the authority store an authorization bypass.

## Bounded rotation overlap, not open-ended tolerance

Renewal needs two epochs valid at once — briefly, or a worker mid-request when
its credential rotates would be refused. That window is expressed as explicit
state (``min_acceptable_credential_epoch`` plus ``epoch_overlap_expires_at``)
rather than as arithmetic like ``epoch >= current - 1``. An arithmetic tolerance
is permanent and unauditable: it accepts the previous epoch forever. Explicit
state means the overlap has a deadline, and after that deadline only the current
epoch verifies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ExecutionStatus(StrEnum):
    """Lifecycle of a dispatched execution, as the protected store records it.

    Only :data:`ACTIVE` may act. The terminal states are distinguished from each
    other for audit rather than for authorization — an operator investigating a
    refusal needs to know whether the run finished normally or was cancelled.
    """

    # Dispatch has written the record but no pod has claimed it yet. Cannot act:
    # a credential presented against a PENDING record means bootstrap has not
    # completed, so nothing has been bound and nothing should be authorized.
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    # The delegation behind the execution was revoked while it ran.
    REVOKED = "revoked"


# The states from which no action is authorized. Written as the complement of
# ACTIVE rather than as a list, so a status added later is refused by default
# instead of silently becoming actionable.
_ACTIONABLE_STATUSES: frozenset[ExecutionStatus] = frozenset({ExecutionStatus.ACTIVE})


class ExecutionStateError(Exception):
    """Live execution state refused the caller.

    ``reason`` is for the audit record, never for the caller: the policy collapses
    every one of these to the same opaque refusal. A caller able to distinguish
    "superseded attempt" from "no such execution" learns whether a run it named
    exists and how many times it has been retried.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ExecutionRecord:
    """Authoritative state for one invocation, from the protected store.

    Frozen, and constructed only by the store: this object is an authorization
    input, so code that could mutate it after the read would have reintroduced
    the tampering the protected table exists to prevent.

    ``current_attempt`` is the store's opinion, which is what makes supersession
    detectable. The credential carries the attempt it was *issued* for; the
    difference between the two is the whole check.
    """

    invocation_id: str
    tenant_id: str
    current_attempt: int
    status: ExecutionStatus
    # Highest credential epoch dispatch/renewal has issued for this execution.
    current_credential_epoch: int
    # Lowest epoch still accepted. Equal to ``current_credential_epoch`` outside a
    # rotation; one lower during the overlap window, which is what lets a request
    # in flight when rotation happens still complete.
    min_acceptable_credential_epoch: int
    # Deadline for the overlap above. Past it, only the current epoch verifies.
    # None means no overlap is open.
    epoch_overlap_expires_at: datetime | None = None
    # The workload this execution was immutably bound to at bootstrap (e.g. a pod
    # UID). None until bootstrap binds it; a bound execution refuses a caller
    # presenting a different binding. See Decision 7 in
    # docs/design/agent-delegated-authority.md — the trusted bootstrap that
    # establishes this value is NOT implemented yet, so today this stays None and
    # the check below is inert rather than absent. Inert-but-present matters: the
    # comparison is written and tested now, so enabling bootstrap does not also
    # require getting the comparison right for the first time.
    workload_binding: str | None = None
    flow_id: str | None = None
    repo: str | None = None
    # The events-table sort key captured by trusted dispatch. Authoritative here
    # for the same reason lineage is: the events row is keyed ``(event_id,
    # arrived_at)``, so a service writing "the caller's own row" needs a sort key
    # the caller did not choose. Deriving it instead by querying ``event_id`` for
    # the newest row would let a second row under one ``event_id`` decide which
    # row is written — see :mod:`src.agentauth.registration`.
    arrived_at: str | None = None
    # The execution identity ("<invocation_id>#<attempt>") that dispatched this
    # one, as recorded by trusted dispatch. None for a root execution.
    #
    # This lives on the protected table rather than being read from the
    # webhook-events row's ``parent_invocation_id`` for one decisive reason: the
    # worker role can write that row today (``DynamoDBWebhookEventsUpdate``). A
    # worker that could edit its own parent pointer could promote an unrelated run
    # into a "descendant" of itself and thereby authorize control over it. Lineage
    # is an authorization input, so it must live where its subject cannot write it.
    parent_principal: str | None = None

    @property
    def principal(self) -> str:
        """The execution identity the store considers current."""
        return f"{self.invocation_id}#{self.current_attempt}"


def evaluate_execution_state(
    *,
    record: ExecutionRecord | None,
    invocation_id: str,
    attempt: int,
    tenant_id: str,
    credential_epoch: int,
    now: datetime,
    presented_workload_binding: str | None = None,
) -> ExecutionRecord:
    """Check a verified credential against current protected state.

    Returns the record on success so the caller uses the *store's* view of tenant
    and flow rather than the credential's. Raises :class:`ExecutionStateError`
    otherwise.

    Check order is chosen so that no later check can be reached with a record
    that a earlier one should have rejected:

    1. **Existence** — fail closed on absence.
    2. **Identity agreement** — the record must be the one this credential names.
    3. **Tenant** — before status, so a cross-tenant probe cannot distinguish a
       cancelled run from a running one by its refusal.
    4. **Status** — only ``ACTIVE`` acts.
    5. **Attempt** — a credential for an older attempt is superseded.
    6. **Credential epoch** — within the current bounded overlap.
    7. **Workload binding** — the presenting workload must be the bound one.
    """
    if record is None:
        raise ExecutionStateError("execution_not_found")

    # The store is queried by invocation ID, so a mismatch here is a store bug or
    # a tampered read rather than an attack the caller can mount. Checked anyway:
    # "the lookup returned something" and "it returned the thing I asked for" are
    # different statements, and only the second may be relied on.
    if record.invocation_id != invocation_id:
        raise ExecutionStateError("execution_identity_mismatch")

    if not tenant_id or record.tenant_id != tenant_id:
        raise ExecutionStateError("execution_tenant_mismatch")

    if record.status not in _ACTIONABLE_STATUSES:
        # The specific status reaches the audit record so an operator can tell a
        # cancelled flow from a finished one; the caller still sees one refusal.
        raise ExecutionStateError(f"execution_not_active:{record.status.value}")

    floor = record.min_acceptable_credential_epoch
    if (
        type(record.current_attempt) is not int
        or record.current_attempt < 1
        or type(record.current_credential_epoch) is not int
        or record.current_credential_epoch < 1
        or type(floor) is not int
        or not 1 <= floor <= record.current_credential_epoch
    ):
        raise ExecutionStateError("invalid_execution_version")

    # A newer attempt has taken over. The old pod may still be running and its
    # credential may still be unexpired — that is exactly the case this catches.
    if attempt < record.current_attempt:
        raise ExecutionStateError("execution_attempt_superseded")
    # An attempt ahead of the store's view cannot be legitimate: attempts are
    # created by trusted dispatch, which writes the record before the pod runs.
    if attempt > record.current_attempt:
        raise ExecutionStateError("execution_attempt_unknown")

    if record.epoch_overlap_expires_at is None or now >= record.epoch_overlap_expires_at:
        # The overlap window closed. Only the current epoch is acceptable now,
        # regardless of what the floor still says — an expired overlap that kept
        # honouring the old floor would be an unbounded tolerance. An absent
        # deadline cannot establish an open window either.
        floor = record.current_credential_epoch
    if credential_epoch < floor:
        raise ExecutionStateError("credential_epoch_superseded")
    if credential_epoch > record.current_credential_epoch:
        # Above what was ever issued. Not reachable with a genuinely minted
        # credential, so this indicates a forged epoch or a store rollback.
        raise ExecutionStateError("credential_epoch_unknown")

    if record.workload_binding is not None and presented_workload_binding != record.workload_binding:
        raise ExecutionStateError("workload_binding_mismatch")

    return record
