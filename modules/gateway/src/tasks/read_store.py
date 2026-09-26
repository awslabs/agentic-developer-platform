"""Read-surface DTOs and storage protocol; InMemoryTaskStore is a test backend.

Production uses DynamoTaskReadStore to adapt the canonical T1 repository.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Protocol

from src.tasks.events import PROTECTED_EVENT_TYPES, TaskEvent, format_cursor
from src.tasks.limits import MAX_EVENTS_PER_TASK, RESERVED_TERMINAL_EVENT_SLOTS


class TaskStoreError(Exception):
    """Storage is unavailable or answered ambiguously.

    Deliberately *not* a "not found": an authorization decision cannot be made
    without reading protected state, and a store failure that presented as
    absence would turn an outage into anonymous access. Routes translate this to
    ``503 prerequisite_unavailable`` and close open streams.
    """


class EventBudgetExhaustedError(Exception):
    """The task has used its event budget, including the reserved tail.

    Raised instead of silently dropping progress. Design section 10 fixes 10,000
    events per task with the final 100 slots reserved for control and terminal
    evidence, so a task that floods progress still has room to record how it
    ended.
    """


class ReportConflictError(Exception):
    """The same report UUID was already committed with different content.

    A retry after a lost response must be idempotent, so the same UUID with the
    same content returns the original sequence. The same UUID with *different*
    content is a producer defect: honouring it would either duplicate an event or
    rewrite committed history, and both make the sequence an unreliable record.
    """


class SequenceFencedError(Exception):
    """The reporting attempt is not the task's current attempt.

    Late output from a replaced worker must not alter an outcome (invariant
    LC-10), so an append from a superseded generation or a stale
    ``runtime_attempt_id`` is refused rather than appended out of band.
    """


@dataclass(frozen=True)
class TaskRecord:
    """The ``TASK#<task_id>/META`` row, as this surface needs to read it.

    ``owner_principal_id`` and ``tenant_id`` are the authorization inputs and are
    server-owned: nothing a caller sends can set them. ``execution_health`` and
    ``runtime_attempt_id`` are nullable/unknown-capable on purpose — a lost
    heartbeat is neither an exit nor a completion (invariant LC-04), so the
    honest value is ``unknown``, never a zero or a success.
    """

    task_id: str
    invocation_id: str
    tenant_id: str
    owner_principal_id: str
    persona: str
    status: str
    version: int
    created_at: str
    updated_at: str
    deadline_at: str
    generation: int = 1
    runtime_attempt_id: str | None = None
    execution_health: str = "healthy"
    recovery_required: bool = False
    external_reference: str | None = None
    result: dict | None = None
    error: dict | None = None
    input_request: dict | None = None
    command_receipts: tuple[dict, ...] = ()
    queue_ack_status: str = "pending"
    #: Highest allocated sequence, 0 before the first event. Kept on the task row
    #: because the allocation and the high-water update are one conditional
    #: write: a sequence that existed without the row knowing it would let a
    #: second writer allocate the same number.
    latest_sequence: int = 0
    #: Oldest sequence still retained. Advances only when retention removes
    #: history, and is what makes an expired cursor answerable with a bound
    #: rather than an empty replay.
    oldest_sequence: int = 0
    #: Total events ever allocated, including any since removed by retention.
    #: Budget is measured against allocation, not against what is still stored,
    #: so pruning history cannot silently grant a task a fresh 10,000 events.
    events_allocated: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    @property
    def latest_event_cursor(self) -> str | None:
        return format_cursor(self.task_id, self.latest_sequence) if self.latest_sequence else None

    @property
    def oldest_event_cursor(self) -> str | None:
        return format_cursor(self.task_id, self.oldest_sequence) if self.oldest_sequence else None


@dataclass(frozen=True)
class ArtifactRecord:
    """The ``TASK_ARTIFACT#<artifact_id>/META`` binding.

    ``task_id`` is nullable because an upload is owned before it is referenced:
    an artifact uploaded and never attached to a task still belongs to its
    uploader and expires unclaimed after 24 hours. Authorizing a download checks
    the task *and* this exact binding, so an artifact ID valid for one task
    cannot be read through another.
    """

    artifact_id: str
    version: int
    tenant_id: str
    owner_principal_id: str
    content_type: str
    content_sha256: str
    content_length: int
    created_at: str
    expires_at: str
    task_id: str | None = None
    storage_key: str = ""


@dataclass(frozen=True)
class AppendResult:
    """What a committed append tells its producer.

    ``replayed`` distinguishes "your report created this event" from "your report
    was already committed and here is its original position". A producer that
    could not tell the difference would have to choose between double-reporting
    and dropping progress after any ambiguous response.
    """

    event: TaskEvent
    replayed: bool


class TaskStore(Protocol):
    """The storage operations this package depends on, and nothing more.

    Kept this small on purpose. A wider interface would invite the routes to
    reach into task mutation that belongs to T1/T3/T4 — admission, dispatch,
    finalization — and the boundary this story must not cross is exactly that.
    """

    def require_policy(self, *, tenant: str, principal: str, persona: str) -> None:
        """Require the current canonical task policy to allow this persona."""

    def resolve_invocation(self, *, tenant: str, principal: str, invocation_id: str) -> tuple[str, int] | None:
        """Resolve a scoped retained run binding, without granting Task access."""

    def load_task(self, *, task_id: str) -> TaskRecord | None:
        """Strongly consistent read of the task row, or None if absent.

        Consistency is not an optimization choice here: this row is an
        authorization input (owner and tenant) and the snapshot's ``version``
        fence. An eventually consistent read could authorize against a revoked
        state or report a version that has already been superseded.
        """

    def append_event(
        self,
        *,
        task_id: str,
        report_id: str | None,
        event_type: str,
        data: dict,
        producer_timestamp: str | None,
        timestamp: str,
        expect_generation: int | None = None,
        expect_runtime_attempt_id: str | None = None,
    ) -> AppendResult:
        """Allocate the next sequence and commit the event in one transaction.

        Must be atomic with the task row's high-water update, must not consume a
        sequence on failure, and must return the original result for a repeated
        ``report_id`` with identical content.
        """

    def read_events(self, *, task_id: str, after_sequence: int, limit: int) -> list[TaskEvent]:
        """Return up to ``limit`` retained events strictly after a sequence, in order."""

    def load_artifact(self, *, artifact_id: str) -> ArtifactRecord | None:
        """Read an artifact binding, or None if absent."""

    def put_artifact(self, *, record: ArtifactRecord, content: bytes) -> ArtifactRecord:
        """Store artifact bytes and their immutable binding."""

    def read_artifact(self, *, record: ArtifactRecord) -> bytes:
        """Return the stored bytes for a binding."""


def _digest(event_type: str, data: dict) -> str:
    """Content digest for report idempotency.

    Sorted-key canonical JSON, so a producer that reserializes its own report
    with different key ordering on retry still matches — the report is the same
    intent, and treating a formatting difference as a conflict would strand a
    legitimate retry. Design section 6 specifies RFC 8785 canonical JSON; for the
    allowlisted, flat, string/number/bool data objects this contract permits,
    ``sort_keys`` with separators is equivalent.
    """
    payload = json.dumps({"type": event_type, "data": data}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class InMemoryTaskStore:
    """A test implementation that enforces the contract's ordering rules.

    This exists so the streaming, replay, authorization and backpressure logic
    can be exercised end to end before T1's DynamoDB store lands. It is not a
    shipping backend and is not registered anywhere in the app; the routes take a
    store by dependency, so the real one substitutes without a route change.

    It deliberately enforces the *rules*, not just the storage: a failed append
    consumes no sequence, a duplicate report replays, a conflicting report
    raises, and a fenced attempt is refused. A permissive double would let the
    routes pass their tests while violating the invariants the contract cares
    about.
    """

    def require_policy(self, *, tenant: str, principal: str, persona: str) -> None:
        """Test backend assumes allowed policy; production reads protected authority."""

    def __init__(self) -> None:
        self.tasks: dict[str, TaskRecord] = {}
        self.events: dict[str, list[TaskEvent]] = {}
        #: ``TASK_REPORT#<task_id>`` → ``REPORT#<generation>#<report_id>`` →
        #: (content digest, allocated sequence).
        self.reports: dict[str, dict[str, tuple[str, int]]] = {}
        self.artifacts: dict[str, ArtifactRecord] = {}
        self.blobs: dict[str, bytes] = {}
        #: Set to raise ``TaskStoreError`` from every operation, so tests can
        #: assert an authorization-dependency outage denies rather than degrades.
        self.fail: bool = False

    # -- task ---------------------------------------------------------------

    def put_task(self, record: TaskRecord) -> None:
        """Seed a task. Test-only: production tasks are created by T2/T3."""
        self.tasks[record.task_id] = record
        self.events.setdefault(record.task_id, [])

    def resolve_invocation(self, *, tenant: str, principal: str, invocation_id: str) -> tuple[str, int] | None:
        if self.fail:
            raise TaskStoreError("task store unavailable")
        matches = [
            record
            for record in self.tasks.values()
            if record.tenant_id == tenant and record.owner_principal_id == principal and record.invocation_id == invocation_id
        ]
        if len(matches) > 1:
            raise TaskStoreError("ambiguous invocation binding")
        return (matches[0].task_id, matches[0].generation) if matches else None

    def load_task(self, *, task_id: str) -> TaskRecord | None:
        if self.fail:
            raise TaskStoreError("task store unavailable")
        return self.tasks.get(task_id)

    def set_status(self, task_id: str, **changes) -> TaskRecord:
        """Apply a state change with its version bump. Test-only."""
        record = self.tasks[task_id]
        updated = replace(record, version=record.version + 1, **changes)
        self.tasks[task_id] = updated
        return updated

    def prune_before(self, task_id: str, sequence: int) -> None:
        """Simulate retention removing history below ``sequence``. Test-only.

        Needed to exercise the expired-cursor path honestly: without real
        removal, a test could only assert the 410 response shape, not that the
        events are actually gone and the oldest bound moved.
        """
        record = self.tasks[task_id]
        self.events[task_id] = [event for event in self.events[task_id] if event.sequence >= sequence]
        self.tasks[task_id] = replace(record, oldest_sequence=sequence)

    # -- events -------------------------------------------------------------

    def append_event(
        self,
        *,
        task_id: str,
        report_id: str | None,
        event_type: str,
        data: dict,
        producer_timestamp: str | None,
        timestamp: str,
        expect_generation: int | None = None,
        expect_runtime_attempt_id: str | None = None,
    ) -> AppendResult:
        if self.fail:
            raise TaskStoreError("task store unavailable")
        record = self.tasks.get(task_id)
        if record is None:
            raise TaskStoreError("task row is absent")

        # Fence first. A superseded attempt must be refused *before* any
        # idempotency or budget bookkeeping, so a fenced producer cannot consume
        # a sequence, reserve a report slot or learn anything about the task's
        # current position.
        if expect_generation is not None and expect_generation != record.generation:
            raise SequenceFencedError("generation is superseded")
        if expect_runtime_attempt_id is not None and expect_runtime_attempt_id != record.runtime_attempt_id:
            raise SequenceFencedError("runtime attempt is not current")

        # The reserved tail is for exactly the events a flood must not crowd out:
        # terminal outcomes, run outcomes and gap reports.
        is_protected = event_type in PROTECTED_EVENT_TYPES
        digest = _digest(event_type, data)
        ledger = self.reports.setdefault(f"TASK_REPORT#{task_id}", {})
        slot = f"REPORT#{record.generation:010d}#{report_id}" if report_id else None

        if slot is not None and slot in ledger:
            previous_digest, sequence = ledger[slot]
            if previous_digest != digest:
                raise ReportConflictError("report id was committed with different content")
            existing = next((event for event in self.events[task_id] if event.sequence == sequence), None)
            if existing is None:
                # Committed, then aged out of retention. The original sequence is
                # still the truthful answer: re-allocating would duplicate an
                # event the client may already have deduplicated against.
                raise ReportConflictError("report was committed but its event is no longer retained")
            return AppendResult(event=existing, replayed=True)

        budget = MAX_EVENTS_PER_TASK if is_protected else MAX_EVENTS_PER_TASK - RESERVED_TERMINAL_EVENT_SLOTS
        if record.events_allocated >= budget:
            raise EventBudgetExhaustedError("event budget exhausted")

        sequence = record.latest_sequence + 1
        event = TaskEvent(
            task_id=task_id,
            invocation_id=record.invocation_id,
            generation=record.generation,
            runtime_attempt_id=record.runtime_attempt_id,
            sequence=sequence,
            type=event_type,
            timestamp=timestamp,
            data=dict(data),
            producer_timestamp=producer_timestamp,
        )
        self.events[task_id].append(event)
        if slot is not None:
            ledger[slot] = (digest, sequence)
        self.tasks[task_id] = replace(
            record,
            latest_sequence=sequence,
            oldest_sequence=record.oldest_sequence or sequence,
            events_allocated=record.events_allocated + 1,
            updated_at=timestamp,
        )
        return AppendResult(event=event, replayed=False)

    def read_events(self, *, task_id: str, after_sequence: int, limit: int) -> list[TaskEvent]:
        if self.fail:
            raise TaskStoreError("task store unavailable")
        return [event for event in self.events.get(task_id, []) if event.sequence > after_sequence][:limit]

    # -- artifacts ----------------------------------------------------------

    def load_artifact(self, *, artifact_id: str) -> ArtifactRecord | None:
        if self.fail:
            raise TaskStoreError("task store unavailable")
        return self.artifacts.get(artifact_id)

    def put_artifact(self, *, record: ArtifactRecord, content: bytes) -> ArtifactRecord:
        if self.fail:
            raise TaskStoreError("task store unavailable")
        self.artifacts[record.artifact_id] = record
        self.blobs[record.storage_key] = content
        return record

    def read_artifact(self, *, record: ArtifactRecord) -> bytes:
        if self.fail:
            raise TaskStoreError("task store unavailable")
        if record.storage_key not in self.blobs:
            raise TaskStoreError("artifact bytes are absent")
        return self.blobs[record.storage_key]
