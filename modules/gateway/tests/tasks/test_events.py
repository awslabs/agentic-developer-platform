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
        "data": {"message": "Analyzing the failing test", "stage": "investigate"},
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


def test_terminal_kinds_are_marked_terminal() -> None:
    for kind in ("task.completed", "task.failed", "task.cancelled", "run.completed", "run.failed", "history.gap"):
        assert make_event(type=kind, data={}).is_terminal, kind
    assert not make_event(type="progress.updated").is_terminal


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
        events.validate_event_data("progress.updated", {"message": "m", "stage": "s", "raw_model_reasoning": "..."})


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


def test_every_declared_kind_has_a_validatable_example() -> None:
    """No kind may be unreachable through its own validator.

    Guards against a required-key table that contradicts the enum: if a kind
    listed in ``EVENT_TYPES`` required a key outside ``EVENT_DATA_KEYS``, that
    kind could never be reported at all, and the failure would only appear the
    first time a producer tried to emit it.
    """
    for kind in events.EVENT_TYPES:
        required = events.REQUIRED_EVENT_DATA.get(kind, ())
        assert set(required) <= events.EVENT_DATA_KEYS, kind
        data = {key: events.FIXED_EVENT_DATA.get(kind, {}).get(key, "x") for key in required}
        events.validate_event_data(kind, data)


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
    raw = events.encode_event(make_event(data={"message": "line one\nline two", "stage": "investigate"}))
    assert len([line for line in raw.decode().strip().split("\n") if line.startswith("data: ")]) == 1
