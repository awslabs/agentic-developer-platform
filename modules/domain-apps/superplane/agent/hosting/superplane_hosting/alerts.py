"""Operator alerts that remain observable after the agent process has exited.

Issue #5050 (U5), EPIC #4910. Requirement R10 acceptance 3.

## The requirement is about lifetime, not about calling an API

Acceptance 3 asks that an overdue-cleanup or budget alert be observable *after the agent
process has exited*. Anything emitted from inside the agent fails that by construction: if
the agent dies before the emitting statement runs, no alert ever existed — and the case
where the agent died mid-run is precisely the case an operator most needs to hear about.

So this module splits ownership of the alert's lifetime:

  * INSIDE the agent, `record_alert` builds a durable record, which `handoff.py` writes
    before the input message is deleted. A crash leaves both the work and the alert
    recoverable.
  * OUTSIDE the agent, a sink whose `lifetime_owner` is not the agent reads durable
    records and delivers them. The agent's responsibility ends at "the record is durable".

`emit` enforces that split at the boundary. It refuses a sink owned by the agent process
and refuses a log-only sink, because those are the exact two ways this requirement gets
accidentally un-met.

## This path is net-new

There is nothing to wire up, and saying otherwise would under-size the work and ship
nothing observable. `put_events`/`putEvents` has ZERO production callers repo-wide — the
only occurrences are assertions in `.github/scripts/tests/` that ops dispatch does *not*
use it — and no custom event bus exists. Run completion today is log-only ("Phase 1:
Structured Log").

What this module delivers is the emission path and its boundary: the durable record, the
sink contract, and the rejection of the sink shapes that silently fail the requirement.
Deploying the sink RESOURCE is outside the paths allocated to U5 and is part of the
unresolved gate on the live criterion (U5-L1). No account, region or credential label is
named anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable


class AlertKind(str, Enum):
    """The two alert kinds R10 acceptance 3 names."""

    OVERDUE_CLEANUP = "overdue_cleanup"
    BUDGET = "budget"


class LifetimeOwner(str, Enum):
    """Whose process lifetime a sink's delivery depends on.

    This is the distinction the whole module turns on. `AGENT` means delivery happens in
    the agent's process, so it stops when the agent stops. `INDEPENDENT` means delivery is
    performed by something that is still running after the agent is gone.
    """

    AGENT = "agent_process"
    INDEPENDENT = "independent_of_agent"


class AlertSinkRejected(ValueError):
    """Raised when a sink cannot satisfy acceptance 3.

    A loud refusal at the boundary rather than a degraded fallback: falling back to a log
    line is indistinguishable from having no alerting, which is the outcome the
    requirement exists to prevent.
    """


@dataclass(frozen=True)
class AlertRecord:
    """A durable operator alert.

    Frozen because a record is evidence: once written it is what the sink will deliver, and
    a mutable record invites a caller to edit it after the durable write.

    `session_id` is included so an operator can correlate the alert with the session that
    raised it — including a session that is no longer running, which is the normal case
    here. No credential material, account id or workspace payload belongs in `detail`: the
    alert path carries no tenant authority and must expose nothing to an operator who could
    not already read it.
    """

    kind: AlertKind
    session_id: str
    summary: str
    raised_at: datetime
    detail: dict | None = None

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError("An alert with no summary tells an operator nothing.")
        if self.raised_at.tzinfo is None:
            # A naive timestamp on an alert that outlives its process is ambiguous at
            # exactly the moment somebody is reconstructing a timeline from it.
            raise ValueError("raised_at must be timezone-aware.")


@runtime_checkable
class AlertSink(Protocol):
    """A destination for durable alerts.

    `lifetime_owner` and `is_durable` are part of the contract, not metadata: `emit`
    reads them to decide whether the sink can satisfy acceptance 3 at all. A sink that
    cannot answer these is not a sink this module will emit through.
    """

    @property
    def lifetime_owner(self) -> LifetimeOwner: ...

    @property
    def is_durable(self) -> bool: ...

    def deliver(self, record: AlertRecord) -> None: ...


def record_alert(
    kind: AlertKind,
    session_id: str,
    summary: str,
    raised_at: datetime,
    detail: dict | None = None,
) -> AlertRecord:
    """Build a durable alert record inside the agent.

    Building is deliberately separate from delivering. The agent's contribution is a record
    that survives it; `handoff.py` writes that record durably BEFORE the input message is
    deleted, so a crash cannot lose the alert while keeping the work.
    """
    return AlertRecord(
        kind=kind,
        session_id=session_id,
        summary=summary,
        raised_at=raised_at,
        detail=detail,
    )


def emit(record: AlertRecord, sink: AlertSink) -> None:
    """Deliver `record` through `sink`, refusing sinks that cannot outlive the agent.

    Raises `AlertSinkRejected` when the sink's delivery is bound to the agent's process
    lifetime, or when it is not durable. Both refusals are the requirement: an alert
    delivered from inside the agent dies with the agent, and a non-durable sink loses the
    alert on the very failure that makes it worth sending.
    """
    if sink.lifetime_owner is not LifetimeOwner.INDEPENDENT:
        raise AlertSinkRejected(
            "Sink delivery is owned by the agent process, so the alert would die with the "
            "agent — which is exactly the case R10 acceptance 3 covers. Emit through a sink "
            "whose lifetime is independent of the agent."
        )
    if sink.is_durable is not True:
        raise AlertSinkRejected(
            "Sink is not durable, so the alert is lost by the same failure that makes it "
            "worth sending. A log line is not an alert sink."
        )
    sink.deliver(record)
