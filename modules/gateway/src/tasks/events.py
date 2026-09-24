"""Event ordering, cursors and SSE frame encoding.

Everything the streaming surface promises is arithmetic on one number: the
per-task ``sequence``. Replay position, deduplication and gap detection are all
derived from it, so a cursor that can disagree with the event it labels makes a
reconnecting client resume in the wrong place *and* makes that loss invisible —
the stream still looks continuous. The contract ships deliberately-broken
fixtures for exactly this shape (``event-cursor-foreign-task``,
``event-cursor-mismatched-sequence``), so parsing and formatting live here, in
one place, with the task binding checked rather than assumed.

Design reference: implementation-design.md section 9; contract
``events.schema.json``, ``common.schema.json#/$defs/event_cursor``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

SCHEMA_VERSION = "1.0"

#: Same expression as ``common.schema.json#/$defs/task_id``. Duplicated rather
#: than derived because a looser local pattern is how a foreign or malformed task
#: handle reaches storage; the contract test asserts the two agree.
TASK_ID_PATTERN = re.compile(r"^tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
UUID4_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
CURSOR_PATTERN = re.compile(r"^(tsk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}):([1-9][0-9]*)$")

#: The closed event-kind enum from ``events.schema.json#/$defs/event_type``. A
#: report naming a kind outside this set is refused; the set is never widened by
#: a caller-supplied value.
EVENT_TYPES = (
    "task.accepted",
    "task.queued",
    "run.started",
    "progress.updated",
    "artifact.created",
    "input.required",
    "input.accepted",
    "input.consumed",
    "input.rejected",
    "command.updated",
    "cancel.requested",
    "run.completed",
    "run.failed",
    "task.completed",
    "task.failed",
    "task.cancelled",
    "history.gap",
)

#: Events that close an SSE stream. Only the *task* reaching a terminal status
#: ends the stream, and the set is deliberately narrower than it first looks:
#:
#: * ``run.completed``/``run.failed`` describe one attempt. A failed run may be
#:   followed by a recovery generation, so closing on it would disconnect a client
#:   moments before the work it is watching resumes.
#: * ``history.gap`` is a mid-stream discontinuity report. Closing on it would
#:   turn "you are missing events 6-7, here is the rest" into "you are missing
#:   events 6-7, goodbye" — the client would lose the remaining live history as a
#:   consequence of being told about a small gap.
#:
#: Design section 9: "Close terminal streams only after all committed terminal
#: events have been emitted."
STREAM_CLOSING_EVENT_TYPES = frozenset({"task.completed", "task.failed", "task.cancelled"})

#: Terminal and control evidence. These may consume the reserved tail of the
#: per-task event budget, and a bounded subscriber buffer may never drop them
#: (design section 9: "Reserve terminal/error event capacity; terminal evidence
#: cannot be dropped").
#:
#: Wider than the stream-closing set on purpose. A gap report and a run outcome
#: are exactly the records that must survive a flood of progress, because they are
#: the evidence that something went wrong — dropping them under pressure would
#: leave a client with a clean-looking stream and no indication of loss, which is
#: the failure T6-AC04 requires to be explicit.
PROTECTED_EVENT_TYPES = STREAM_CLOSING_EVENT_TYPES | frozenset({"run.completed", "run.failed", "history.gap"})

#: Data keys permitted by ``events.schema.json#/$defs/event_data``. The object is
#: `additionalProperties: false` in the contract, so an unknown key is a refusal
#: rather than a passthrough — that is what keeps private model reasoning and
#: free-form diagnostic text out of a durable, externally readable record.
EVENT_DATA_KEYS = frozenset(
    {
        "status",
        "version",
        "message",
        "stage",
        "command_id",
        "command_status",
        "handoff",
        "turn_number",
        "turn_id",
        "input_request_id",
        "prompt",
        "artifact_id",
        "content_sha256",
        "dispatch_id",
        "outcome",
        "error_code",
        "reason",
        "execution_health",
        "recovery_required",
        "queue_ack_status",
        "omitted_from_cursor",
        "omitted_to_cursor",
        "omitted_report_ids",
    }
)

#: Per-kind required data keys, mirroring the conditional ``allOf`` branches of
#: ``events.schema.json#/$defs/event``. Held as data so the report route and the
#: emitter enforce one rule rather than two drifting copies.
REQUIRED_EVENT_DATA: dict[str, tuple[str, ...]] = {
    "task.accepted": ("status", "version"),
    "task.queued": ("status", "version"),
    "run.started": ("status", "version"),
    "progress.updated": ("message", "stage"),
    "artifact.created": ("artifact_id", "content_sha256"),
    "input.required": ("input_request_id", "prompt"),
    "input.accepted": ("command_id", "command_status", "handoff"),
    "input.rejected": ("command_id", "command_status", "handoff"),
    "command.updated": ("command_id", "command_status", "handoff"),
    "input.consumed": ("command_id", "command_status", "handoff", "turn_id", "turn_number"),
    "cancel.requested": ("command_id", "status", "version"),
    "run.completed": ("outcome",),
    "run.failed": ("outcome", "error_code"),
    "task.completed": ("status", "version", "outcome"),
    "task.failed": ("status", "version", "outcome", "error_code"),
    "task.cancelled": ("status", "version", "outcome"),
    "history.gap": ("omitted_from_cursor", "omitted_to_cursor", "omitted_report_ids", "reason"),
}

#: Closed value sets from ``events.schema.json#/$defs/event_data``, resolved through
#: the ``$ref``s in ``common.schema.json`` and ``results.schema.json``. Enforced
#: because presence checks are not enough: an event whose *keys* are all permitted
#: but whose ``stage`` is a word the contract does not define is a durable record
#: that fails validation for every consumer who checks it against the schema —
#: including the SSE clients this surface exists to serve. A producer inventing its
#: own stage vocabulary would be discovered only when someone validated the stream,
#: which is exactly the drift the frozen contract is meant to prevent.
#:
#: Mirrored here rather than read from ``docs/`` at runtime for the same reason
#: ``limits.py`` mirrors ``limits.json``: the gateway image does not contain the
#: contract tree, so a runtime read would pass in a test run and raise in the pod.
#: ``test_event_data_matches_contract.py`` asserts the mirror both ways — equal
#: values, and no enum in the schema left unmirrored — because the duplication is
#: only defensible while it is checked.
EVENT_DATA_ENUMS: dict[str, frozenset[str]] = {
    "status": frozenset({"accepted", "queued", "running", "waiting_for_input", "cancel_requested", "completed", "failed", "cancelled"}),
    "stage": frozenset({"evidence_inventory", "analysis", "clarification", "synthesis"}),
    "command_status": frozenset({"accepted", "consumed", "rejected", "cancelled"}),
    "handoff": frozenset({"not_started", "prepared", "sent", "confirmed", "unknown"}),
    "outcome": frozenset({"completed", "failed", "cancelled"}),
    "execution_health": frozenset({"healthy", "unknown", "stopped"}),
    "queue_ack_status": frozenset({"pending", "confirmed", "unknown"}),
    "error_code": frozenset(
        {
            "admission_recovery_exhausted",
            "deadline_exceeded",
            "model_outcome_unknown",
            "model_access_denied",
            "budget_exceeded",
            "authority_revoked",
            "invalid_agent_output",
            "protocol_violation",
            "process_failed",
            "recovery_exhausted",
            "cancelled_by_client",
            "cancellation_stop_unconfirmed",
            "event_budget_exhausted",
            "storage_unavailable",
        }
    ),
}

#: Data keys the contract constrains as positive integers
#: (``common.schema.json#/$defs/task_version`` and ``turn_number``). A zero or
#: negative one is a fence value no real record can match, so a client comparing
#: against it would silently never see agreement.
EVENT_DATA_POSITIVE_INTEGERS = ("version", "turn_number")

#: Data keys the contract constrains as 1..4000-character strings. An over-long
#: value that reached a durable event would be unreadable by a validating consumer
#: forever, and a committed event cannot be withdrawn.
EVENT_DATA_BOUNDED_STRINGS = ("message", "prompt")

#: Fixed values the contract pins per kind. A host reporting
#: ``task.completed`` with ``outcome: "failed"`` is describing two different
#: outcomes in one record; refusing it here keeps the durable history internally
#: consistent instead of relying on every reader to notice.
FIXED_EVENT_DATA: dict[str, dict[str, str]] = {
    "run.completed": {"outcome": "completed"},
    "run.failed": {"outcome": "failed"},
    "task.completed": {"status": "completed", "outcome": "completed"},
    "task.failed": {"status": "failed", "outcome": "failed"},
    "task.cancelled": {"status": "cancelled", "outcome": "cancelled"},
}


class CursorError(ValueError):
    """A cursor that is malformed, or that names a different task.

    Distinct from "a cursor the task has outgrown": a syntactically valid cursor
    beyond the current high-water mark, or one belonging to another task, is a
    client mistake answered with ``400 invalid_cursor``, while an expired cursor
    is answered with ``410 history_expired`` and the retained bounds. Collapsing
    the two would tell a client to retry a request that can never succeed.
    """


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_timestamp(moment: datetime) -> str:
    """Render an RFC3339 UTC timestamp in the exact contract shape.

    ``common.schema.json#/$defs/timestamp`` fixes a trailing ``Z`` with optional
    microseconds, so ``datetime.isoformat()`` output (``+00:00``) does not
    validate. Seconds resolution is used because every consumer of these
    timestamps compares them as instants, and a fixed width keeps the emitted
    fixtures byte-comparable with the contract's own.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_cursor(task_id: str, sequence: int) -> str:
    """Build ``<task_id>:<sequence>``, refusing anything the contract rejects.

    Validating on the way *out* matters as much as on the way in: the cursor is
    the client's only durable position marker, so emitting one that the contract
    pattern would reject hands out a token the client can never successfully
    replay with.
    """
    if not TASK_ID_PATTERN.match(task_id):
        raise CursorError("task id is not a Task API v1 handle")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise CursorError("sequence must be a positive integer")
    return f"{task_id}:{sequence}"


