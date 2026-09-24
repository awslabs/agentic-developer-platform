"""Render a task record as the ``task_snapshot`` the contract fixes.

One read must answer the whole question: what state is the task in, what evidence
exists for that state, and where does its event history begin and end. A snapshot
that reported state without the cursor bounds would leave a client unable to start
streaming without a second, possibly-inconsistent read.

The rule this module exists to enforce is the one in invariant LC-08, which T6
owns: **unknown evidence is nullable, never zero and never success.** A lost
heartbeat is not an exit and not a completion, so the honest snapshot of that task
is ``running`` with ``execution_health: "unknown"`` and ``recovery_required: true``
— not ``failed``, and not a fabricated success. The same rule governs the
terminal receipts: a nonterminal task carries ``result: null`` and ``error: null``,
because an in-flight task has no outcome to report and inventing an empty one
would let a polling client conclude the task finished with nothing to show.

Design reference: implementation-design.md section 4; ``public-api.schema.json#/$defs/task_snapshot``.
"""

from __future__ import annotations

from src.tasks.events import SCHEMA_VERSION
from src.tasks.store import TaskRecord

#: Statuses with no committed outcome. Terminal receipts are suppressed for these
#: rather than merely expected-absent, so a store row left inconsistent by a
#: partially-applied transition cannot surface a result on a running task.
NONTERMINAL_STATUSES = frozenset({"accepted", "queued", "running", "waiting_for_input", "cancel_requested"})


def render(record: TaskRecord, *, request_id: str) -> dict:
    """Build the snapshot body for ``GET /v1/tasks/{task_id}``.

    ``request_id`` is threaded in from the route rather than generated here: the
    contract requires the snapshot to carry the same correlation identifier the
    request is logged under, which is the only way an operator can tie a client's
    report of a wrong snapshot to the read that produced it.

    The conditional branches of the schema are enforced here as *construction*
    rules, not assertions. Emitting the record's fields verbatim and trusting
    every writer to have maintained the state/receipt agreement would make this
    route the place where another component's partial write becomes a
    contract-violating response to a caller.
    """
    nonterminal = record.status in NONTERMINAL_STATUSES

    body = {
        "schema_version": SCHEMA_VERSION,
        "task_id": record.task_id,
        "invocation_id": record.invocation_id,
        "persona": record.persona,
        "status": record.status,
        "version": record.version,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "deadline_at": record.deadline_at,
        "latest_event_cursor": record.latest_event_cursor,
        "oldest_event_cursor": record.oldest_event_cursor,
        "generation": record.generation,
        # Nullable on purpose: an attempt that has not registered yet is unknown,
        # and substituting a placeholder would make a stale-attempt fence compare
        # against a value no worker ever held.
        "runtime_attempt_id": record.runtime_attempt_id,
        "execution_health": record.execution_health,
        "recovery_required": record.recovery_required,
        # Suppressed rather than passed through on nonterminal states. See the
        # module docstring: an in-flight task has no outcome.
        "result": None if nonterminal else record.result,
        "error": None if nonterminal else record.error,
        # An open clarification request belongs to exactly one state. Carrying one
        # on a running task would tell a client to answer a prompt the worker is
        # no longer waiting on; carrying one on a terminal task would invite a
        # reply to a task that can no longer consume it.
        "input_request": record.input_request if record.status == "waiting_for_input" else None,
        "command_receipts": [dict(receipt) for receipt in record.command_receipts],
        "queue_ack_status": record.queue_ack_status,
        "request_id": request_id,
    }

    # Optional in the contract, and omitted rather than sent as null when unset:
    # the field is a caller-supplied correlation handle, and an explicit null
    # would assert the caller supplied none where the truth is that this task
    # predates or simply omits the field.
    if record.external_reference is not None:
        body["external_reference"] = record.external_reference

    return body
