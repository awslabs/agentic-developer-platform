"""``GET /v1/tasks/{task_id}`` and ``GET /v1/tasks/{task_id}/events``.

The two public read routes. Everything substantive they do lives in
``snapshot.py``, ``streaming.py`` and ``authz.py``; this module is the thin layer
that resolves a cursor, authorizes, and hands off — and it is worth saying what
"thin" is protecting, because there are two decisions here that cannot move
anywhere else.

**The cursor is resolved before the stream opens.** An expired cursor is
``410 history_expired`` with the retained bounds and an explicit gap flag; a
malformed or foreign one is ``400 invalid_cursor``. Both must be answered as HTTP
statuses, which means they must be decided *before* the first byte of the event
stream. Once a ``200 text/event-stream`` response has begun, the only way to
report either is a stream that stops — and a client cannot distinguish that from a
network fault, so it retries the same doomed cursor. Design section 9 says the
410 comes "before opening SSE"; this is where that happens.

**Authorization is not a one-time gate.** The stream re-checks ownership on a
bounded interval through the ``recheck`` callable passed into ``stream_events``.
A long-lived read authorized only at open would keep delivering protected events
after access was revoked, for as long as the client held the socket.

Design reference: implementation-design.md sections 5 and 9.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.database import get_db, get_session_factory
from src.tasks import authz, errors, http, snapshot, streaming
from src.tasks.events import CursorError, parse_cursor
from src.tasks.store import TaskRecord, TaskStore
from src.tasks.streaming import StreamRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/tasks", tags=["task-api"])

#: Per-process stream accounting. Module-level because the caps bound what this
#: replica will hold open concurrently, which is a property of the process rather
#: than of any request. See ``StreamRegistry`` for the per-replica caveat.
_STREAMS = StreamRegistry()

#: The storage backend, injected. ``None`` until T1's DynamoDB store lands, which
#: is why the read surface answers 503 rather than raising: an unconfigured
#: dependency is an unavailable prerequisite, not a caller error.
_STORE: TaskStore | None = None


def set_store(store: TaskStore | None) -> None:
    """Install the storage backend. Called by T1's wiring and by tests."""
    global _STORE
    _STORE = store


def get_store() -> TaskStore:
    if _STORE is None:
        raise errors.prerequisite_unavailable("Task storage is not configured in this environment.")
    return _STORE


async def caller_for(request: Request, db: AsyncSession) -> authz.Caller:
    """Authenticate, resolve the canonical principal, and require read scope.

    The flag check runs first so that a disabled surface does not validate
    credentials: an environment with the Task API off should not be a place where
    tokens are exercised, and refusing before authentication keeps a disabled route
    from being usable as a token oracle.
    """
    http.require_flag(http.FLAG_READ)
    context, scopes = authz.authenticate(request)
    caller = await authz.resolve_caller(context, scopes, db)
    caller.require(authz.SCOPE_READ)
    return caller


def resolve_after(request: Request, record: TaskRecord) -> int:
    """Resolve the replay position from ``Last-Event-ID`` or ``?after=``.

    Both forms are accepted because both exist for real reasons: browsers resend
    ``Last-Event-ID`` automatically on reconnect, and a non-browser client that
    persists its position across process restarts needs to supply it explicitly.

    Supplying *both* with different values is ``400``, per design section 5
    ("Supplying conflicting cursor forms returns 400"). Silently preferring one
    would resume a confused client at a position it did not ask for and cannot
    predict — and picking the lower of the two, which looks safer, would replay
    events an automatic ``Last-Event-ID`` says the client already has.

    No cursor at all means replay from the oldest retained event, which is
    ``after_sequence = 0``, not ``oldest_sequence``: the loop reads strictly
    *after* its position, so starting at the oldest retained sequence would skip
    the very first event the client is entitled to.
    """
    header = (request.headers.get("Last-Event-ID") or "").strip()
    query = (request.query_params.get("after") or "").strip()

    if header and query and header != query:
        raise errors.invalid_cursor("Last-Event-ID and the after parameter name different positions.")

    supplied = header or query
    if not supplied:
        return 0

    try:
        sequence = parse_cursor(supplied, task_id=record.task_id)
    except CursorError as error:
        # The message is this module's own text, not the exception's, because a
        # cursor is caller-supplied and echoing it back into a response body is
        # how a reflected value reaches a log or a console unescaped.
        logger.info("Task API stream refused a cursor", extra={"reason": str(error)})
        raise errors.invalid_cursor("The supplied cursor is not a valid event cursor for this task.") from None

    if sequence > record.latest_sequence:
        # A future cursor is a client mistake, not a wait condition. Accepting it
        # would open a stream that correctly delivers nothing, indistinguishable
        # from a task that has gone quiet.
        raise errors.invalid_cursor("The supplied cursor is ahead of this task's latest event.")

    if streaming.history_is_expired(record, sequence):
        raise errors.history_expired(
            task_id=record.task_id,
            current_status=record.status,
            oldest_event_cursor=record.oldest_event_cursor,
            latest_event_cursor=record.latest_event_cursor,
        )

    return sequence


