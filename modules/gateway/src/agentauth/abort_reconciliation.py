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
from datetime import UTC, datetime

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


VANISHED_RECONCILED_BY = "vanished_pod_reconciliation"
VANISHED_STATUS = "failed"


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
        return AbortRepair(False, "tenant_mismatch")
    if "abort_command_id" not in execution:
        return AbortRepair(False, "not_aborted")
    return _write_terminal_status(
        authority_client=authority_client,
        events_table=events_table,
        execution=execution,
        invocation_id=invocation_id,
        tenant_id=tenant_id,
        status=ABORTED_STATUS,
        stop_reason="operator_aborted",
        reconciled_by_attribute="abort_reconciled_by",
        reconciled_by=RECONCILED_BY,
        requested_at=execution.get("abort_requested_at", {}).get("S", ""),
        subject="an aborted run",
    )


def repair_vanished_terminal_status(
    *,
    authority_client,
    events_table: str,
    execution: dict,
    invocation_id: str,
    tenant_id: str,
    authority_table: str | None = None,
) -> AbortRepair:
    """Write the terminal ``failed`` row for a bound run whose pod object no longer exists (#7783).

    The sweep established that the pod the run was bound to is positively gone (404
    NotFound) and that the claim has been silent for the grace period. The run can no
    longer report, so its events row would say ``in_progress`` forever: the dashboard
    would show it live, and ``compute_liveness`` would keep every exit-gated path
    (review recovery, continuation discharge) refusing with "unverified". Same
    fail-closed shape as the abort repair: never overwrite a terminal status, never
    raise, let the next pass retry a transient failure.
    """
    if execution.get("tenant_id") != {"S": tenant_id}:
        return AbortRepair(False, "tenant_mismatch")
    if not execution.get("workload_binding", {}).get("S"):
        return AbortRepair(False, "not_bound")
    result = _write_terminal_status(
        authority_client=authority_client,
        events_table=events_table,
        execution=execution,
        invocation_id=invocation_id,
        tenant_id=tenant_id,
        status=VANISHED_STATUS,
        stop_reason="pod_vanished",
        reconciled_by_attribute="terminal_reconciled_by",
        reconciled_by=VANISHED_RECONCILED_BY,
        requested_at=None,
        subject="a vanished pod's run",
    )
    if result.reason == TRANSIENT_REPAIR_FAILURE:
        return result
    # The protected EXEC record is the authority every exit-gated engine path reads
    # (`protected_failure_for_assignment`, protected review recovery); mark it
    # terminal too, under the same fences retained-abort recovery uses, so the run
    # reads as a failed exit rather than staying `active` forever.
    exec_repaired = _mark_execution_vanished(
        authority_client=authority_client, authority_table=authority_table, execution=execution, invocation_id=invocation_id, tenant_id=tenant_id
    )
    if exec_repaired == TRANSIENT_REPAIR_FAILURE:
        return AbortRepair(False, TRANSIENT_REPAIR_FAILURE)
    return result if result.repaired or exec_repaired != "repaired" else AbortRepair(True, "repaired")


