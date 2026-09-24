"""The SSE loop: snapshot handoff, replay, heartbeats, backpressure, revocation.

The centrepiece is ``test_reconnect_and_replay_trace``, which walks the seven
steps of ``docs/task-api/contracts/v1/traces/reconnect-and-replay.json`` in order.
The rest cover the individual guarantees that trace depends on.

Time is injected throughout. Verifying a 15-second heartbeat, a 15-second
authorization recheck and a 10-minute window against the real clock would mean a
suite that takes ten minutes and still only proves the timers fire *eventually*.
With a fake clock the tests assert the property the criteria actually state — that
a recheck happens within the bound, that heartbeats do not advance position — in
milliseconds.
"""

from __future__ import annotations

import json

import pytest

from src.tasks import errors, streaming
from src.tasks.events import format_cursor
from src.tasks.limits import (
    SSE_CONNECTION_WINDOW_SECONDS,
    SSE_EVENT_PAGE_SIZE,
    SSE_HEARTBEAT_INTERVAL_SECONDS,
    STREAM_AUTHORIZATION_RECHECK_SECONDS,
)
from src.tasks.read_store import InMemoryTaskStore, TaskRecord, TaskStoreError

TASK = "tsk_3d5f8a10-2b4c-4e6f-9a81-7c3e5d9f1b20"
INVOCATION = "5e7a9c31-4d6f-4813-ba25-9c1e3f5a7d40"
ATTEMPT = "a1b2c3d4-e5f6-4718-9a2b-3c4d5e6f7081"
NOW = "2026-09-24T14:43:20Z"


class FakeClock(streaming.Clock):
    """A clock that only advances when the loop sleeps.

    Deliberately not a clock that advances on every call to ``monotonic()``: the
    loop reads the time several times per iteration, and a self-advancing clock
    would make the number of reads observable as elapsed time. Tests would then
    pass or fail on an implementation detail rather than on the timing rule.
    """

    def __init__(self) -> None:
        super().__init__()
        self.t = 0.0
        self.sleeps = 0
        from datetime import UTC, datetime

        self.now = lambda: datetime(2026, 9, 24, 14, 43, 20, tzinfo=UTC)

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.sleeps += 1

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_store(**overrides) -> InMemoryTaskStore:
    store = InMemoryTaskStore()
    base = {
        "task_id": TASK,
        "invocation_id": INVOCATION,
        "tenant_id": "org-alpha",
        "owner_principal_id": "svc-alpha",
        "persona": "agent-task-investigator",
        "status": "running",
        "version": 4,
        "created_at": "2026-09-24T14:42:03Z",
        "updated_at": NOW,
        "deadline_at": "2026-09-24T15:12:03Z",
        "runtime_attempt_id": ATTEMPT,
    }
    store.put_task(TaskRecord(**{**base, **overrides}))
    return store


def emit(store: InMemoryTaskStore, count: int = 1, *, event_type="progress.updated", data=None) -> None:
    for index in range(count):
        store.append_event(
            task_id=TASK,
            report_id=None,
            event_type=event_type,
            data=data if data is not None else {"message": f"step {index}", "stage": "analysis"},
            producer_timestamp=None,
            timestamp=NOW,
        )


def make_request(store: InMemoryTaskStore, *, after=0, recheck=None) -> streaming.StreamRequest:
    async def default_recheck():
        return store.load_task(task_id=TASK)

    return streaming.StreamRequest(task_id=TASK, after_sequence=after, recheck=recheck or default_recheck)


async def collect(request, store, *, clock=None, outcome=None, max_frames=50, is_disconnected=None):
    """Drive the generator until it stops or yields ``max_frames``.

    A cap is necessary because the live tail is an infinite loop by design — it
    polls forever waiting for a task to end. The cap stands in for a client that
    stops reading.
    """
    clock = clock or FakeClock()
    outcome = outcome or streaming.StreamOutcome(task_id=TASK)
    frames: list[bytes] = []
    generator = streaming.stream_events(request, store, outcome, clock=clock, is_disconnected=is_disconnected)
    async for frame in generator:
        frames.append(frame)
        if len(frames) >= max_frames:
            await generator.aclose()
            break
    return frames, outcome, clock


