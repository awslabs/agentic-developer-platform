"""Outbox delivery: resumable, duplicate-safe, and blind to secrets.

Issue #5525 (w6-02), EPIC #4910, Wave 6.

## Delete-last, and why the trade is not close

A row is marked delivered **only after the delivery itself is durable**. The ordering
is `durable state written -> durable handoff sent -> row marked delivered LAST`,
which is the discipline `agent/hosting/superplane_hosting/handoff.py:43-58` already
encodes and which #5524 §3.3 names as the rule for this step.

Marking first would mean a crash in the gap loses the work permanently. Marking last
means a crash in the gap delivers the same row twice. Those are not symmetric:

* duplicate delivery is refused downstream by the uniqueness constraint the store
  already owns (`store.py`, `harness_operations_idempotent`);
* lost work is unrecoverable -- nothing remains to find it by.

So at-least-once is chosen deliberately. It converts an unsolvable loss problem into
an already-solved duplicate problem. A reader tempted to "fix" the duplicate by
marking earlier would be trading a solved problem for an unsolvable one.

## Why claims expire instead of being released

A worker that dies holding a claim must not strand its row. `claimed_until` is a
lease with a deadline, so an abandoned claim becomes claimable again by the passage
of time rather than by someone noticing. `FOR UPDATE SKIP LOCKED` is what keeps two
live workers off the same row in the first place -- and `SKIP LOCKED` rather than
plain `FOR UPDATE` because the latter makes concurrent workers queue behind each
other, which turns a fleet into one worker with extra steps.

Note what this is *not*: it is not the lease/fence model for operation execution.
That is #5527's (w6-04), it governs the executor rather than the queue, and a fence
token there protects against a stale executor acting on a provider. This claim only
prevents two dispatchers picking up the same row.

## Workers are handed a description, not a database

`DispatchEnvelope` carries the operation's identity and action. It carries no
connection, no DSN, no credential and no provider secret -- #5525 design 3. The
executor protocol takes an envelope and returns whether delivery became durable;
it cannot reach the store, so it cannot mark its own row delivered. That is why the
envelope is built from the outbox row's denormalized columns rather than by joining
to `harness_operations`: a worker that needed the join would need read access to
every operation's full record.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from .identity import TERMINAL_STATES, OperationState
from .store import ConcurrentUpdate, Connection, OperationStore

__all__ = [
    "DeliveryReport",
    "DispatchEnvelope",
    "DispatchExecutor",
    "DispatchOutbox",
]

# How long a claim is held before it may be taken by another worker. Long enough that
# a slow-but-alive delivery is not stolen mid-flight; short enough that a dead
# worker's row is retried in the same order of time as a deployment.
DEFAULT_CLAIM_SECONDS = 60

# Cap on delivery attempts before a row is left for an operator. Bounded because a
# row that fails deterministically -- a malformed action, an executor that refuses it
# -- would otherwise be retried forever at the head of the queue, starving valid
# work. Reaching the cap does NOT delete the row or conclude the operation: the
# record stays, `last_error` stays, and the operation is marked UNKNOWN rather than
# failed, because "we could not deliver it" is not evidence about what the provider
# did. Concluding it failed would be the timeout-read-as-failure defect #5524 §3.5
# names.
DEFAULT_MAX_ATTEMPTS = 10

# "Has this operation reached any conclusion at all?", asked in SQL. Used by the repair
# branch of `recover_abandoned` to find operations that are still unconcluded.
#
# Derived from the enum rather than spelled out as a literal list, because a
# hand-written copy stops agreeing with `TERMINAL_STATES` the moment a state is added --
# and the consequence of that disagreement is a repair sweep that re-concludes
# operations which are already finished. Passed as a query parameter rather than
# interpolated into the statement, so the predicate is data and not a formatting hole.
_CONCLUDED_STATE_VALUES: tuple[str, ...] = tuple(
    sorted(state.value for state in TERMINAL_STATES)
)


def _rows_affected(tag: object) -> int:
    """How many rows a driver's command tag reports.

    asyncpg's `execute` returns the PostgreSQL command tag -- `"UPDATE 1"`, `"UPDATE 0"`
    -- and the trailing count is the only portable way to learn whether a guarded
    settlement matched anything without adding a `RETURNING` clause to every statement.

    Anything unparseable returns 0, i.e. "not settled". That is the conservative
    direction on purpose: a caller that wrongly believes it settled a row stops
    retrying work it does not own, whereas one that wrongly believes it did not will
    re-examine the row and find it settled. Only the first loses work.
    """
    if isinstance(tag, int):
        return tag
    if not isinstance(tag, str):
        return 0
    parts = tag.rsplit(" ", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        return 0
    return int(parts[1])


@dataclass(frozen=True)
class DispatchEnvelope:
    """What a worker is told about one unit of work.

    Deliberately minimal, but it must be *sufficient*: an envelope a worker cannot act
    on makes the queue a list of names. So it carries the admitted request
    (``request_payload``) and the published identity the worker reports against
    (``job_id``, ``attempt_id``) alongside the routing fields.

    Still absent, and deliberately: no connection, no DSN, no credential, no provider
    handle. The payload is what the *caller* asked for, which the caller already knew.
    Credential delivery is #5528's (w6-05).

    ``claim_generation`` is the worker's proof of ownership. It is required back on
    settlement, and a stale worker holding an old generation can no longer clear,
    acknowledge or exhaust the claim that replaced it. Frozen so a worker cannot mutate
    the envelope and hand it on as a different claim.
    """

    outbox_id: int
    operation_id: str
    org_id: str
    workspace_id: str
    job_id: str
    attempt_id: str
    action: str
    attempts: int
    claim_generation: int
    request_payload: str

    def admitted_request(self) -> object:
        """The request this dispatch is for, decoded and re-validated.

        Here as well as on `OperationRecord` because a worker holds an envelope and not
        a record -- that is the whole point of the boundary -- and "reconstruct the
        admitted request" must not require reaching back into the operations table.
        """
        from .identity import decode_payload

        return decode_payload(self.request_payload)


@dataclass(frozen=True)
class DeliveryReport:
    """The outcome of one drain pass.

    Counts rather than the rows themselves: a caller polls this to decide whether to
    drain again, and returning the rows would invite acting on them outside the
    claim that protected them.
    """

    delivered: int = 0
    failed: int = 0
    exhausted: int = 0

    @property
    def handled(self) -> int:
        """Rows this pass finished with, successfully or not."""
        return self.delivered + self.failed + self.exhausted


class DispatchExecutor(Protocol):
    """Whatever carries a dispatch to the thing that will run it.

    Returns `True` only when the handoff is **durable** -- the queue write landed, the
    worker acknowledged, whatever "durable" means for that transport. Returning
    `True` eagerly is what makes delete-last meaningless, so the contract is stated
    as a return value the implementation has to be able to justify.

    Raising is permitted and is treated as "not delivered": the row stays pending and
    is retried. That is the same answer as returning `False`, so an executor that
    cannot tell the difference between a refusal and a crash is still safe here.

    A `Protocol` rather than a base class because #5527 (w6-04) owns the production
    executor; this story must not ship a second one for it to disagree with.
    """

    async def deliver(self, envelope: DispatchEnvelope) -> bool: ...


class DispatchOutbox:
    """Claims pending rows, delivers them, and marks them delivered last.

    Holds no connection: like `OperationStore`, every method takes one. A long-lived
    connection owned here would be a pool this class had to manage, and that is the
    seam where a DSN would end up being read from the environment.
    """

    def __init__(
        self,
        *,
        store: OperationStore | None = None,
        claim_seconds: int = DEFAULT_CLAIM_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._store = store or OperationStore()
        self._claim_seconds = max(1, int(claim_seconds))
        self._max_attempts = max(1, int(max_attempts))

    # ------------------------------------------------------------------
    # Claiming
    # ------------------------------------------------------------------

    async def claim(
        self, connection: Connection, *, limit: int = 10
    ) -> tuple[DispatchEnvelope, ...]:
        """Claim up to ``limit`` pending rows for this worker.

        The claim, the `attempts` increment and the `claim_generation` increment happen
        in one statement, so a worker cannot claim a row and then fail to record that it
        tried -- which would make the attempt cap unreachable and a permanently-failing
        row immortal -- and cannot hold a claim that the row does not know about.

        `SKIP LOCKED` means concurrent workers get disjoint sets instead of queueing.
        The `claimed_until IS NULL OR claimed_until < now()` predicate is what makes
        an abandoned claim recoverable without intervention.

        `abandoned_at IS NULL` excludes a row already settled by `recover_abandoned`.
        Without it, a row whose final claim was abandoned and then resolved as UNKNOWN
        would be handed out again the moment a clock passed its lease, re-delivering
        work whose operation is already terminal.
        """
        bounded = max(1, min(int(limit), 100))
        rows = await connection.fetch(
            """
            WITH claimable AS (
                SELECT id
                  FROM harness_dispatch_outbox
                 WHERE delivered_at IS NULL
                   AND abandoned_at IS NULL
                   AND (claimed_until IS NULL OR claimed_until < now())
                   AND attempts < $2
                 ORDER BY id
                 LIMIT $3
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE harness_dispatch_outbox AS o
               SET claimed_until = now() + ($1 || ' seconds')::interval,
                   attempts = o.attempts + 1,
                   claim_generation = o.claim_generation + 1
              FROM claimable
             WHERE o.id = claimable.id
            RETURNING o.id, o.operation_id, o.org_id, o.workspace_id, o.job_id,
                      o.attempt_id, o.action, o.attempts, o.claim_generation,
                      o.request_payload
            """,
            str(self._claim_seconds),
            self._max_attempts,
            bounded,
        )
        return tuple(
            DispatchEnvelope(
                outbox_id=data["id"],
                operation_id=data["operation_id"],
                org_id=data["org_id"],
                workspace_id=data["workspace_id"],
                job_id=data["job_id"],
                attempt_id=data["attempt_id"],
                action=data["action"],
                attempts=data["attempts"],
                claim_generation=data["claim_generation"],
                request_payload=data["request_payload"],
            )
            for data in (dict(row) for row in rows)  # type: ignore[union-attr]
        )

    # ------------------------------------------------------------------
    # Draining
    # ------------------------------------------------------------------

    async def drain_once(
        self,
        connection: Connection,
        executor: DispatchExecutor,
        *,
        limit: int = 10,
    ) -> DeliveryReport:
        """Claim, deliver and settle one batch. Returns what happened.

        Resumability is a property of this being callable repeatedly against the same
        table: nothing is held in memory between calls, so a process that dies
        mid-drain loses only its claims, and those expire. A caller loops on
        `handled` until it reaches zero.
        """
        envelopes = await self.claim(connection, limit=limit)
        delivered = failed = exhausted = 0
        for envelope in envelopes:
            try:
                ok = await executor.deliver(envelope)
            except asyncio.CancelledError:
                # NOT a delivery failure. A cancellation is this process being told to
                # stop, and swallowing it into "the row was not delivered" would both
                # keep a shutting-down worker running and settle a claim whose delivery
                # outcome is genuinely unknown. The claim is left to expire, which is
                # exactly what a lease is for.
                raise
            except Exception as error:  # noqa: BLE001 - any failure is "not delivered"
                # Deliberately broad. Every failure mode of an arbitrary transport --
                # refusal, timeout, programming error in the executor -- has the same
                # safe answer here: the row was not delivered, so leave it pending.
                # Narrowing this would let an unanticipated exception escape the loop
                # and abandon the rest of the claimed batch mid-pass.
                if envelope.attempts >= self._max_attempts:
                    if await self._mark_undeliverable(connection, envelope):
                        exhausted += 1
                elif await self._record_failure(connection, envelope, repr(error)):
                    failed += 1
                continue
            if ok:
                # Marked delivered only now, after the executor reported durability.
                # This is the delete-last point.
                #
                # Counted only when the marking actually landed: a claim that was
                # superseded while its delivery was in flight has not delivered this
                # row's obligation, and reporting otherwise would tell a caller the
                # queue is emptier than it is.
                if await self._mark_delivered(connection, envelope):
                    await self._mark_running(connection, envelope)
                    delivered += 1
            elif envelope.attempts >= self._max_attempts:
                if await self._mark_undeliverable(connection, envelope):
                    exhausted += 1
            elif await self._record_failure(
                connection, envelope, "executor reported delivery not durable"
            ):
                failed += 1
        return DeliveryReport(delivered=delivered, failed=failed, exhausted=exhausted)

    async def drain(
        self,
        connection: Connection,
        executor: DispatchExecutor,
        *,
        limit: int = 10,
        max_batches: int = 100,
    ) -> DeliveryReport:
        """Drain until nothing is claimable, or ``max_batches`` passes have run.

        Bounded because an unbounded loop against a table another process is still
        writing to never returns, and "drain forever" is a scheduler's decision, not
        this method's.
        """
        totals = DeliveryReport()
        for _ in range(max(1, int(max_batches))):
            report = await self.drain_once(connection, executor, limit=limit)
            totals = DeliveryReport(
                delivered=totals.delivered + report.delivered,
                failed=totals.failed + report.failed,
                exhausted=totals.exhausted + report.exhausted,
            )
            if report.handled == 0:
                break
        return totals

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    async def _mark_delivered(
        self, connection: Connection, envelope: DispatchEnvelope
    ) -> bool:
        """Mark the row delivered, if this claim still owns it.

        Returns whether this call was the one that marked it.

        Two guards, for two different reasons:

        * `delivered_at IS NULL` makes a second settlement of the same row a no-op
          rather than an overwrite, so a duplicate delivery does not rewrite the
          timestamp of the first -- the evidence of when the work actually went out.
        * `claim_generation = $2` makes a *stale* worker's acknowledgement a no-op. A
          worker whose lease expired does not get to acknowledge work that a successor
          is now responsible for; if it could, the row would be marked delivered on the
          strength of a handoff nobody can still vouch for while the successor is mid
          flight.

        Duplicate-delivery safety is unaffected, because it never rested on this guard:
        it rests on the operation's uniqueness constraint, keyed on stable operation
        identity rather than on the claim.
        """
        return await self._owned_update(
            connection,
            envelope,
            """
            UPDATE harness_dispatch_outbox
               SET delivered_at = now(), claimed_until = NULL, last_error = NULL
             WHERE id = $1 AND claim_generation = $2 AND delivered_at IS NULL
            """,
        )

    async def _record_failure(
        self, connection: Connection, envelope: DispatchEnvelope, error: str
    ) -> bool:
        """Release the claim and record why, leaving the row pending.

        Returns whether this call released the claim.

        The claim is cleared rather than left to expire so the next pass can retry
        immediately: a known failure has no reason to wait out a lease meant for a
        worker that might still be alive. `attempts` was already incremented at
        claim time, so nothing here can lose that count.

        `claim_generation = $2` is the guard that was missing, and its absence was
        reproducible against a real database: claimant A's lease expired, B legitimately
        took the next attempt, A's late failure report cleared B's lease because the
        UPDATE matched on id alone, and a third worker immediately claimed a row B was
        still delivering. One row, two live workers, from a report that arrived late.
        With the generation required, A's late write matches nothing and B keeps its
        lease.

        The error text is truncated: it reaches a column and a log line, and an
        executor's exception message is not a bounded value.
        """
        return await self._owned_update(
            connection,
            envelope,
            """
            UPDATE harness_dispatch_outbox
               SET claimed_until = NULL, last_error = $3
             WHERE id = $1 AND claim_generation = $2 AND delivered_at IS NULL
            """,
            error[:2000],
        )

    async def _owned_update(
        self,
        connection: Connection,
        envelope: DispatchEnvelope,
        statement: str,
        *extra: object,
    ) -> bool:
        """Run a settlement UPDATE that requires the claim this envelope holds.

        Returns True only when a row was actually changed, which is what lets a caller
        distinguish "I settled this" from "my claim was already superseded". Every
        settlement goes through here so the ownership predicate is written once: a
        second spelling of it is a second place for it to be forgotten, and forgetting
        it is the defect this method exists to fix.

        The row count is read from the driver's command tag (`UPDATE <n>`) rather than
        from a `RETURNING` clause, so this works for statements that return nothing. A
        tag that cannot be parsed is treated as "not settled" -- the conservative
        answer, since claiming to have settled a row this call may not have touched
        is the error with consequences.
        """
        tag = await connection.execute(
            statement, envelope.outbox_id, envelope.claim_generation, *extra
        )
        return _rows_affected(tag) > 0

    async def _mark_undeliverable(
        self,
        connection: Connection,
        envelope: DispatchEnvelope,
        *,
        detail: str = "dispatch could not be delivered within the attempt limit",
    ) -> bool:
        """Stop retrying a row and mark its operation's outcome UNKNOWN, atomically.

        Returns whether this call settled the row.

        UNKNOWN, not FAILED. "We could not hand this to an executor" is not evidence
        about what a provider did -- and if any attempt *did* reach the executor
        before the response was lost, the work may be running. Recording FAILED here
        would let a consumer release a budget reservation or retry a provision that
        actually happened, which is the read-a-timeout-as-a-failure defect #5524 §3.5
        names as already having shipped upstream.

        **Claim ownership is settled first, and the operation only if that succeeded.**
        The order is the fix for the stale-verdict hole: an expired predecessor must not
        conclude a successor's operation. Previously exhaustion read the operation and
        wrote UNKNOWN with no reference to which claim was speaking, so a verdict from a
        worker whose lease had expired could terminate an operation another worker was
        actively delivering. Stamping `abandoned_at` under the generation guard first
        means a stale caller stops here, having changed nothing.

        ## Why the whole thing is one transaction

        Ordering the two writes correctly is not the same as making them one fact, and
        the gap between them was reachable. `abandoned_at` committed on its own removes
        the row from *both* recovery predicates at once -- `claim` skips abandoned rows
        and `recover_abandoned` only looked for unabandoned ones -- so a process that
        died after that commit and before the operation was concluded left an operation
        PENDING with nothing anywhere still looking for it. Every subsequent sweep
        reported zero work. The only repair was an operator writing to the table by
        hand, which is precisely the state a durable store exists to make impossible.

        Both writes are the same database, so this needs no coordinator: one transaction
        is sufficient and is the whole mechanism. An interruption anywhere inside it --
        after the abandonment write, between that write and the read, or during the
        transition -- rolls the abandonment back, which returns the row to the condition
        the sweep looks for and makes the next sweep finish the job. Nested inside a
        caller's transaction this becomes a savepoint, which has the same property.

        The rollback restores `claimed_until` along with `abandoned_at`, so an
        interrupted exhaustion inside `drain_once` waits out the lease it was holding
        before recovery takes it -- correct, because until that lease lapses the
        interrupted worker cannot be assumed dead.

        The row is left in place, undelivered, with `last_error` intact: an operator
        needs to see it, and deleting it would destroy the only record that the work
        was accepted.
        """
        async with connection.transaction():  # type: ignore[attr-defined]
            settled = await self._owned_update(
                connection,
                envelope,
                # `abandoned_at IS NULL` is deliberately NOT a predicate here, and
                # `COALESCE` is why that is safe: the repair branch of
                # `recover_abandoned` settles rows a previous build already stamped,
                # and a guard that refused them would leave exactly the operations this
                # transaction exists to rescue unrescuable. The first stamp's timestamp
                # is preserved, so the evidence of when the row was given up on is not
                # rewritten by the pass that finishes the job. Ownership is still fenced
                # on `claim_generation`, which is what keeps a stale worker out.
                """
                UPDATE harness_dispatch_outbox
                   SET claimed_until = NULL,
                       abandoned_at = COALESCE(abandoned_at, now())
                 WHERE id = $1 AND claim_generation = $2
                   AND delivered_at IS NULL
                """,
            )
            if not settled:
                # Either a successor owns the row now, or it was delivered. Both mean
                # this caller has nothing to say about the operation's outcome.
                return False
            record = await connection.fetchrow(
                "SELECT state, version FROM harness_operations WHERE operation_id = $1",
                envelope.operation_id,
            )
            if record is None:
                return True
            data = dict(record)  # type: ignore[call-overload]
            if OperationState(data["state"]) in TERMINAL_STATES:
                # Two cases, one answer. A real conclusion (SUCCEEDED/FAILED/CANCELLED)
                # came from a channel that actually knows what the provider did, and
                # overwriting it with UNKNOWN would destroy that information. An
                # operation already at UNKNOWN needs nothing: re-transitioning it would
                # bump the version and rewrite `detail` for no new fact, which is how a
                # repair pass turns into churn a reader has to interpret.
                return True
            try:
                await self._store.transition(
                    connection,
                    envelope.operation_id,
                    expected_version=data["version"],
                    state=OperationState.UNKNOWN,
                    detail=detail,
                )
            except ConcurrentUpdate:
                # Something else moved the operation between the read and the write. It
                # knows more than this loop does -- an executor reporting a real outcome
                # is exactly what would race here -- so its answer stands. Caught inside
                # the transaction on purpose: this is a settled outcome, not a failure,
                # and letting it roll back would undo an abandonment that is correct.
                return True
            return True

    async def _mark_running(
        self, connection: Connection, envelope: DispatchEnvelope
    ) -> None:
        """Move a delivered operation from PENDING to RUNNING, best-effort.

        Best-effort on purpose, and it is the one place in this module where "best
        effort" is correct: the operation's real progress is reported by the executor
        through the channel #5527 owns, and this is a courtesy status so a caller
        polling immediately after dispatch does not see PENDING for a row already
        handed off. If the executor has already reported something, that report wins
        and this does nothing.

        It is also deliberately AFTER `_mark_delivered`. If this were first and then
        the delivered-marking failed, the row would be retried while the operation
        read as RUNNING.
        """
        record = await connection.fetchrow(
            "SELECT state, version FROM harness_operations WHERE operation_id = $1",
            envelope.operation_id,
        )
        if record is None:
            return
        data = dict(record)  # type: ignore[call-overload]
        if OperationState(data["state"]) is not OperationState.PENDING:
            return
        try:
            await self._store.transition(
                connection,
                envelope.operation_id,
                expected_version=data["version"],
                state=OperationState.RUNNING,
            )
        except ConcurrentUpdate:
            return

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------

    async def recover_abandoned(
        self, connection: Connection, *, limit: int = 10
    ) -> int:
        """Settle rows nothing else will: abandoned final claims, and split commits.

        Returns how many were settled. Called by the same scheduler that calls `drain`;
        it is a separate method rather than a step inside `drain_once` because it is a
        different question -- "is there new work?" versus "did a previous worker die
        holding the last attempt?" -- and a caller that wants one may not want the
        other.

        Two disjoint conditions qualify, and the second exists because the first one's
        settlement used to be two commits:

        * **(a) an expired final claim that was never settled** -- the case described
          below, where a worker took the last permitted attempt and died.
        * **(b) a row stamped abandoned whose operation never reached any
          conclusion** -- the split commit. `_mark_undeliverable` now writes both halves
          in one transaction, so this state is no longer *produced*; it can still be
          *present*, left behind by a process running the earlier build, and a row in it
          is invisible to `claim` (abandoned rows are skipped) and to (a) as well
          (`abandoned_at IS NULL`). Its operation would stay PENDING for as long as the
          table does.

          So the transaction prevents new damage and this branch clears existing damage.
          Only one of those is enough for correct code and neither is enough for a
          correct *deployment*: same-database state must be repairable by the same
          database's own sweep, not by an operator writing to the table by hand.

        Both branches re-use `_mark_undeliverable`, which keeps "what the outcome of an
        undeliverable dispatch is" in one place -- UNKNOWN, never overwriting a real
        conclusion, fenced on the generation this pass just took.

        ## The state this exists for

        `attempts` is incremented at claim time and the claim query excludes rows at the
        limit. So a worker that took the last permitted attempt and then died left a row
        that is:

        * not delivered -- the handoff never completed;
        * not claimable -- `attempts` has reached the cap;
        * not settled -- `_mark_undeliverable` ran in the process that died.

        The operation stayed `pending` forever, owned by nobody, with no path back. That
        is the opposite of resumable crash recovery, and it was reachable from a single
        badly-timed exit.

        ## Why the outcome is UNKNOWN and not FAILED

        The dead worker may have completed the handoff and died before recording it. We
        have no evidence either way, and UNKNOWN is the state that says so. FAILED would
        invite a consumer to release a budget reservation or re-provision infrastructure
        that may already exist -- duplicated spend, which is the specific harm this
        wave's contracts are written against.

        ## Why counting completed failures instead was rejected

        It trades this bug for a worse one: a row whose delivery kills the process every
        time would never accumulate a completed failure, so it would be retried forever
        at the head of the queue. Settling the abandoned claim bounds the work; making
        it retryable does not.

        The claim is taken normally -- `FOR UPDATE SKIP LOCKED`, generation incremented
        -- so two recovery passes cannot both settle one row, and the settlement runs
        under the generation this pass just took.
        """
        bounded = max(1, min(int(limit), 100))
        rows = await connection.fetch(
            """
            WITH stranded AS (
                SELECT o.id
                  FROM harness_dispatch_outbox AS o
                  JOIN harness_operations AS p ON p.operation_id = o.operation_id
                 WHERE o.delivered_at IS NULL
                   AND (
                        -- (a) the abandoned final claim
                        (
                            o.abandoned_at IS NULL
                            AND o.attempts >= $1
                            AND o.claimed_until IS NOT NULL
                            AND o.claimed_until < now()
                        )
                        -- (b) the split commit: given up on, never concluded. No lease
                        -- condition, because the write that cleared `claimed_until` is
                        -- the same one that stamped `abandoned_at` -- there is no lease
                        -- left to wait out, and requiring one would make this branch
                        -- match nothing.
                        OR (
                            o.abandoned_at IS NOT NULL
                            AND p.state <> ALL($3::text[])
                        )
                   )
                 ORDER BY o.id
                 LIMIT $2
                 FOR UPDATE OF o SKIP LOCKED
            )
            UPDATE harness_dispatch_outbox AS o
               SET claim_generation = o.claim_generation + 1
              FROM stranded
             WHERE o.id = stranded.id
            RETURNING o.id, o.operation_id, o.org_id, o.workspace_id, o.job_id,
                      o.attempt_id, o.action, o.attempts, o.claim_generation,
                      o.request_payload, o.abandoned_at
            """,
            self._max_attempts,
            bounded,
            list(_CONCLUDED_STATE_VALUES),
        )
        settled = 0
        for data in (dict(row) for row in rows):  # type: ignore[union-attr]
            envelope = DispatchEnvelope(
                outbox_id=data["id"],
                operation_id=data["operation_id"],
                org_id=data["org_id"],
                workspace_id=data["workspace_id"],
                job_id=data["job_id"],
                attempt_id=data["attempt_id"],
                action=data["action"],
                attempts=data["attempts"],
                claim_generation=data["claim_generation"],
                request_payload=data["request_payload"],
            )
            # The row was selected before this statement touched it, so `abandoned_at`
            # here is whatever it was on entry -- which is exactly which branch matched.
            # Distinguished because the two are different facts for whoever reads the
            # operation's `detail` later: "nobody ever reported an outcome" and "the
            # outcome was recorded here but the record did not land" lead to different
            # investigations.
            if data["abandoned_at"] is None:
                detail = (
                    "the final delivery attempt was claimed and never settled; the "
                    "worker holding it did not report an outcome, so whether the "
                    "dispatch reached an executor cannot be established"
                )
            else:
                detail = (
                    "delivery was given up on and the operation's outcome was never "
                    "recorded; this sweep completed the settlement, and whether the "
                    "dispatch reached an executor cannot be established"
                )
            if await self._mark_undeliverable(connection, envelope, detail=detail):
                settled += 1
        return settled

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    async def pending_count(self, connection: Connection) -> int:
        """How many rows are still undelivered, including exhausted ones.

        For an operator and for tests. Counts exhausted rows too, because a row that
        stopped being retried is still work that was accepted and never delivered,
        and hiding it behind the attempt cap is how a queue looks empty while holding
        unfinished obligations.
        """
        value = await connection.fetchval(
            "SELECT count(*) FROM harness_dispatch_outbox WHERE delivered_at IS NULL"
        )
        return int(value or 0)
