"""Superplane reasoning-session hosting configuration.

Issue #5050 (U5), EPIC #4910. Requirement R10.

The hosting DECISION — Agent Factory's existing lane, no dedicated Superplane queue or
ScaledJob — is recorded in `../README.md` with the reasoning acceptance 1 requires. This
package holds the two behaviours that decision leaves to this module:

  * `handoff` — the ordering for work that must outlive the agent process (acceptance 2):
    durable state written, durable handoff sent, input message deleted LAST.
  * `alerts` — the net-new emission path for operator alerts that stay observable after
    the agent has exited (acceptance 3).

Deliberately absent, per acceptance 4: any job/attempt state machine, queue, approval store
or budget ledger. The durable workload lifecycle belongs to B.
"""

from __future__ import annotations

from .alerts import (
    AlertKind,
    AlertRecord,
    AlertSink,
    AlertSinkRejected,
    LifetimeOwner,
    emit,
    record_alert,
)
from .handoff import (
    ORDER,
    AgentInbox,
    CompletionLog,
    DurableStore,
    HandoffChannel,
    OrderingViolation,
    SessionOutcome,
    Step,
    complete_session,
)

__all__ = [
    "ORDER",
    "AgentInbox",
    "AlertKind",
    "AlertRecord",
    "AlertSink",
    "AlertSinkRejected",
    "CompletionLog",
    "DurableStore",
    "HandoffChannel",
    "LifetimeOwner",
    "OrderingViolation",
    "SessionOutcome",
    "Step",
    "complete_session",
    "emit",
    "record_alert",
]