def parse(frame: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in frame.decode().strip().split("\n"):
        name, _, value = line.partition(": ")
        fields[name] = value
    return fields


def event_frames(frames: list[bytes]) -> list[dict]:
    return [json.loads(parse(f)["data"]) for f in frames if f.startswith(b"id: ")]


def sequences(frames: list[bytes]) -> list[int]:
    return [body["sequence"] for body in event_frames(frames)]


def heartbeats(frames: list[bytes]) -> list[bytes]:
    return [f for f in frames if f.startswith(b": ")]


def is_consecutive(seen: list[int]) -> bool:
    """Whether a sequence list is strictly consecutive.

    Both halves of the contract's "no duplicate and no gap" are checked together
    rather than as two weaker assertions: a duplicate means the client processes an
    event twice, a gap means it silently missed one, and a test that only rules out
    one of them would pass on the other.
    """
    return all(later == earlier + 1 for earlier, later in zip(seen, seen[1:], strict=False))


# -- the contract trace ----------------------------------------------------


async def test_reconnect_and_replay_trace() -> None:
    """Walk ``traces/reconnect-and-replay.json`` step by step.

    The trace's own summary states the property: "the snapshot frame gives a client
    current state without moving its position in history, so a reconnect after a
    snapshot resumes from the last real event rather than skipping whatever arrived
    in between."
    """
    store = make_store()
    emit(store, 12)

    # Step 1: fresh connection, no Last-Event-ID. Opens with a snapshot.
    # Step 2-3: events flow; a keepalive arrives without moving the cursor.
    frames, outcome, _ = await collect(make_request(store, after=11), store, max_frames=2)
    assert parse(frames[0])["event"] == "snapshot"
    assert "id" not in parse(frames[0]), "step 1: the snapshot must not advance Last-Event-ID"
    assert sequences(frames) == [12]
    last_event_id = parse(frames[1])["id"]
    assert last_event_id == f"{TASK}:12"

    # Step 4: the connection drops. Events 13 and 14 commit while nobody is reading.
    emit(store, 2)

    # Step 5: reconnect with Last-Event-ID: ...:12. Replay resumes strictly after
    # it — no duplicate of 12, and no skip of 13.
    resumed, _, _ = await collect(make_request(store, after=12), store, max_frames=3)
    assert parse(resumed[0])["event"] == "snapshot"
    assert "id" not in parse(resumed[0]), "step 5: the reopened snapshot must not advance the cursor either"
    assert sequences(resumed) == [13, 14], "step 5: replay must resume strictly after the supplied cursor"

    # Step 6: a cursor older than retention is refused with the retained bounds
    # rather than answered with a silently-incomplete replay.
    store.prune_before(TASK, 10)
    record = store.load_task(task_id=TASK)
    assert streaming.history_is_expired(record, 3)
    expired = errors.history_expired(
        task_id=TASK,
        current_status=record.status,
        oldest_event_cursor=record.oldest_event_cursor,
        latest_event_cursor=record.latest_event_cursor,
    )
    assert expired.status == 410
    assert expired.details["history_gap"] is True
    assert expired.details["oldest_event_cursor"] == f"{TASK}:10"

    # Step 7: a mid-stream discontinuity is reported as an event, not papered over,
    # and naming the omitted report IDs is the only identity that progress ever
    # had — no sequence was allocated to it.
    gap = streaming.gap_event_data(
        task_id=TASK,
        from_sequence=6,
        to_sequence=7,
        report_ids=["3d852922-4be7-4319-98c7-dfc1b31a5a2f"],
        reason="Progress storage was unavailable for 8 seconds.",
    )
    assert gap["omitted_from_cursor"] == f"{TASK}:6"
    assert gap["omitted_report_ids"] == ["3d852922-4be7-4319-98c7-dfc1b31a5a2f"]

    # The resume cursor advances only for frames whose delivery completed. The
    # trace's step-1 client was cut off while receiving event 12, so its confirmed
    # position is still 11 — see the bookkeeping note in stream_events.
    assert outcome.resume_cursor == f"{TASK}:11"


# -- snapshot and handoff (T6-AC02) ----------------------------------------


async def test_stream_opens_with_a_snapshot_carrying_the_current_bounds() -> None:
    store = make_store()
    emit(store, 3)
    frames, _, _ = await collect(make_request(store, after=3), store, max_frames=1)
    body = json.loads(parse(frames[0])["data"])
    assert body == {
        "schema_version": "1.0",
        "frame": "snapshot",
        "task_id": TASK,
        "version": 4,
        "status": "running",
        "high_water_cursor": f"{TASK}:3",
        "oldest_event_cursor": f"{TASK}:1",
        "advances_last_event_id": False,
    }


async def test_subscribe_before_start_sees_the_first_events() -> None:
    """A subscriber that arrives before any event exists must not miss the first.

    The snapshot reports null cursors, and the same polling loop then delivers
    event 1 when it commits — there is no separate "live subscription" that could
    have been established after event 1 was written.
    """
    store = make_store(status="queued", version=1)
    frames, _, _ = await collect(make_request(store), store, max_frames=1)
    snapshot = json.loads(parse(frames[0])["data"])
    assert snapshot["high_water_cursor"] is None
    assert snapshot["oldest_event_cursor"] is None

    emit(store, 2)
    frames, _, _ = await collect(make_request(store), store, max_frames=3)
    assert sequences(frames) == [1, 2]


async def test_mid_run_subscriber_replays_retained_history_from_the_start() -> None:
    """No cursor means replay from the oldest retained event, not from now.

    Starting at the live edge would silently deny a late subscriber the history it
    never saw, while presenting a stream that looks complete.
    """
    store = make_store()
    emit(store, 5)
    frames, _, _ = await collect(make_request(store), store, max_frames=6)
    assert sequences(frames) == [1, 2, 3, 4, 5]


async def test_completed_task_replays_full_history_then_closes() -> None:
    """A task that finished before anyone subscribed still yields its evidence."""
    store = make_store()
    emit(store, 2)
    emit(store, 1, event_type="task.completed", data={"status": "completed", "version": 5, "outcome": "completed"})
    store.set_status(TASK, status="completed")

    frames, outcome, _ = await collect(make_request(store), store, max_frames=20)
    assert sequences(frames) == [1, 2, 3]
    assert outcome.reason == streaming.CLOSE_TERMINAL


async def test_restart_mid_history_preserves_retained_events(monkeypatch) -> None:
    """A backend restart loses in-process state, not durable history.

    Simulated by discarding the generator entirely and starting a new one from the
    client's cursor — which is exactly what a client experiences when the pod it
    was connected to is replaced. Because the loop holds no state beyond the
    cursor, the new stream reconstructs the position from durable storage.
    """
    store = make_store()
    emit(store, 4)
    first, _, _ = await collect(make_request(store), store, max_frames=3)
    assert sequences(first) == [1, 2]

    # The "restart": nothing is carried over but the client's Last-Event-ID.
    after_restart, _, _ = await collect(make_request(store, after=2), store, max_frames=3)
    assert sequences(after_restart) == [3, 4]
    assert is_consecutive([*sequences(first), *sequences(after_restart)])


async def test_replay_has_no_duplicates_or_gaps_across_many_reconnects() -> None:
    """Reconnecting repeatedly must produce each event exactly once.

    The strongest available statement of AC02's "no silent handoff gaps": whatever
    the reconnect pattern, concatenating what the client saw yields a strictly
    consecutive run.
    """
    store = make_store()
    emit(store, 9)
    seen: list[int] = []
    cursor = 0
    for _ in range(4):
        frames, _, _ = await collect(make_request(store, after=cursor), store, max_frames=3)
        seen.extend(sequences(frames))
        cursor = seen[-1] if seen else 0
    assert seen == [1, 2, 3, 4, 5, 6, 7, 8, 9][: len(seen)]
    assert is_consecutive(seen)


# -- progress vs heartbeats (T6-AC01) --------------------------------------


async def test_two_authored_events_are_observable_before_the_run_exits() -> None:
    """T6-AC01 directly: two substantive progress events, externally observable.

    The criterion fails on heartbeat-only traffic or buffered final output, so this
    asserts the two events arrive as *separate* frames while the task is still
    running — not batched at the end.
    """
    store = make_store()
    emit(store, 2)
    frames, outcome, _ = await collect(make_request(store), store, max_frames=4)
    bodies = event_frames(frames)
    assert len(bodies) == 2
    assert [b["type"] for b in bodies] == ["progress.updated", "progress.updated"]
    assert [b["data"]["message"] for b in bodies] == ["step 0", "step 1"]
    assert store.load_task(task_id=TASK).status == "running", "the run must still be open"
    assert outcome.events_emitted == 2


async def test_heartbeat_is_sent_only_when_idle_and_never_advances_position() -> None:
    store = make_store()
    clock = FakeClock()
    frames, outcome, _ = await collect(make_request(store), store, clock=clock, max_frames=2)
    beats = heartbeats(frames)
    assert beats, "an idle stream must keep itself alive"
    assert all(b"id:" not in beat for beat in beats)
    assert outcome.last_sequence == 0
    assert outcome.resume_cursor is None, "a heartbeat must not create a resume position"
    payload = json.loads(beats[0].decode()[2:].strip())
    assert payload["counts_as_progress"] is False


async def test_heartbeat_waits_for_the_configured_interval() -> None:
    """A heartbeat every poll would be 15x the specified keepalive traffic.

    Asserted by recording *when* the first heartbeat is yielded, not by breaking
    early and checking none arrived. An earlier version of this test did the
    latter, and it could never fail: the loop only yields when it has something to
    send, so on an idle stream the very next frame after the snapshot is the
    heartbeat — the test would stop exactly when the thing it was looking for
    appeared, and conclude it had not.
    """
    store = make_store()
    clock = FakeClock()
    outcome = streaming.StreamOutcome(task_id=TASK)
    generator = streaming.stream_events(make_request(store), store, outcome, clock=clock)
    first_beat_at = None
    async for frame in generator:
        if frame.startswith(b": "):
            first_beat_at = clock.t
            await generator.aclose()
            break
    assert first_beat_at == SSE_HEARTBEAT_INTERVAL_SECONDS


async def test_an_event_resets_the_heartbeat_timer() -> None:
    """Traffic is traffic: an event already proves the connection is alive.

    Emitting a heartbeat right after an event would be redundant keepalive on a
    demonstrably live socket.
    """
    store = make_store()
    clock = FakeClock()
    emit(store, 1)
    frames, _, _ = await collect(make_request(store), store, clock=clock, max_frames=2)
    assert sequences(frames) == [1]
    assert heartbeats(frames) == []


# -- authorization during a stream (T6-AC03) ------------------------------


async def test_revoked_access_closes_the_stream_within_the_recheck_bound() -> None:
    """A stream is a long-lived read, so authorization is re-checked, not cached.

    Without the recheck, revoking a principal's access would leave any already-open
    stream reading protected events indefinitely — and the 30-second revocation
    bound could not be met at all.
    """
    store = make_store()
    calls = {"n": 0}

    async def recheck():
        calls["n"] += 1
        if calls["n"] > 1:
            raise errors.not_found()
        return store.load_task(task_id=TASK)

    clock = FakeClock()
    outcome = streaming.StreamOutcome(task_id=TASK)
    generator = streaming.stream_events(make_request(store, recheck=recheck), store, outcome, clock=clock)
    frames = []
    async for frame in generator:
        frames.append(frame)
        if len(frames) > 40:
            await generator.aclose()
            break

    assert outcome.reason == streaming.CLOSE_REVOKED
    assert clock.t <= STREAM_AUTHORIZATION_RECHECK_SECONDS + 1, "revocation must be noticed within the recheck interval"


async def test_authorization_is_checked_before_the_snapshot_is_built() -> None:
    """An unauthorized caller must not learn the task's version or status.

    If the snapshot were emitted before the check, a cross-tenant caller would
    receive real state in the first frame and only then be disconnected — the state
    would already have leaked.
    """
    store = make_store()

    async def recheck():
        raise errors.not_found()

    outcome = streaming.StreamOutcome(task_id=TASK)
    generator = streaming.stream_events(make_request(store, recheck=recheck), store, outcome)
    with pytest.raises(errors.TaskApiError) as caught:
        async for _ in generator:
            pytest.fail("no frame may be emitted to an unauthorized caller")
    assert caught.value.status == 404


async def test_storage_outage_at_open_is_a_503_not_an_unhandled_error() -> None:
    """A dependency outage must not reach the caller as a 500.

    A ``TaskStoreError`` escaping the generator would be rendered by the ASGI layer
    as a 500 with no contract body, telling the caller its request was malformed
    when in fact a dependency is down and the request is retryable.
    """
    store = make_store()
    store.fail = True
    outcome = streaming.StreamOutcome(task_id=TASK)
    generator = streaming.stream_events(make_request(store), store, outcome)
    with pytest.raises(errors.TaskApiError) as caught:
        async for _ in generator:
            pytest.fail("no frame may be emitted when storage is unreadable")
    assert caught.value.status == 503
    assert caught.value.code == "prerequisite_unavailable"
    assert caught.value.retry_after_ms is not None, "a retryable refusal must say when to retry"


async def test_storage_outage_mid_stream_closes_rather_than_going_quiet() -> None:
    """An idle-looking stream is indistinguishable from a task that went quiet.

    Holding the socket open while storage is unreadable would present a healthy
    stream while events accumulate unread, which makes the loss invisible.
    """
    store = make_store()
    emit(store, 1)

    reads = {"n": 0}
    real_read = store.read_events

    def failing_read(**kwargs):
        reads["n"] += 1
        if reads["n"] > 1:
            raise TaskStoreError("event storage unavailable")
        return real_read(**kwargs)

    store.read_events = failing_read
    frames, outcome, _ = await collect(make_request(store), store, max_frames=10)
    assert sequences(frames) == [1], "events read before the outage are still delivered"
    assert outcome.reason == streaming.CLOSE_UNAVAILABLE


async def test_authorization_dependency_outage_mid_stream_closes_as_unavailable() -> None:
    """Revocation and "cannot confirm" are different events; only one is retryable.

    Reporting an outage as a revocation would tell a legitimate client its access
    was withdrawn, and a client that believes that has no reason to retry.
    """
    store = make_store()
    calls = {"n": 0}

    async def recheck():
        calls["n"] += 1
        if calls["n"] > 1:
            raise TaskStoreError("directory unavailable")
        return store.load_task(task_id=TASK)

    frames, outcome, _ = await collect(make_request(store, recheck=recheck), store, max_frames=40)
    assert outcome.reason == streaming.CLOSE_UNAVAILABLE


# -- lifecycle and windows -------------------------------------------------


async def test_connection_window_closes_the_stream_for_an_orderly_reconnect() -> None:
    """Closing below API Gateway's ceiling is what makes the drop explainable.

    Left to the platform's 15-minute limit, the client would see an unexplained
    mid-frame disconnect it cannot distinguish from a network fault.
    """
    store = make_store()
    clock = FakeClock()
    outcome = streaming.StreamOutcome(task_id=TASK)
    generator = streaming.stream_events(make_request(store), store, outcome, clock=clock)
    async for _ in generator:
        if clock.t > SSE_CONNECTION_WINDOW_SECONDS + 5:
            await generator.aclose()
            break
    assert outcome.reason == streaming.CLOSE_WINDOW
    assert clock.t >= SSE_CONNECTION_WINDOW_SECONDS


async def test_client_disconnect_is_detected_and_recorded() -> None:
    store = make_store()
    frames, outcome, _ = await collect(make_request(store), store, is_disconnected=lambda: _true(), max_frames=5)
    assert outcome.reason == streaming.CLOSE_CLIENT


async def _true() -> bool:
    return True


async def test_terminal_event_closes_but_a_run_failure_does_not() -> None:
    """A failed run may be followed by a recovery generation.

    Closing on ``run.failed`` would disconnect a client moments before the work it
    is watching resumes, so the stream continues and only a task-terminal event
    ends it.
    """
    store = make_store()
    emit(store, 1, event_type="run.failed", data={"outcome": "failed", "error_code": "provider_timeout"})
    frames, outcome, _ = await collect(make_request(store), store, max_frames=4)
    assert sequences(frames) == [1]
    assert outcome.reason != streaming.CLOSE_TERMINAL


async def test_history_gap_does_not_close_the_stream() -> None:
    """Telling a client about a gap must not also cost it the rest of the stream."""
    store = make_store()
    emit(
        store,
        1,
        event_type="history.gap",
        data=streaming.gap_event_data(task_id=TASK, from_sequence=1, to_sequence=2, report_ids=["r"], reason="storage outage"),
    )
    emit(store, 1)
    frames, outcome, _ = await collect(make_request(store), store, max_frames=4)
    assert sequences(frames) == [1, 2], "the stream must continue past a gap report"
    assert outcome.reason != streaming.CLOSE_TERMINAL


async def test_all_terminal_events_in_a_page_are_emitted_before_closing() -> None:
    """Design section 9: close only after committed terminal events are emitted.

    Returning on the terminal event without draining the page would strand a
    ``task.completed`` behind a ``run.completed`` in the same read, and the client
    would never learn the task's actual outcome.
    """
    store = make_store()
    emit(store, 1, event_type="run.completed", data={"outcome": "completed"})
    emit(store, 1, event_type="task.completed", data={"status": "completed", "version": 5, "outcome": "completed"})
    frames, outcome, _ = await collect(make_request(store), store, max_frames=10)
    assert sequences(frames) == [1, 2]
    assert outcome.reason == streaming.CLOSE_TERMINAL


async def test_a_full_page_is_followed_immediately_without_sleeping() -> None:
    """A long backlog must replay at read speed, not 100 events per second.

    With a 1-second poll and 100-event pages, sleeping between full pages would
    make a 1,000-event replay take ten seconds for no reason — the events are
    already committed and waiting.
    """
    store = make_store()
    emit(store, SSE_EVENT_PAGE_SIZE + 5)
    clock = FakeClock()
    frames, _, _ = await collect(make_request(store), store, clock=clock, max_frames=SSE_EVENT_PAGE_SIZE + 2)
    assert len(sequences(frames)) == SSE_EVENT_PAGE_SIZE + 1
    assert clock.sleeps == 0, "a full page must not be followed by a poll delay"


# -- expired history bounds ------------------------------------------------


def test_history_expiry_boundary_is_not_off_by_one() -> None:
    """The exact boundary decides between a wrong 410 and a skipped event.

    A cursor of ``oldest - 1`` means the next event the client wants *is* the
    oldest retained one, so replay is complete and a 410 would wrongly reject a
    client that can resume perfectly.
    """
    store = make_store()
    emit(store, 10)
    store.prune_before(TASK, 5)
    record = store.load_task(task_id=TASK)

    assert streaming.history_is_expired(record, 3) is True
    assert streaming.history_is_expired(record, 4) is False, "oldest-1 is resumable, not expired"
    assert streaming.history_is_expired(record, 5) is False


def test_a_task_with_no_events_never_reports_expired_history() -> None:
    """There is no history to have lost, so 410 would be a lie.

    A fresh task must be streamable from the start; answering 410 would make a
    just-created task appear to have lost events it never had.
    """
    record = make_store().load_task(task_id=TASK)
    assert streaming.history_is_expired(record, 0) is False


# -- backpressure (T6-AC04) ------------------------------------------------


def test_a_slow_consumer_is_dropped_rather_than_blocking_the_producer() -> None:
    """The choice the design makes, asserted as behaviour.

    Blocking the write would apply backpressure through the stream into the agent's
    execution, letting one slow reader stall the task. Silently dropping frames
    would leave the client's history wrong while still looking complete. So the
    reader is dropped, and its resume position is what makes that recoverable.
    """
    buffer = streaming.BoundedFrameBuffer(max_frames=3, max_bytes=10_000)
    assert all(buffer.offer(b"frame") for _ in range(3))
    assert buffer.offer(b"frame") is False
    assert buffer.overflowed is True


def test_the_byte_bound_is_enforced_independently_of_the_frame_count() -> None:
    """A few enormous frames must overflow as surely as many small ones.

    Counting frames alone would let 99 large progress messages consume unbounded
    memory while reading as "within limits".
    """
    buffer = streaming.BoundedFrameBuffer(max_frames=100, max_bytes=100)
    assert buffer.offer(b"x" * 60) is True
    assert buffer.offer(b"x" * 60) is False


def test_terminal_evidence_is_admitted_past_a_full_buffer() -> None:
    """Design section 9: terminal evidence cannot be dropped.

    Dropping it would defeat the purpose of the disconnect. The client is dropped
    so it can reconnect and replay — but a client that never learns the task ended
    has nothing to reconnect for, and no way to tell a finished task from a stalled
    one.
    """
    buffer = streaming.BoundedFrameBuffer(max_frames=1, max_bytes=10)
    assert buffer.offer(b"progress") is True
    assert buffer.offer(b"progress") is False
    assert buffer.offer(b"task.completed evidence", protected=True) is True
    assert b"task.completed evidence" in buffer.drain()


def test_a_dropped_subscriber_gets_an_accurate_resume_cursor() -> None:
    """The disconnect is only honest if the client can pick up where it left off."""
    assert streaming.resume_cursor_for(TASK, 7) == format_cursor(TASK, 7)


def test_a_subscriber_that_saw_nothing_gets_no_cursor() -> None:
    """``<task_id>:0`` is a token the contract rejects and cannot be replayed."""
    assert streaming.resume_cursor_for(TASK, 0) is None


async def test_a_blocked_write_gives_up_at_the_bound() -> None:
    """A client that opens a connection and never reads must not hold a slot.

    Without the timeout it would occupy a stream slot for the full 10-minute
    window while contributing nothing, which is how a small number of stalled
    consumers exhausts the per-environment cap.
    """
    import asyncio

    async def never_completes(_frame: bytes) -> None:
        await asyncio.sleep(3600)

    assert await streaming.drain_with_timeout([b"frame"], never_completes, timeout=0.01) is False


async def test_a_responsive_write_succeeds() -> None:
    written: list[bytes] = []

    async def write(frame: bytes) -> None:
        written.append(frame)

    assert await streaming.drain_with_timeout([b"a", b"b"], write, timeout=1) is True
    assert written == [b"a", b"b"]
