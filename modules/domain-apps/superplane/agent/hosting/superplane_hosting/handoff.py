"""Ordered completion of work that must outlive the agent process.

Issue #5050 (U5), EPIC #4910. Requirement R10 acceptance 2.

The rule, copied from the established precedent in
`modules/domain-apps/cyber/workers/triage/handler.py:178-248`:

    durable state written -> durable handoff sent -> input message deleted LAST

Deleting the input message is the irreversible acknowledgement that the work is safely
recorded, so it is only correct as the final step. A crash at any earlier point leaves the
message unacknowledged; its visibility timeout expires, the message returns to the queue,
and KEDA spawns a replacement job. The inverse order loses the work silently: the message
is gone and nothing durable records what it was for.

## Why this returns a step log rather than a bool

The requirement is about ORDER, and a boolean cannot carry order. A test that only checked
"state written" and "message deleted" would pass against an implementation that deleted
first. `complete_session` therefore appends each completed step to a list and returns it,
so the ordering itself is what gets asserted.

## What this module deliberately does not do

`DurableStore`, `HandoffChannel` and `AgentInbox` are ports, not implementations. R10
acceptance 4 puts the durable workload lifecycle in B's scope: no job/attempt state
machine, no queue, no approval store, no budget ledger is implemented here. This module
owns the ORDERING BETWEEN those ports and nothing else. Defining a port is not taking
ownership of the lifecycle behind it, and a mock standing in for B closes no live
criterion (`acceptance-split.md` rule 2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from .alerts import AlertRecord


class Step(str, Enum):
    """The ordered steps of a durable completion.

    Declaration order is the required execution order, and `ORDER` below derives from it
    rather than repeating it — a list written out by hand is a second source of truth that
    can disagree with the code it describes.
    """

    STATE_WRITTEN = "durable_state_written"
    ALERTS_RECORDED = "durable_alerts_recorded"
    HANDOFF_SENT = "durable_handoff_sent"
    INPUT_DELETED = "input_message_deleted"


#: The required order. `INPUT_DELETED` is last; that is the acceptance criterion.
ORDER: tuple[Step, ...] = tuple(Step)


class OrderingViolation(RuntimeError):
    """Raised when a completion step is attempted out of order.

    A loud failure rather than a silent reorder: an out-of-order delete is the bug class
    acceptance 2 exists to prevent, and a caller that has got here has a real defect.
    """


@runtime_checkable
class DurableStore(Protocol):
    """Port for durable session state. Owned by B; mocked in this module's tests."""

    def write_session_result(self, session_id: str, result: dict) -> None: ...

    def write_alert(self, record: AlertRecord) -> None: ...


@runtime_checkable
class HandoffChannel(Protocol):
    """Port for the durable downstream handoff. Owned by B; mocked in tests."""

    def send(self, session_id: str, payload: dict) -> None: ...


@runtime_checkable
class AgentInbox(Protocol):
    """Port for the agent's input message.

    Named an *inbox*, not a queue: R10 acceptance 4's carried-forward boundary is that
    agent inboxes are not workload queues. This is the message that woke the agent up, and
    `delete` is the acknowledgement that it has been handled.
    """

    def delete(self, receipt_handle: str) -> None: ...


@dataclass
class SessionOutcome:
    """What a finished reasoning session has to hand off.

    `alerts` are operator alerts the session produced (overdue cleanup, budget). They ride
    the same durable write as the result so that an alert cannot be lost by a crash that
    the result survives.
    """

    session_id: str
    receipt_handle: str
    result: dict
    alerts: tuple[AlertRecord, ...] = ()


@dataclass
class CompletionLog:
    """The steps that actually completed, in the order they completed."""

    steps: list[Step] = field(default_factory=list)

    def record(self, step: Step) -> None:
        """Append `step`, refusing any order other than `ORDER`.

        Steps may be SKIPPED (a session with no alerts records none), but the relative
        order of the steps that do run must match `ORDER`.
        """
        if self.steps:
            previous = self.steps[-1]
            if ORDER.index(step) <= ORDER.index(previous):
                raise OrderingViolation(
                    f"Step {step.value!r} cannot follow {previous.value!r}: "
                    f"required order is {[s.value for s in ORDER]}. "
                    "Deleting the input message before the durable handoff loses work silently."
                )
        self.steps.append(step)

    @property
    def input_deleted(self) -> bool:
        """Whether the input message was acknowledged."""
        return Step.INPUT_DELETED in self.steps

    def durable_work_finished_before_delete(self) -> bool:
        """Whether every durable step preceded the delete.

        True for a log with no delete at all: nothing was acknowledged, so nothing was
        acknowledged early. That is the crash case, and it is safe — the message returns
        to the queue for redelivery.
        """
        if not self.input_deleted:
            return True
        return self.steps.index(Step.INPUT_DELETED) == len(self.steps) - 1


def complete_session(
    outcome: SessionOutcome,
    store: DurableStore,
    handoff: HandoffChannel,
    inbox: AgentInbox,
) -> CompletionLog:
    """Complete a reasoning session in the order R10 acceptance 2 requires.

    Returns the log of completed steps. Any exception propagates with the input message
    still undeleted, which is what makes the work recoverable: KEDA respawns the job when
    the message becomes visible again.
    """
    log = CompletionLog()

    # 1. Durable state first. Nothing is acknowledged until the work is recorded.
    store.write_session_result(outcome.session_id, outcome.result)
    log.record(Step.STATE_WRITTEN)

    # 2. Operator alerts, durably, while the input message is still redeliverable. This is
    #    what makes an alert survive the agent exiting (acceptance 3): the record outlives
    #    the process, and a separate sink delivers it. See alerts.py.
    if outcome.alerts:
        for record in outcome.alerts:
            store.write_alert(record)
        log.record(Step.ALERTS_RECORDED)

    # 3. Durable handoff to the downstream consumer.
    handoff.send(outcome.session_id, outcome.result)
    log.record(Step.HANDOFF_SENT)

    # 4. Input message deleted LAST. Everything durable is already recorded, so losing the
    #    message from here on costs nothing.
    inbox.delete(outcome.receipt_handle)
    log.record(Step.INPUT_DELETED)

    return log
