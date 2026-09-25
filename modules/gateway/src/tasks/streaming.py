"""The SSE delivery loop: snapshot, replay, live tail, and honest failure.

This module is the mechanism behind T6-AC01 through AC04, and it is deliberately
separate from the route that mounts it so the loop can be driven in tests without
a live socket and without real time passing.

The shape of the loop follows design section 9 rather than the more obvious
"subscribe then catch up" design, and the difference is the whole point:

    Use strongly consistent DynamoDB event queries every second for active SSE
    readers, with 100-event pages. [...] The query loop continues from the last
    emitted sequence, including during snapshot catch-up; there is no separate
    ephemeral subscription gap.

There is no in-memory fan-out, no pub/sub relay and no separate "live" path. One
loop polls durable storage from the last emitted sequence, forever. A design with
a live subscription plus a catch-up read has a seam between them, and events that
commit during the handoff fall through it — invisibly, because the stream still
looks continuous. Polling is slower to first byte and strictly more honest; the
contract's 1-second interval accepts that trade.

Four things this loop refuses to do, each corresponding to a failure the
acceptance criteria name explicitly:

* It never emits a frame it has not durably read. Progress is persisted before
  delivery, so a client cannot observe an event that a reconnect would lose.
* It never lets a slow consumer hold up the task. The buffer is bounded and a
  reader that cannot keep up is disconnected with its resume position, because
  blocking the write would apply backpressure straight into the agent's execution.
* It never closes on a non-task-terminal event. See ``STREAM_CLOSING_EVENT_TYPES``.
* It never treats elapsed time or heartbeats as progress.

Design reference: implementation-design.md section 9; ``events.schema.json``;
``traces/reconnect-and-replay.json``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from src.tasks import errors
from src.tasks.events import (
    encode_event,
    encode_heartbeat,
    encode_snapshot,
    format_cursor,
    heartbeat_frame,
    snapshot_frame,
    utc_now,
)
from src.tasks.limits import (
    SSE_BLOCKED_WRITE_DISCONNECT_SECONDS,
    SSE_CONNECTION_WINDOW_SECONDS,
    SSE_EVENT_PAGE_SIZE,
    SSE_HEARTBEAT_INTERVAL_SECONDS,
    SSE_MAX_BUFFERED_BYTES,
    SSE_MAX_BUFFERED_FRAMES,
    SSE_MAX_STREAMS_PER_ENVIRONMENT,
    SSE_MAX_STREAMS_PER_PRINCIPAL,
    SSE_MAX_STREAMS_PER_TASK,
    SSE_POLL_INTERVAL_SECONDS,
    STREAM_AUTHORIZATION_RECHECK_SECONDS,
)
from src.tasks.read_store import TaskRecord, TaskStore, TaskStoreError

logger = logging.getLogger(__name__)

#: Why a stream ended. Recorded and logged rather than inferred, because
#: "the client went away" and "we cut the client off" are different operational
#: events and T6-AC04 requires loss to be explicit rather than reconstructed.
CLOSE_TERMINAL = "terminal"
CLOSE_WINDOW = "connection_window_elapsed"
CLOSE_REVOKED = "authorization_revoked"
CLOSE_UNAVAILABLE = "authorization_unavailable"
CLOSE_SLOW_CONSUMER = "slow_consumer"
CLOSE_CLIENT = "client_disconnected"


@dataclass
class StreamOutcome:
    """Why a stream ended and where a client should resume.

    Mutable and passed in by the caller, because a generator that is abandoned
    mid-iteration (the normal case for a client disconnect) never returns a value.
    The route needs the reason *after* iteration stops, so it is written here as
    the loop goes rather than returned at the end.
    """

    task_id: str
    reason: str = CLOSE_CLIENT
    last_sequence: int = 0
    events_emitted: int = 0

    @property
    def resume_cursor(self) -> str | None:
        """Where a reconnecting client should resume from, if anywhere.

        None before any event is emitted, which is the honest answer rather than a
        convenience: the client holds no durable position yet, and handing it
        ``<task_id>:0`` would be a token the contract's pattern rejects and that it
        could never replay from.
        """
        return resume_cursor_for(self.task_id, self.last_sequence)


class Clock:
    """Injectable time and sleep.

    Extracted for one reason: the properties worth testing here are a 15-second
    heartbeat, a 15-second authorization recheck, a 10-minute window and a
    10-second blocked-write timeout. Verifying those against the real clock would
    mean a test suite that takes ten minutes and still only proves the timers fire
    eventually. With time injected, a test can prove the *ordering* — that a
    recheck happens before the 16th second of a stream — which is the property the
    criterion actually states.
    """

    def __init__(self) -> None:
        self.now = utc_now

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()


@dataclass
class StreamRequest:
    """One subscriber's parameters, resolved before the stream opens.

    ``after_sequence`` is resolved by the route (which knows how to answer an
    expired cursor with 410 and a malformed one with 400) so that by the time this
    object exists, the replay position is known to be valid and retained. The
    generator therefore never has to decide between those two refusals mid-stream,
    where it could only communicate them as a stream that stops.
    """

    task_id: str
    after_sequence: int
    #: Re-reads the task row and re-checks ownership. Returns the current record,
    #: raises ``TaskApiError`` if access is gone. Called at most every 15 seconds.
    recheck: Callable[[], Awaitable[TaskRecord]]


async def stream_events(
    request: StreamRequest,
    store: TaskStore,
    outcome: StreamOutcome,
    *,
    clock: Clock | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[bytes]:
    """Yield SSE frames: snapshot, then everything after the resume position.

    The first frame is always a snapshot and it never carries an ``id``, so a
    client that connects, receives state and immediately drops still resumes from
    its *last real event* — not from the snapshot. That is the cursor discipline
    the reconnect trace exists to pin down: without it, events committed between
    the snapshot being built and the drop are skipped, and the skip is undetectable
    because the stream looked continuous.
    """
    clock = clock or Clock()

    # Translated here rather than left to propagate. A ``TaskStoreError`` escaping
    # the generator would reach the ASGI layer as an unhandled exception and be
    # rendered as a 500 with no contract body, telling a caller its request was
    # malformed when the truth is that a dependency is down and the request is
    # retryable.
    try:
        record = await request.recheck()
    except TaskStoreError:
        logger.warning("Task API stream refused: task storage unavailable", exc_info=True)
        raise errors.prerequisite_unavailable("Task storage is unavailable.") from None

    yield encode_snapshot(
        snapshot_frame(
            task_id=record.task_id,
            version=record.version,
            status=record.status,
            high_water_cursor=record.latest_event_cursor,
            oldest_event_cursor=record.oldest_event_cursor,
        )
    )

    cursor = request.after_sequence
    outcome.last_sequence = cursor
    opened_at = clock.monotonic()
    last_heartbeat = opened_at
    last_recheck = opened_at

    while True:
        if is_disconnected is not None and await is_disconnected():
            outcome.reason = CLOSE_CLIENT
            return

        now = clock.monotonic()

        # The connection window is deliberately below API Gateway's own ceiling so
        # the client observes an orderly close and reconnects, rather than an
        # unexplained mid-frame drop it cannot distinguish from a network fault.
        if now - opened_at >= SSE_CONNECTION_WINDOW_SECONDS:
            outcome.reason = CLOSE_WINDOW
            return

        # Authorization is re-checked on a bounded interval, not once at open. A
        # stream is a long-lived read: without this, revoking access would leave
        # an already-open stream reading protected events indefinitely, and the
        # 30-second revocation bound could not be met.
        if now - last_recheck >= STREAM_AUTHORIZATION_RECHECK_SECONDS:
            try:
                record = await request.recheck()
            except errors.TaskApiError:
                # Access is gone. The stream stops; it does not degrade into
                # emitting a subset.
                outcome.reason = CLOSE_REVOKED
                return
            except TaskStoreError:
                # An unavailable authorization dependency denies rather than
                # letting the stream coast on a stale decision. Distinguished from
                # revocation because "we can no longer confirm you may read this"
                # and "you may no longer read this" are different operational
                # events, and only the first is worth retrying.
                logger.warning("Task API stream closing: authorization dependency unavailable", exc_info=True)
                outcome.reason = CLOSE_UNAVAILABLE
                return
            last_recheck = now

        try:
            page = store.read_events(task_id=request.task_id, after_sequence=cursor, limit=SSE_EVENT_PAGE_SIZE)
        except TaskStoreError:
            # Storage is an authorization and correctness dependency. Continuing
            # to hold the socket open would present an idle-but-healthy stream
            # while events accumulate unread — indistinguishable to the client
            # from a task that has gone quiet.
            logger.warning("Task API stream closing: event storage unavailable", exc_info=True)
            outcome.reason = CLOSE_UNAVAILABLE
            return

        if page:
            for event in page:
                # A page read strictly after the cursor cannot contain an event at
                # or below it, so this is a guard against a store that violates
                # its contract rather than an expected branch. Emitting a
                # duplicate would corrupt a client's deduplication state.
                if event.sequence <= cursor:
                    continue
                yield encode_event(event)

                # Bookkeeping deliberately *after* the yield, which matters for
                # more than style. An async generator suspends at the yield, so if
                # the client disconnects while receiving this frame the lines below
                # never run and ``last_sequence`` stays at the previous event.
                #
                # That is the direction to err in. Recording the sequence before
                # the yield would claim delivery of a frame that may never have
                # arrived, and a reconnect from that cursor would skip the event
                # entirely — a silent gap, which AC02 forbids. Understating means a
                # reconnect re-sends one event the client may already hold, and
                # clients deduplicate by event ID. Duplicate-and-detectable beats
                # lost-and-invisible.
                cursor = event.sequence
                outcome.last_sequence = cursor
                outcome.events_emitted += 1
                last_heartbeat = clock.monotonic()

                if event.closes_stream:
                    # Design section 9: "Close terminal streams only after all
                    # committed terminal events have been emitted." The remaining
                    # events in this page are still drained below before return.
                    outcome.reason = CLOSE_TERMINAL

            if outcome.reason == CLOSE_TERMINAL:
                return

            # A full page means more is already committed. Continuing immediately
            # rather than sleeping is what lets a long backlog replay at read
            # speed instead of 100 events per second.
            if len(page) == SSE_EVENT_PAGE_SIZE:
                continue

        # A heartbeat is sent only when nothing else has been. It is a comment
        # frame, so it cannot advance the client's position, and it explicitly
        # declares itself not-progress: an open socket is not evidence the task is
        # advancing, and T6-AC01 fails if heartbeat traffic alone is presented as
        # progress.
        if clock.monotonic() - last_heartbeat >= SSE_HEARTBEAT_INTERVAL_SECONDS:
            yield encode_heartbeat(heartbeat_frame(clock.now()))
            last_heartbeat = clock.monotonic()

        await clock.sleep(SSE_POLL_INTERVAL_SECONDS)


class BoundedFrameBuffer:
    """A per-subscriber buffer that drops the subscriber, never the evidence.

    Design section 9: "Bound each subscriber buffer independently; close a slow
    reader with a replay cursor rather than blocking the agent."

    The ordering of those two clauses is the design. When a consumer stops reading,
    something has to give, and the choices are: block the write (which applies
    backpressure through the stream into the agent's execution — one slow reader
    can then stall the task, which T6-AC04 forbids), silently drop frames (which
    makes the client's history wrong while it still looks complete), or disconnect
    the reader with an accurate resume position. Only the third is honest, so a
    full buffer is a disconnect.

    Protected frames — terminal outcomes, run outcomes, gap reports — are admitted
    past the bound rather than triggering the disconnect, because the point of
    disconnecting is to let the client reconnect and replay, and a client that
    never learns the task *ended* has nothing to reconnect for. Terminal evidence
    cannot be dropped.
    """

    def __init__(self, *, max_frames: int = SSE_MAX_BUFFERED_FRAMES, max_bytes: int = SSE_MAX_BUFFERED_BYTES) -> None:
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self.frames: list[bytes] = []
        self.buffered_bytes = 0
        self.overflowed = False

    def offer(self, frame: bytes, *, protected: bool = False) -> bool:
        """Buffer a frame. Returns False when the subscriber must be dropped.

        Returning a decision rather than raising, because "this subscriber is too
        slow" is an expected condition on a public streaming surface, not an error
        in the producing path.
        """
        if protected:
            self.frames.append(frame)
            self.buffered_bytes += len(frame)
            return True

        if len(self.frames) >= self.max_frames or self.buffered_bytes + len(frame) > self.max_bytes:
            self.overflowed = True
            return False

        self.frames.append(frame)
        self.buffered_bytes += len(frame)
        return True

    def drain(self) -> list[bytes]:
        frames, self.frames, self.buffered_bytes = self.frames, [], 0
        return frames


class StreamRegistry:
    """In-memory counter for isolated fixtures; production uses RedisStreamRegistry."""

    def __init__(self) -> None:
        self.per_task: dict[str, int] = {}
        self.per_principal: dict[str, int] = {}
        self.total = 0

    def acquire(self, *, task_id: str, principal_id: str) -> None:
        """Reserve a slot, or refuse with the limit that was reached.

        ``429`` rather than ``503``: the caller is over a limit, and the condition
        clears when its own streams close. Which of the three caps was hit is named
        in the message because all three are the caller's own resource use — this
        discloses nothing about other tenants, and a client told only "too many
        streams" cannot tell whether closing its own connections will help.
        """
        if self.total >= SSE_MAX_STREAMS_PER_ENVIRONMENT:
            raise errors.rate_limited("This environment is at its concurrent Task API stream limit.")
        if self.per_task.get(task_id, 0) >= SSE_MAX_STREAMS_PER_TASK:
            raise errors.rate_limited("This task is at its concurrent stream limit.")
        if self.per_principal.get(principal_id, 0) >= SSE_MAX_STREAMS_PER_PRINCIPAL:
            raise errors.rate_limited("This principal is at its concurrent stream limit.")

        self.per_task[task_id] = self.per_task.get(task_id, 0) + 1
        self.per_principal[principal_id] = self.per_principal.get(principal_id, 0) + 1
        self.total += 1

    def release(self, *, task_id: str, principal_id: str) -> None:
        """Return a slot, deleting keys at zero.

        Keys are removed rather than left at zero so the dictionaries track live
        streams instead of growing once per task ever streamed — a long-lived pod
        would otherwise accumulate an entry for every task it has served, which is
        a slow leak in the thing that exists to prevent exhaustion.
        """
        for counter, key in ((self.per_task, task_id), (self.per_principal, principal_id)):
            remaining = counter.get(key, 0) - 1
            if remaining > 0:
                counter[key] = remaining
            else:
                counter.pop(key, None)
        self.total = max(0, self.total - 1)


def resume_cursor_for(task_id: str, sequence: int) -> str | None:
    """The cursor a dropped subscriber should reconnect with.

    None at sequence 0 rather than ``<task_id>:0``: a client that has received no
    events has no position, and handing it a zero cursor would give it a token the
    contract's pattern rejects.
    """
    return format_cursor(task_id, sequence) if sequence > 0 else None


async def drain_with_timeout(
    frames: list[bytes],
    write: Callable[[bytes], Awaitable[None]],
    *,
    timeout: float = SSE_BLOCKED_WRITE_DISCONNECT_SECONDS,
) -> bool:
    """Write buffered frames, giving up after the blocked-write bound.

    Returns False if the write blocked past the bound, which the caller turns into
    a disconnect. The timeout is the mechanism that keeps a TCP-stalled consumer
    from holding a server task open indefinitely: without it, a client that opens
    a connection and never reads would occupy a slot for the full 10-minute window
    while contributing nothing.
    """
    try:
        async with asyncio.timeout(timeout):
            for frame in frames:
                await write(frame)
    except TimeoutError:
        return False
    return True


def history_is_expired(record: TaskRecord, after_sequence: int) -> bool:
    """Whether a cursor points below what is still retained.

    Strictly below ``oldest_sequence - 1``: a cursor equal to
    ``oldest_sequence - 1`` is the boundary case where the *next* event the client
    wants is the oldest one still held, so replay is complete and a 410 would be
    wrong. Getting this off by one would either reject a resumable client or
    silently skip the oldest retained event.
    """
    if record.oldest_sequence == 0:
        return False
    return after_sequence < record.oldest_sequence - 1


def gap_event_data(*, task_id: str, from_sequence: int, to_sequence: int, report_ids: list[str], reason: str) -> dict:
    """Build ``history.gap`` data naming what was lost.

    Design section 9: "If storage recovers, persist a history-gap record
    identifying omitted producer reports. [...] never fabricate sequence numbers
    for unpersisted progress." The report IDs are named because they are the only
    durable identity the omitted progress ever had — no sequence was allocated to
    it, so there is nothing else to point at.
    """
    return {
        "omitted_from_cursor": format_cursor(task_id, from_sequence),
        "omitted_to_cursor": format_cursor(task_id, to_sequence),
        "omitted_report_ids": list(report_ids),
        "reason": reason,
    }
