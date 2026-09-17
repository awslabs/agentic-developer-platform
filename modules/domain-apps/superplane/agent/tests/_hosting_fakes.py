"""Fakes and constants shared by the hosting suites.

Issue #5050 (U5), EPIC #4910.

A separate module rather than living in `conftest.py` because the test modules need to
import these names directly (`Boom`, `RecordingInbox`, `FakeSink`), and importing conftest
by name is fragile — pytest owns that module's identity, and this module directory is not a
package, so `from .conftest import ...` is not available. `conftest.py` imports from here
and exposes the fixtures.

## These fakes stand in for B, and that is recorded

`DurableStore`, `HandoffChannel` and `AgentInbox` are ports whose implementations belong to
B (R10 acceptance 4). Everything here is a MOCK, and per `acceptance-split.md` rule 2 a
green run against a mock closes no live criterion — it establishes this module's ordering
logic and nothing about B's durable store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import _hosting_path  # noqa: F401  (imported for its sys.path side effect)
from superplane_hosting import AlertRecord, LifetimeOwner

# A fixed, timezone-aware instant. These tests assert ordering and validation branches,
# never "now", so a constant keeps them deterministic — and AlertRecord rejects naive
# datetimes, so a naive constant would fail construction.
RAISED_AT = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)

SESSION_ID = "session-u5-1"
RECEIPT_HANDLE = "receipt-handle-abc"


class Boom(RuntimeError):
    """A mid-run crash, raised by a fake to simulate the agent dying."""


@dataclass
class CallLog:
    """Ordered record of every side effect across all fakes.

    One shared log rather than one per fake: the acceptance criterion is about the order of
    events ACROSS the store, the channel and the inbox, and separate logs cannot express a
    cross-object ordering.
    """

    calls: list[str] = field(default_factory=list)

    def record(self, name: str) -> None:
        self.calls.append(name)

    def index(self, name: str) -> int:
        return self.calls.index(name)

    def __contains__(self, name: str) -> bool:
        return name in self.calls


@dataclass
class RecordingStore:
    """Fake durable store. Mock for B's store; records call order."""

    log: CallLog
    fail_on_result: bool = False
    fail_on_alert: bool = False
    results: dict[str, dict] = field(default_factory=dict)
    alerts: list[AlertRecord] = field(default_factory=list)

    def write_session_result(self, session_id: str, result: dict) -> None:
        if self.fail_on_result:
            raise Boom("durable state write failed")
        self.results[session_id] = result
        self.log.record("write_session_result")

    def write_alert(self, record: AlertRecord) -> None:
        if self.fail_on_alert:
            raise Boom("durable alert write failed")
        self.alerts.append(record)
        self.log.record("write_alert")


@dataclass
class RecordingHandoff:
    """Fake durable handoff channel. Mock for B's channel."""

    log: CallLog
    fail: bool = False
    sent: list[tuple[str, dict]] = field(default_factory=list)

    def send(self, session_id: str, payload: dict) -> None:
        if self.fail:
            raise Boom("durable handoff failed")
        self.sent.append((session_id, payload))
        self.log.record("send")


@dataclass
class RecordingInbox:
    """Fake agent inbox.

    `deleted` is the flag the crash tests read: an undeleted message is a message that
    returns to the queue for KEDA to respawn against, which is the safety property.
    """

    log: CallLog
    deleted: list[str] = field(default_factory=list)

    def delete(self, receipt_handle: str) -> None:
        self.deleted.append(receipt_handle)
        self.log.record("delete")


@dataclass
class FakeSink:
    """Configurable alert sink, so a test can build the shapes `emit` must refuse."""

    lifetime_owner_value: LifetimeOwner = LifetimeOwner.INDEPENDENT
    durable: bool = True
    delivered: list[AlertRecord] = field(default_factory=list)

    @property
    def lifetime_owner(self) -> LifetimeOwner:
        return self.lifetime_owner_value

    @property
    def is_durable(self) -> bool:
        return self.durable

    def deliver(self, record: AlertRecord) -> None:
        self.delivered.append(record)