def parse_cursor(cursor: str, *, task_id: str) -> int:
    """Return the sequence a cursor names, or raise if it is not this task's.

    The task binding is the security-relevant half. A cursor confers no read
    authority (``common.schema.json#/$defs/event_cursor``), so a cursor naming
    another task must not be silently reinterpreted against the task the caller
    *is* authorized for — that would let a stale client resume at an arbitrary
    position in a stream it never read.
    """
    match = CURSOR_PATTERN.match(cursor or "")
    if not match:
        raise CursorError("cursor is not <task_id>:<sequence>")
    if match.group(1) != task_id:
        raise CursorError("cursor names a different task")
    return int(match.group(2))


@dataclass(frozen=True)
class TaskEvent:
    """One durable, ordered progress record.

    Frozen because a committed event is immutable evidence: its sequence has
    been allocated, it may already have been delivered to a subscriber, and a
    client has deduplicated on its ``event_id``. Mutating one after the fact
    would make two readers disagree about what the task reported.
    """

    task_id: str
    invocation_id: str
    generation: int
    runtime_attempt_id: str | None
    sequence: int
    type: str
    timestamp: str
    data: dict = field(default_factory=dict)
    producer_timestamp: str | None = None

    @property
    def event_id(self) -> str:
        return format_cursor(self.task_id, self.sequence)

    @property
    def closes_stream(self) -> bool:
        """Whether emitting this event ends the stream. See ``STREAM_CLOSING_EVENT_TYPES``."""
        return self.type in STREAM_CLOSING_EVENT_TYPES

    @property
    def is_protected(self) -> bool:
        """Whether a bounded buffer must never drop this event."""
        return self.type in PROTECTED_EVENT_TYPES

    def to_contract(self) -> dict:
        """Serialize to ``events.schema.json#/$defs/event``.

        ``producer_timestamp`` is omitted rather than sent as null when absent.
        The contract permits null, but the two-marker latency threshold is
        measured at emitter *and* receiver, so an explicit null would assert "the
        producer supplied no time" where the truthful statement is that this
        event was authored by the gateway itself and has no producer.
        """
        event = {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "invocation_id": self.invocation_id,
            "generation": self.generation,
            "runtime_attempt_id": self.runtime_attempt_id,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "type": self.type,
            "timestamp": self.timestamp,
            "data": dict(self.data),
        }
        if self.producer_timestamp is not None:
            event["producer_timestamp"] = self.producer_timestamp
        return event


