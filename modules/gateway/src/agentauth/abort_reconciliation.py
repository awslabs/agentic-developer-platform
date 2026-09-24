"""Eventual repair of an aborted run's terminal row, after the run has actually stopped (#3963 S4).

## The gap this closes

``_persist_abort_terminal_status`` in the worker retries the terminal ``aborted``
write ``ABORT_TERMINAL_WRITE_ATTEMPTS`` times and then gives up. That bound is
deliberate — the retries run *before* the DeleteMessage that prevents a rerun, so a
longer sequence would delay the one write that matters most, and unbounded retry
would hold the FIFO message group for the pod's whole lifetime. The worker's own
docstring is explicit that this "narrows the window; it does not close it".

``_finalize_abort_acknowledgement`` then makes one more attempt, after a confirmed
acknowledgement, which is the safest moment the *pod* has: the delete is confirmed,
so a retry cannot delay the rerun guarantee. But it is still the same pod, in the
same seconds, against the same possibly-unavailable table. When DynamoDB is down for
longer than that, the pod exits and the row stays at ``in_progress`` forever:

    operator asks for a stop -> run genuinely stops -> dashboard shows it live

That is the outcome S4 names as unacceptable. The operator was told the abort was
accepted (they hold a signed receipt), the run really did stop, and the only surface
they can see says otherwise — indefinitely, because nothing on the platform ever
looks at that row again. Every retry inside the pod's lifetime is a *narrower*
window, never a closed one, because the pod is precisely the thing that goes away.

## Why the fact survives even though the write did not

The repair is possible at all because the accepted abort was recorded somewhere the
failing write is not: ``record_abort_intent`` puts ``abort_command_id`` /
``abort_requested_at`` on the protected ``EXEC#`` record in the authority table —
the table the worker role cannot address. So "an operator stopped this run" is
durable independently of whether the run managed to report it. The events row is the
*report*; the authority marker is the *fact*. This module reads the fact and repairs
the report.

That asymmetry is the whole reason a reconciler can exist here. If the abort had been
recorded on the events row (the obvious place), the same outage that lost the
terminal write could have lost the marker, and there would be nothing left to
reconcile from.

## "After actual quiescence" is a claim about the pod, not about a timer

The repair writes a terminal status, and a terminal status asserts the run is over.
So the gate has to be evidence that it *is* over. This module does not decide that
itself; it accepts a caller that has already established it, and the caller in
production is ``work_admission.recover_exited_claims``, which reaches this function
only from its ``workload.has_exited`` branch: a Kubernetes read showing the
``agent-worker`` container in ``terminated`` state, with the pod UID and service
account verified, and the pod phase ``Succeeded`` or ``Failed``. The sweep's other
branches mean the run reported its own outcome, so there is nothing to repair.

Three weaker gates were available and are all wrong:

*Elapsed time.* A 24-hour staleness cutoff is what the dashboard already uses to
render a badge, and ``liveness.py`` is emphatic that it must not become a verdict:
"loss of contact is not evidence of exit". A time-based repair would write
``aborted`` on a run that is still cancelling.

*Lease or credential expiry.* ``has_exited``'s own docstring rules this out — "lease
expiry is deliberately absent from this decision" — and ``execution.py`` puts it more
sharply: a signature cannot know it is stale.

*The abort marker alone.* The marker means the abort was *accepted*, not that the run
stopped; ``record_abort_intent`` says so at length, and deliberately leaves the
execution ``ACTIVE`` so the worker keeps the credential it needs to actually perform
the cancellation. Repairing on the marker alone would report a deliberate stop while
the task was still running — the ``unverifiable``-collapsed-into-``exited`` mistake.

## Why it rides the existing sweep instead of bringing a scheduler

``maintain_work_claims`` already runs this pass every 60s inside the gateway
processes ("Lifecycle cleanup in existing gateway processes; no new scheduler"), it
already holds the exit evidence, and it already reads the ``EXEC#`` record this
module needs. So the repair costs one conditional write on a pass that was going to
happen anyway.

Crucially it is also the *retry* source, which took a correction to get right. That
sweep selects claims in state ``HELD``; the first version of this wiring repaired
after the release, so a repair that failed on a transient error had its claim flipped
to ``RELEASED`` in the same pass and was never looked at again — reintroducing the
permanently stale row, only now with a reconciler that appeared to cover it. The
repair therefore runs *before* the release, and a ``TRANSIENT_REPAIR_FAILURE`` leaves
the claim held so the next pass re-selects the same row. Retry needs no attempt
counter and no extra state, and it survives a restart of the repairing process
because the thing being retried is a Postgres row, not in-memory work.

The cost is explicit: while the events table is unavailable, that issue's lane stays
blocked. That is deliberate and it is bounded to the transport failure alone — a
determination that will not change on retry (an unset table name, a tenant
disagreement) falls through and releases, so a misconfiguration cannot wedge a lane
forever.

The alternative — a cron Lambda scanning ``webhook-events`` for stale rows — needs
``dynamodb:Scan`` on a table where that grant is currently and deliberately withheld
(the orchestration tick is restricted to Query on one index, with a comment
explaining that a Scan grant would let it read 30 days of every tenant's
deliveries), or a new status-keyed GSI. Enumerating *claims* instead of *rows* avoids
the whole question: the set of runs that might need repair is already indexed, in
Postgres, by the sweep that knows they are dead.

## Why the write goes through the gateway role

The worker role was deliberately stripped of unconditioned ``UpdateItem`` on
``webhook-events`` (#5028 AC4) because the row key is caller-supplied and every
worker shares the role. This repair therefore cannot live in the worker even in
principle, and it does not want to: the pod is gone. The gateway role already holds
exactly the two actions needed (``GetItem``, ``UpdateItem``, via
``gateway_agent_self_write``) plus the CMK grant, so no new permission is introduced
by this module.

## Where the vocabulary comes from

Both the status written and the set of statuses refused are imported from
``activity.liveness``, which declares itself the single owner of "is a run still
active" and is what the dashboard actually renders. Restating either here would
create a second definition of "already finished" that could disagree with the
surface the operator reads — and the refusal set is where that bites, because a copy
is only as good as its author's memory of the full list.

## The consequence that must be stated, not discovered

``is_delivery_completed`` treats ``status == aborted`` as completed, so writing this
row also means a later redelivery of that invocation is refused on the legacy path.
That is intended — it is the same refusal the abort was trying to establish — but it
is a real second effect of a write described as "reporting", and a future reader
should not have to infer it. On the protected path the refusal does not depend on
this row at all: ``bind`` refuses a redelivered envelope because the record is ACTIVE
and already bound.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError

from src.activity.liveness import ABORTED_STATUS, OBSERVED_TERMINAL_STATUSES
from src.agentauth.registration import CONTROL_ATTRIBUTES

logger = logging.getLogger("bedrockgateway.agentauth.abort_reconciliation")

# Written alongside the status so an operator reading the row can tell a repaired
# report from one the run wrote itself. Without it, the two are indistinguishable,
# and "the abort reported itself normally" is a materially different story from "the
# run died without reporting and the platform repaired it 40 seconds later" — the
# second one means the operator's dashboard was wrong for those 40 seconds, which is
# exactly the kind of thing an incident review needs to be able to see.
RECONCILED_BY = "abort_reconciliation"

# The one `AbortRepair.reason` that means "try this again unchanged". Named and
# exported because the caller must branch on it — it decides whether the work claim is
# held open for another pass — and a caller re-spelling the string would silently stop
# retrying the moment either side was reworded. Only transport failure qualifies:
# `not_aborted` / `tenant_mismatch` / `row_key_unavailable` are determinations that
# will not change on a retry, and `already_terminal` means someone else got there.
TRANSIENT_REPAIR_FAILURE = "events_table_unavailable"

# Statuses this repair must not overwrite: exactly `OBSERVED_TERMINAL_STATUSES`,
# imported rather than restated. That set's defining property is the one this guard
# needs — every member is "written by a component that saw the outcome it is
# reporting" — and this pass saw only that the pod is gone. A hand-written copy here
# would be a second definition of "already finished" free to drift from the one the
# dashboard renders: my first draft of this list had already silently dropped
# `rejected` and `rate_limited`, which would have let a repair overwrite an ingress
# refusal with `aborted` and report that an operator stopped a run that never began.
#
# In particular `failed` is NOT upgraded to `aborted`: a run that was aborted and also
# failed on its way out reported the failure it observed, and replacing that with a
# tidier story would destroy the more specific evidence.
#
# `complete` matters most. A run can be aborted moments after it genuinely finished —
# the operator's command and the run's last work racing — and in that case the work IS
# done. Overwriting `complete` with `aborted` would tell the operator their stop took
# effect when it did not, and would hide a completed run's results.
#
# Sorted so the generated ConditionExpression is byte-identical between processes;
# `frozenset` iteration order varies with the hash seed, and an expression that
# differs per replica is needlessly hard to match against CloudTrail.
_PROTECTED_STATUSES: tuple[str, ...] = tuple(sorted(OBSERVED_TERMINAL_STATUSES))


@dataclass(frozen=True)
class AbortRepair:
    """What one repair attempt did, for the caller's log line.

    ``repaired`` is false both when no repair was needed and when one was refused;
    ``reason`` distinguishes them. A bare boolean would make "there was nothing to
    fix" and "there was something to fix and we could not" the same value, and those
    require opposite responses from whoever reads the metric.
    """

    repaired: bool
    reason: str


def repair_aborted_terminal_status(
    *,
    authority_client,
    events_table: str,
    execution: dict,
    invocation_id: str,
    tenant_id: str,
) -> AbortRepair:
    """Write the terminal ``aborted`` row an exited run failed to write.

    ``execution`` is the raw ``EXEC#`` item the caller has *already read* — passed in
    rather than re-read so this cannot disagree with the record the caller used to
    establish that the pod exited. Re-reading would introduce a second version of the
    state the exit decision was made against.

    Returns rather than raises on every failure. The caller is a maintenance sweep
    whose primary job is releasing work claims; a repair that could raise would make
    an unavailable events table abandon claim recovery, which is a strictly worse
    outcome than a stale dashboard row that gets another attempt in 60 seconds.
    """
    if execution.get("tenant_id") != {"S": tenant_id}:
        # The caller's tenant and the record's must agree before a key is built from
        # either. This mirrors `registration._update_row`: the events table is not the
        # protected one, so a disagreement means the two tables describe different
        # things and no write may land.
        return AbortRepair(False, "tenant_mismatch")
    if "abort_command_id" not in execution:
        return AbortRepair(False, "not_aborted")
    arrived_at = execution.get("arrived_at", {}).get("S")
    if not arrived_at or not invocation_id or not events_table:
        # The sort key is only ever the one the protected record captured at trusted
        # dispatch. Deriving it by querying for the newest row under this event_id
        # would let a second planted row decide which row this repair writes.
        return AbortRepair(False, "row_key_unavailable")

    names = {"#st": "status", "#tid": "tenant_id"}
    values = {
        ":status": {"S": ABORTED_STATUS},
        ":tid": {"S": tenant_id},
        ":reconciled_by": {"S": RECONCILED_BY},
        ":stop_reason": {"S": "operator_aborted"},
        ":requested_at": {"S": execution.get("abort_requested_at", {}).get("S", "")},
    }
    # `status_updated_at` is deliberately NOT set to "now". The abort's moment is when
    # the operator's command was accepted, which the marker holds; stamping this repair
    # with the current clock would date the stop minutes after it happened and make the
    # run look like it ran longer than it did.
    protected = []
    for index, status in enumerate(_PROTECTED_STATUSES):
        values[f":p{index}"] = {"S": status}
        protected.append(f"#st <> :p{index}")
    condition = (
        # Repair an existing report, never fabricate a run; the gateway role holds no
        # PutItem on this table for the same reason.
        #
        # `attribute_exists(event_id)` is redundant *today* — `#tid = :tid` already
        # fails on a missing row, because a comparison against an absent attribute is
        # false rather than true, so an UpdateItem on a nonexistent key is refused by
        # the tenant clause alone (verified against DynamoDB semantics, not assumed).
        # It is kept because it states the intent structurally instead of relying on
        # that emergent property: if the tenant check is ever narrowed or moved, this
        # clause is what still stops an update from creating the row. Stated plainly
        # because removing it does NOT fail the suite, and a future reader deleting it
        # as dead weight deserves to know that is expected, not a gap in coverage.
        "attribute_exists(event_id) AND #tid = :tid AND (" + " AND ".join(protected) + ")"
    )
    try:
        authority_client.update_item(
            TableName=events_table,
            Key={"event_id": {"S": invocation_id}, "arrived_at": {"S": arrived_at}},
            UpdateExpression=(
                "SET #st = :status, stop_reason = :stop_reason, "
                "abort_reconciled_by = :reconciled_by, abort_requested_at = :requested_at "
                "REMOVE " + ", ".join(CONTROL_ATTRIBUTES)
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # Already terminal, or not this tenant's row. Idempotent by design: two
            # gateway processes may run this pass against the same claim, and the
            # second one losing the race is the correct outcome, not an error.
            return AbortRepair(False, "already_terminal")
        logger.error(
            "Could not repair an aborted run's terminal status",
            extra={"invocation_id": invocation_id, "code": exc.response.get("Error", {}).get("Code")},
        )
        return AbortRepair(False, TRANSIENT_REPAIR_FAILURE)
    except BotoCoreError:
        logger.error("Could not repair an aborted run's terminal status", extra={"invocation_id": invocation_id})
        return AbortRepair(False, TRANSIENT_REPAIR_FAILURE)
    logger.warning(
        "Repaired an aborted run's terminal status after its pod exited without reporting it; "
        "the dashboard showed this run as live until now (invocation=%s)",
        invocation_id,
    )
    return AbortRepair(True, "repaired")