def _mark_execution_vanished(*, authority_client, authority_table: str | None, execution: dict, invocation_id: str, tenant_id: str) -> str:
    """Conditionally conclude an ``active`` EXEC row as ``completed``/``failed`` for a vanished pod."""
    if not authority_table or execution.get("status") != {"S": "active"}:
        return "not_active"
    if not execution.get("workload_binding", {}).get("S") or not execution.get("current_attempt", {}).get("N"):
        return "not_bound"
    try:
        authority_client.update_item(
            TableName=authority_table,
            Key={"pk": {"S": f"TENANT#{tenant_id}"}, "sk": {"S": f"EXEC#{invocation_id}"}},
            ConditionExpression="#s = :active AND tenant_id = :tenant AND workload_binding = :uid AND current_attempt = :attempt",
            UpdateExpression="SET #s = :complete, terminal_outcome = :outcome, terminal_reconciled_by = :source, exit_reason = :exit_reason",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":active": {"S": "active"},
                ":complete": {"S": "completed"},
                ":tenant": {"S": tenant_id},
                ":uid": execution["workload_binding"],
                ":attempt": execution["current_attempt"],
                ":outcome": {"S": VANISHED_STATUS},
                ":source": {"S": VANISHED_RECONCILED_BY},
                ":exit_reason": {"S": "pod_vanished"},
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return "execution_changed"
        logger.error("Could not conclude a vanished pod's execution record", extra={"invocation_id": invocation_id})
        return TRANSIENT_REPAIR_FAILURE
    except BotoCoreError:
        logger.error("Could not conclude a vanished pod's execution record", extra={"invocation_id": invocation_id})
        return TRANSIENT_REPAIR_FAILURE
    logger.warning("Concluded a vanished pod's execution record as failed (invocation=%s)", invocation_id)
    return "repaired"


def _write_terminal_status(
    *,
    authority_client,
    events_table: str,
    execution: dict,
    invocation_id: str,
    tenant_id: str,
    status: str,
    stop_reason: str,
    reconciled_by_attribute: str,
    reconciled_by: str,
    requested_at: str | None,
    subject: str,
) -> AbortRepair:
    arrived_at = execution.get("arrived_at", {}).get("S")
    if not arrived_at or not invocation_id or not events_table:
        return AbortRepair(False, "row_key_unavailable")

    names = {"#st": "status", "#tid": "tenant_id", "#rb": reconciled_by_attribute}
    values = {
        ":status": {"S": status},
        ":updated_at": {"S": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
        ":tid": {"S": tenant_id},
        ":reconciled_by": {"S": reconciled_by},
        ":stop_reason": {"S": stop_reason},
    }
    sets = ["#st = :status", "status_updated_at = :updated_at", "stop_reason = :stop_reason", "#rb = :reconciled_by"]
    if requested_at is not None:
        values[":requested_at"] = {"S": requested_at}
        sets.append("abort_requested_at = :requested_at")
    protected = []
    for index, terminal in enumerate(_PROTECTED_STATUSES):
        values[f":p{index}"] = {"S": terminal}
        protected.append(f"#st <> :p{index}")
    condition = "attribute_exists(event_id) AND #tid = :tid AND (" + " AND ".join(protected) + ")"
    try:
        authority_client.update_item(
            TableName=events_table,
            Key={"event_id": {"S": invocation_id}, "arrived_at": {"S": arrived_at}},
            UpdateExpression="SET " + ", ".join(sets) + " REMOVE " + ", ".join(CONTROL_ATTRIBUTES),
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            try:
                row = authority_client.get_item(
                    TableName=events_table,
                    Key={"event_id": {"S": invocation_id}, "arrived_at": {"S": arrived_at}},
                    ConsistentRead=True,
                ).get("Item", {})
            except (ClientError, BotoCoreError):
                return AbortRepair(False, TRANSIENT_REPAIR_FAILURE)
            if not row:
                return AbortRepair(False, "event_row_missing")
            if row.get("tenant_id") != {"S": tenant_id}:
                return AbortRepair(False, "event_tenant_mismatch")
            if row.get("status", {}).get("S") in OBSERVED_TERMINAL_STATUSES:
                return AbortRepair(False, "already_terminal")
            return AbortRepair(False, "event_row_not_repairable")
        logger.error(
            "Could not repair %s terminal status",
            subject,
            extra={"invocation_id": invocation_id, "code": exc.response.get("Error", {}).get("Code")},
        )
        return AbortRepair(False, TRANSIENT_REPAIR_FAILURE)
    except BotoCoreError:
        logger.error("Could not repair %s terminal status", subject, extra={"invocation_id": invocation_id})
        return AbortRepair(False, TRANSIENT_REPAIR_FAILURE)
    logger.warning(
        "Repaired %s terminal status after its pod stopped without reporting it; the dashboard showed this run as live until now (invocation=%s)",
        subject,
        invocation_id,
    )
    return AbortRepair(True, "repaired")