def validate_event_data(event_type: str, data: dict) -> None:
    """Enforce the kind/data agreement the contract encodes conditionally.

    Raises ``ValueError`` with a message safe to return to an authenticated
    internal caller: it names the offending key or kind and never echoes a
    value, because report data is untrusted content that may carry task text.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError("unknown event type")
    if not isinstance(data, dict):
        raise ValueError("event data must be an object")
    unknown = sorted(set(data) - EVENT_DATA_KEYS)
    if unknown:
        raise ValueError(f"event data keys not permitted: {', '.join(unknown)}")
    missing = [key for key in REQUIRED_EVENT_DATA.get(event_type, ()) if key not in data]
    if missing:
        raise ValueError(f"event data for {event_type} requires: {', '.join(missing)}")

    # Values, not just keys. A permitted key holding an undefined value produces a
    # durable event that no schema-checking consumer can accept; refusing it at the
    # boundary keeps the contract's closed vocabularies actually closed.
    for key, permitted in EVENT_DATA_ENUMS.items():
        if key in data and data[key] not in permitted:
            raise ValueError(f"event data {key} is not one of the values the contract defines")
    for key in EVENT_DATA_POSITIVE_INTEGERS:
        value = data.get(key)
        # ``bool`` is excluded explicitly because it is a subclass of ``int``, so
        # ``True`` would otherwise pass as the integer 1.
        if key in data and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
            raise ValueError(f"event data {key} must be a positive integer")
    for key in EVENT_DATA_BOUNDED_STRINGS:
        value = data.get(key)
        if key in data and (not isinstance(value, str) or not 1 <= len(value) <= 4000):
            raise ValueError(f"event data {key} must be a string of 1 to 4000 characters")

    for key, expected in FIXED_EVENT_DATA.get(event_type, {}).items():
        if data.get(key) != expected:
            raise ValueError(f"event data {key} for {event_type} must be {expected}")


def snapshot_frame(*, task_id: str, version: int, status: str, high_water_cursor: str | None, oldest_event_cursor: str | None) -> dict:
    """Build the opening SSE frame: current state, deliberately not a position.

    ``advances_last_event_id`` is emitted explicitly as ``false`` even though the
    contract fixes the value. It is the one field in this surface whose omission
    is silently harmful: a client that treated a snapshot as a cursor would, on
    the next reconnect, resume *past* the events that arrived between the
    snapshot being built and the connection dropping — and because the stream
    looked continuous, the loss would be undetectable. Stating it in the wire
    format makes the rule checkable by the consumer rather than assumed.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "frame": "snapshot",
        "task_id": task_id,
        "version": version,
        "status": status,
        "high_water_cursor": high_water_cursor,
        "oldest_event_cursor": oldest_event_cursor,
        "advances_last_event_id": False,
    }


