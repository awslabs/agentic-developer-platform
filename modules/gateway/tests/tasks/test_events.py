"""Cursor handling, event serialization and SSE framing.

These are the properties everything else in the streaming surface rests on. A
cursor that can disagree with the event it labels makes a reconnecting client
resume in the wrong place *and* makes that loss invisible, because the stream
still looks continuous. So the tests here are mostly about refusals: what the
cursor helpers will not build and will not accept.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src.tasks import events

TASK = "tsk_3d5f8a10-2b4c-4e6f-9a81-7c3e5d9f1b20"
OTHER_TASK = "tsk_9f1b2044-5c6d-4e7f-8a91-2b3c4d5e6f70"
ATTEMPT = "a1b2c3d4-e5f6-4718-9a2b-3c4d5e6f7081"
INVOCATION = "5e7a9c31-4d6f-4813-ba25-9c1e3f5a7d40"


def make_event(**overrides) -> events.TaskEvent:
    base = {
        "task_id": TASK,
        "invocation_id": INVOCATION,
        "generation": 1,
        "runtime_attempt_id": ATTEMPT,
        "sequence": 5,
        "type": "progress.updated",
        "timestamp": "2026-09-24T14:43:20Z",
        "data": {"message": "Analyzing the failing test", "stage": "analysis"},
    }
    return events.TaskEvent(**{**base, **overrides})


# -- cursors ---------------------------------------------------------------


def test_cursor_round_trips() -> None:
    assert events.parse_cursor(events.format_cursor(TASK, 42), task_id=TASK) == 42


def test_format_cursor_refuses_a_non_task_handle() -> None:
    """Validating on the way out matters as much as on the way in.

    A cursor is the client's only durable position marker. Emitting one the
    contract pattern rejects hands out a token the client can never replay with,
    and the failure would appear on the *next* connection, far from its cause.
    """
    with pytest.raises(events.CursorError):
        events.format_cursor("task-42", 1)


@pytest.mark.parametrize("sequence", [0, -1, True])
def test_format_cursor_refuses_non_positive_sequences(sequence) -> None:
    """Zero is the specific hazard: it is the "no events yet" sentinel.

    ``TaskRecord.latest_sequence`` is 0 before the first event, so a cursor of
    ``...:0`` would be a position marker for a stream that has not started. The
    contract's pattern requires ``[1-9][0-9]*`` for the same reason. ``True`` is
    included because ``bool`` is an ``int`` in Python and ``True == 1``, so an
    accidental boolean would otherwise format as a valid-looking cursor.
    """
    with pytest.raises(events.CursorError):
        events.format_cursor(TASK, sequence)


def test_parse_cursor_refuses_a_cursor_naming_another_task() -> None:
    """The security-relevant half: a cursor confers no read authority.

    A cursor for a task the caller cannot read must not be silently reinterpreted
    as a position in the task they *can* read — that would let a stale or
    malicious client resume at an arbitrary point in a stream it never saw.
    """
    with pytest.raises(events.CursorError):
        events.parse_cursor(events.format_cursor(OTHER_TASK, 3), task_id=TASK)


@pytest.mark.parametrize("cursor", ["", "  ", TASK, f"{TASK}:", f"{TASK}:0", f"{TASK}:abc", f"{TASK}:1:2", f" {TASK}:1"])
def test_parse_cursor_refuses_malformed_input(cursor) -> None:
    with pytest.raises(events.CursorError):
        events.parse_cursor(cursor, task_id=TASK)


def test_parse_cursor_refuses_none() -> None:
    """A missing Last-Event-ID header arrives as None, not as a string.

    This must be an explicit refusal rather than a crash, because the route
    distinguishes "no cursor supplied" (start from the snapshot) from "an
    unparseable cursor was supplied" (400) before it ever calls this.
    """
    with pytest.raises(events.CursorError):
        events.parse_cursor(None, task_id=TASK)


# -- timestamps ------------------------------------------------------------


def test_timestamp_uses_the_contract_z_form() -> None:
    """``isoformat()`` emits ``+00:00``, which the contract pattern rejects."""
    stamped = events.format_timestamp(datetime(2026, 9, 24, 14, 43, 20, tzinfo=UTC))
    assert stamped == "2026-09-24T14:43:20Z"


def test_timestamp_converts_a_non_utc_instant() -> None:
    """A naive or offset datetime must be normalized, not relabelled.

    Stamping ``Z`` onto a local time would record an instant that never happened,
    and these timestamps are compared across producer and gateway to measure the
    progress-latency bound.
    """
    from datetime import timedelta, timezone

    moment = datetime(2026, 9, 24, 16, 43, 20, tzinfo=timezone(timedelta(hours=2)))
    assert events.format_timestamp(moment) == "2026-09-24T14:43:20Z"


# -- event serialization ---------------------------------------------------


def test_event_id_is_the_cursor() -> None:
    """One identifier for position and deduplication.

    If the SSE ``id`` and the replay cursor were different strings, a client
    resuming from a Last-Event-ID it captured would have to translate between
    them, and any mismatch would silently skip or repeat events.
    """
    event = make_event(sequence=9)
    assert event.event_id == f"{TASK}:9"


def test_only_task_terminal_kinds_close_the_stream() -> None:
    """The narrow set. Two near-misses are the point of this test.

    A ``run.failed`` may be followed by a recovery generation, so closing on it
    would disconnect a client moments before the work it is watching resumes. A
    ``history.gap`` is a mid-stream discontinuity report, so closing on it would
    turn "you are missing events 6-7, here is the rest" into "you are missing
    events 6-7, goodbye" — losing the remaining live history as a consequence of
    being told about a small gap.
    """
    for kind in ("task.completed", "task.failed", "task.cancelled"):
        assert make_event(type=kind, data={}).closes_stream, kind
    for kind in ("run.completed", "run.failed", "history.gap", "progress.updated"):
        assert not make_event(type=kind, data={}).closes_stream, kind


def test_protected_kinds_are_wider_than_stream_closing_kinds() -> None:
    """A bounded buffer must never drop the evidence that something went wrong.

    Run outcomes and gap reports are exactly what a progress flood would otherwise
    crowd out, and losing them leaves a client with a clean-looking stream and no
    indication of loss — the failure T6-AC04 requires to be explicit.
    """
    for kind in ("task.completed", "task.failed", "task.cancelled", "run.completed", "run.failed", "history.gap"):
        assert make_event(type=kind, data={}).is_protected, kind
    assert not make_event(type="progress.updated").is_protected
    assert events.STREAM_CLOSING_EVENT_TYPES < events.PROTECTED_EVENT_TYPES


def test_producer_timestamp_is_omitted_not_nulled_when_absent() -> None:
    """A gateway-authored event has no producer, which is not the same claim.

    The contract permits null, but an explicit null asserts "the producer supplied
    no time" where the truth is that there was no producer. The distinction
    matters because producer timestamps are the emitter half of the two-marker
    latency measurement.
    """
    assert "producer_timestamp" not in make_event().to_contract()
    assert make_event(producer_timestamp="2026-09-24T14:43:19Z").to_contract()["producer_timestamp"] == "2026-09-24T14:43:19Z"


def test_to_contract_does_not_alias_the_event_data() -> None:
    """A committed event is immutable evidence; a shared dict would leak mutation.

    Two subscribers serialize the same event. If ``to_contract`` handed out the
    same dict object, a caller mutating its copy would change what the other
    subscriber — and any later replay — observes.
    """
    event = make_event()
    body = event.to_contract()
    body["data"]["message"] = "tampered"
    assert event.data["message"] == "Analyzing the failing test"


# -- data validation -------------------------------------------------------


def test_unknown_data_keys_are_refused() -> None:
    """``additionalProperties: false`` is what keeps private reasoning out.

    The durable event record is externally readable. An unrecognized key is a
    refusal rather than a passthrough, so free-form diagnostics and model
    reasoning cannot ride along into a client-visible history.
    """
    with pytest.raises(ValueError, match="not permitted"):
        events.validate_event_data("progress.updated", {"message": "m", "stage": "analysis", "raw_model_reasoning": "..."})


def test_missing_required_keys_are_refused_per_kind() -> None:
    with pytest.raises(ValueError, match="requires"):
        events.validate_event_data("progress.updated", {"message": "only a message"})
    with pytest.raises(ValueError, match="requires"):
        events.validate_event_data("history.gap", {"reason": "retention"})


def test_pinned_values_must_agree_with_the_kind() -> None:
    """A ``task.completed`` carrying a failed outcome describes two outcomes.

    Refusing it here keeps the durable history internally consistent rather than
    relying on every future reader to notice the contradiction.
    """
    with pytest.raises(ValueError, match="outcome"):
        events.validate_event_data("task.completed", {"status": "completed", "version": 3, "outcome": "failed"})


def test_unknown_event_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown event type"):
        events.validate_event_data("task.exploded", {})


#: One contract-legal value per data key, used to build a valid example of every
#: declared kind. Enum-constrained keys draw from the enum itself so this table
#: cannot drift out of agreement with it.
SAMPLE_DATA = {
    "version": 3,
    "turn_number": 1,
    "message": "Correlating 503 responses against pool acquisition timeouts.",
    "stage": "analysis",
    "command_id": "cmd_1",
    "turn_id": "turn_1",
    "input_request_id": "req_1",
    "prompt": "Which environment?",
    "artifact_id": "art_7c1e4d92-5a6b-4c8d-9e01-2f3a4b5c6d70",
    "content_sha256": "a" * 64,
    "error_code": "runtime_error",
    "reason": "retention",
    "omitted_from_cursor": f"{TASK}:6",
    "omitted_to_cursor": f"{TASK}:7",
    "omitted_report_ids": [],
}


def sample_for(kind: str, key: str):
    """A value the contract permits for ``key`` on ``kind``."""
    if key in events.FIXED_EVENT_DATA.get(kind, {}):
        return events.FIXED_EVENT_DATA[kind][key]
    if key in events.EVENT_DATA_ENUMS:
        return sorted(events.EVENT_DATA_ENUMS[key])[0]
    return SAMPLE_DATA[key]


def test_every_declared_kind_has_a_validatable_example() -> None:
    """No kind may be unreachable through its own validator.

    Guards against a required-key table that contradicts the enum: if a kind
    listed in ``EVENT_TYPES`` required a key outside ``EVENT_DATA_KEYS``, that
    kind could never be reported at all, and the failure would only appear the
    first time a producer tried to emit it.

    The example is built from values the contract actually permits, not from
    placeholders. Placeholders would pass a validator that only checked which keys
    were present, so they would hide exactly the class of bug this asserts against:
    a kind whose required keys cannot be satisfied with any legal value.
    """
    for kind in events.EVENT_TYPES:
        required = events.REQUIRED_EVENT_DATA.get(kind, ())
        assert set(required) <= events.EVENT_DATA_KEYS, kind
        events.validate_event_data(kind, {key: sample_for(kind, key) for key in required})


@pytest.mark.parametrize("key", sorted(events.EVENT_DATA_ENUMS))
def test_closed_vocabularies_refuse_an_undefined_value(key: str) -> None:
    """A permitted key holding an invented value is still a refusal.

    Every one of these keys is an enum in ``events.schema.json``. Accepting a value
    outside it would commit a durable event that no schema-checking consumer can
    read — including the SSE clients this surface exists to serve — and the drift
    would surface only when someone validated the stream. Parametrized over the whole
    table so a key added without its value set cannot pass unnoticed.
    """
    with pytest.raises(ValueError, match=f"event data {key} is not one of"):
        events.validate_event_data("progress.updated", {"message": "m", "stage": "analysis", key: "definitely-not-in-the-enum"})


@pytest.mark.parametrize("value", [0, -1, "3", 1.5, True], ids=["zero", "negative", "string", "float", "bool"])
def test_positive_integer_fields_refuse_non_positive_integers(value) -> None:
    """``version`` is a positive integer; a zero or a numeric string is not one.

    ``version`` participates in optimistic concurrency, so a zero or a string that
    merely looks numeric is a fence value no real record can ever match — a client
    comparing against it would silently never see agreement. ``True`` is included
    because it is an ``int`` in Python and passes a naive ``isinstance`` check.
    """
    with pytest.raises(ValueError, match="version must be a positive integer"):
        events.validate_event_data("task.accepted", {"status": "accepted", "version": value})


def test_message_length_is_bounded_at_the_contract_limit() -> None:
    """4000 characters is the contract's bound, and an empty message is not a message.

    The bound is enforced here rather than at storage because an over-long message
    that reached a durable event would be unreadable by a validating consumer
    forever, and there is no way to withdraw a committed event.
    """
    events.validate_event_data("progress.updated", {"message": "m" * 4000, "stage": "analysis"})
    with pytest.raises(ValueError, match="1 to 4000 characters"):
        events.validate_event_data("progress.updated", {"message": "m" * 4001, "stage": "analysis"})
    with pytest.raises(ValueError, match="1 to 4000 characters"):
        events.validate_event_data("progress.updated", {"message": "", "stage": "analysis"})


# -- SSE framing -----------------------------------------------------------


def _parse_frame(raw: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in raw.decode().strip().split("\n"):
        name, _, value = line.partition(": ")
        fields[name] = value
    return fields


def test_event_frames_carry_an_id() -> None:
    fields = _parse_frame(events.encode_event(make_event(sequence=7)))
    assert fields["id"] == f"{TASK}:7"
    assert fields["event"] == "event"
    assert json.loads(fields["data"])["sequence"] == 7


def test_snapshot_frames_carry_no_id() -> None:
    """The no-advance rule, enforced by the transport rather than the payload.

    A conforming SSE client sets Last-Event-ID from the ``id`` field and ignores
    the body. A snapshot that carried an ``id`` would move the client's position
    no matter what ``advances_last_event_id`` claimed — and on the next reconnect
    the client would resume *past* events that arrived while the snapshot was
    being built, with the stream still looking continuous.
    """
    frame = events.snapshot_frame(task_id=TASK, version=4, status="running", high_water_cursor=f"{TASK}:5", oldest_event_cursor=f"{TASK}:1")
    raw = events.encode_snapshot(frame)
    assert b"\nid:" not in b"\n" + raw
    assert frame["advances_last_event_id"] is False
    assert _parse_frame(raw)["event"] == "snapshot"


def test_heartbeats_are_comments_and_cannot_advance_position() -> None:
    """A comment line is structurally incapable of carrying an ``id``.

    T6-AC01 fails if heartbeat traffic alone is presented as progress, so the
    keepalive is a frame a conforming client discards rather than an event it
    could count or resume from.
    """
    raw = events.encode_heartbeat(events.heartbeat_frame(datetime(2026, 9, 24, 14, 43, 20, tzinfo=UTC)))
    assert raw.startswith(b": ")
    assert b"id:" not in raw
    assert b"event:" not in raw
    payload = json.loads(raw.decode()[2:].strip())
    assert payload["counts_as_progress"] is False


def test_frames_are_terminated_for_dispatch() -> None:
    """A frame without its blank line is buffered, not delivered.

    SSE dispatches on a blank line. Omitting it is the classic way a "streaming"
    endpoint appears to work in a test that reads the whole body and delivers
    nothing incrementally to a live client — which is precisely the
    buffered-final-output failure T6-AC01 rejects.
    """
    assert events.encode_event(make_event()).endswith(b"\n\n")
    assert events.encode_snapshot(
        events.snapshot_frame(task_id=TASK, version=1, status="queued", high_water_cursor=None, oldest_event_cursor=None)
    ).endswith(b"\n\n")
    assert events.encode_heartbeat(events.heartbeat_frame(events.utc_now())).endswith(b"\n\n")


def test_frame_data_is_single_line_json() -> None:
    """A newline inside ``data:`` would split one frame into two.

    Progress messages are producer-authored text. If a message containing a
    newline were emitted raw, the remainder would be parsed as a separate SSE
    field — a content-dependent framing break. ``json.dumps`` escapes it.
    """
    raw = events.encode_event(make_event(data={"message": "line one\nline two", "stage": "analysis"}))
    assert len([line for line in raw.decode().strip().split("\n") if line.startswith("data: ")]) == 1