@router.get("/{task_id}")
@http.contract_errors
async def read_task(task_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Strongly consistent task snapshot.

    Polling this remains a complete way to use the API: it carries the terminal
    result and the event cursor bounds, so a client that cannot hold an SSE
    connection open — a Lambda, a CI step, anything behind a proxy that buffers —
    is not a second-class consumer.
    """
    caller = await caller_for(request, db)
    record = authz.authorize_task(caller, get_store(), task_id)
    return http.ok(snapshot.render(record, request_id=http.request_id(request)))


@router.get("/{task_id}/events")
@http.contract_errors
async def read_events(task_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Durable, resumable SSE progress.

    The response headers matter as much as the body. ``X-Accel-Buffering: no``
    and ``Cache-Control: no-store`` exist because an intermediary that buffers
    this response converts incremental progress into one delivery at the end —
    which is the precise failure T6-AC01 names as a fail ("buffered final output
    FAILS"). The transport has to be told not to do the thing the criterion
    forbids.
    """
    caller = await caller_for(request, db)
    store = get_store()
    record = authz.authorize_task(caller, store, task_id)
    after = resolve_after(request, record)

    async def recheck() -> TaskRecord:
        """Re-authorize mid-stream, from the credential up.

        Returns the fresh record; raises ``TaskApiError`` if access is gone. The
        stream loop distinguishes that from ``TaskStoreError`` — revocation closes
        the stream as revoked, an outage closes it as unavailable — so this
        deliberately catches neither.

        The credential is re-validated and the principal re-resolved on every
        recheck, not just the task row re-read. Design section 4 requires all four
        checks — "the active alias, canonical principal, task ownership and current
        task policy" — and only two of them are properties of the task. Reusing the
        caller resolved at open would catch an ownership change but miss the more
        likely revocations: an expired token and a deactivated alias. A stream is a
        long-lived read, so those are precisely the things that change during one,
        and missing them would let a revoked credential keep reading protected
        events for as long as it held the socket. "No positive service-policy cache
        may extend that bound" — a caller object carried for ten minutes is such a
        cache.

        A fresh session per recheck rather than the request-scoped one: that session
        is closed when the ASGI request scope ends, which for a StreamingResponse is
        before most of the stream's life. Reusing it would make every recheck after
        the first fail on a closed connection — an authorization check that errors is
        an authorization check that does not run.
        """
        context, scopes = authz.authenticate(request)
        async with get_session_factory()() as session:
            current = await authz.resolve_caller(context, scopes, session)
        current.require(authz.SCOPE_READ)
        return authz.authorize_task(current, store, task_id)

    # Acquired here rather than inside the generator, because refusing a stream
    # has to be a 429 body and a body can only be sent before the response
    # begins. The cost is a narrow window: if the server discards the response
    # without ever iterating it, this slot is never released. Releasing in the
    # generator's ``finally`` covers every path that starts, which is every path
    # a client can cause.
    _STREAMS.acquire(task_id=task_id, principal_id=caller.principal_id)
    outcome = streaming.StreamOutcome(task_id=task_id)

    async def frames() -> AsyncIterator[bytes]:
        """Drive the loop, and release the slot however it ends.

        The ``finally`` is the load-bearing part. A client disconnect abandons this
        generator rather than returning from it, so a slot released only on the
        normal path would leak on exactly the most common ending — and the caps
        would bind tighter over the life of the pod until every stream was refused.
        """
        stream = streaming.StreamRequest(task_id=task_id, after_sequence=after, recheck=recheck)
        try:
            async for frame in streaming.stream_events(stream, store, outcome, is_disconnected=request.is_disconnected):
                yield frame
        finally:
            _STREAMS.release(task_id=task_id, principal_id=caller.principal_id)
            logger.info(
                "Task API stream closed",
                extra={
                    "task_id": task_id,
                    "close_reason": outcome.reason,
                    "events_emitted": outcome.events_emitted,
                    "resume_cursor": outcome.resume_cursor,
                },
            )

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