def heartbeat_frame(moment: datetime) -> dict:
    """Build the 15-second keepalive payload.

    ``counts_as_progress`` is fixed false for the same reason as above: an open
    socket is not evidence the task is advancing. T6-AC01 fails if heartbeat
    traffic alone is presented as progress, so the frame says so about itself.
    """
    return {"frame": "heartbeat", "timestamp": format_timestamp(moment), "counts_as_progress": False}


def _encode(*, event: str, data: dict, event_id: str | None = None) -> bytes:
    """Encode one SSE frame.

    ``id:`` is written only when an actual durable event is being delivered.
    Omitting it for snapshots is what implements the no-advance rule at the
    transport level: a browser or any conforming SSE client sets Last-Event-ID
    from the ``id`` field, so a snapshot that carried one would move the
    client's position no matter what the payload claimed about itself.
    """
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, separators=(',', ':'), sort_keys=True)}")
    return ("\n".join(lines) + "\n\n").encode()


def encode_snapshot(frame: dict) -> bytes:
    return _encode(event="snapshot", data=frame)


def encode_event(event: TaskEvent) -> bytes:
    return _encode(event="event", data=event.to_contract(), event_id=event.event_id)


def encode_heartbeat(frame: dict) -> bytes:
    """Encode the heartbeat as an SSE *comment*, per design section 9.

    A comment line cannot carry an ``id`` and is discarded by conforming
    clients, which makes it structurally incapable of advancing Last-Event-ID or
    being mistaken for an event. The JSON payload rides in the comment so a
    diagnostic consumer can still read the timestamp.
    """
    return f": {json.dumps(frame, separators=(',', ':'), sort_keys=True)}\n\n".encode()
