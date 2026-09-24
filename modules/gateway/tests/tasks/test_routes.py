"""The public read surface over HTTP: snapshot, cursors and the SSE stream.

These tests are the AC03 and AC05 evidence at the route boundary. ``test_streaming``
already proves the delivery loop's ordering, replay and close semantics against a
fake clock; what can only be proved here is everything the loop never sees — the
status code and body of a refusal, the response headers of the stream, and the fact
that authorization runs before a cursor is honoured rather than after.

Every success body and every refusal body is validated against the frozen v1
schemas. That is deliberate belt-and-braces: a route could emit a plausible JSON
object with the right values and still violate the contract by adding a field
``additionalProperties: false`` forbids, and the only thing that catches that is the
schema the evaluator uses.

Covers T6-AC02 (snapshot-to-live handoff observable over HTTP), T6-AC03 (cross-tenant
and nonowner refusal, stale cursors) and T6-AC05 (correctly scoped, contract-shaped
bodies).
"""

from __future__ import annotations

import json

import pytest

from src.tasks import authz, errors, http
from src.tasks import routes as routes_module
from src.tasks import streaming as streaming_module
from src.tasks.limits import SSE_MAX_STREAMS_PER_TASK
from src.tasks.read_store import TaskStoreError

from .conftest import (
    NO_SCOPES,
    OTHER_TASK,
    OTHER_TENANT,
    SAME_TENANT_OTHER,
    TASK,
    emit,
    finish,
    frames,
    make_record,
)

ERROR_SCHEMA = "errors.schema.json#/$defs/error_response"
SNAPSHOT_SCHEMA = "public-api.schema.json#/$defs/task_snapshot"


def sequences(collected: list[dict]) -> list[int]:
    """The sequence numbers of the durable events among some frames.

    Keyed on the presence of ``id``, which is exactly what distinguishes a durable
    event from a snapshot or a heartbeat on the wire — the same signal a conforming
    SSE client uses to decide whether to advance its position.
    """
    return [json.loads(frame["data"])["sequence"] for frame in collected if "id" in frame]


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


async def test_snapshot_matches_the_frozen_contract(client, store, contract) -> None:
    """The owner's read is a contract-valid ``task_snapshot``."""
    emit(store, 5)

    response = await client.get(f"/v1/tasks/{TASK}")

    assert response.status_code == 200
    body = response.json()
    assert contract(body, SNAPSHOT_SCHEMA) == []
    assert body["latest_event_cursor"] == f"{TASK}:5"
    assert body["oldest_event_cursor"] == f"{TASK}:1"
    # The polling path has to be complete on its own: a client that cannot hold a
    # socket open still learns where history begins and ends from this one read.
    assert body["result"] is None and body["error"] is None
    assert response.headers["cache-control"] == "no-store"


async def test_snapshot_before_any_event_reports_null_cursors(client, contract) -> None:
    """No events yet is ``null``, not ``<task>:0``.

    A zero cursor would not match the contract's pattern, so a client that stored it
    and replayed with it would be refused by the very surface that issued it.
    """
    response = await client.get(f"/v1/tasks/{TASK}")

    body = response.json()
    assert contract(body, SNAPSHOT_SCHEMA) == []
    assert body["latest_event_cursor"] is None
    assert body["oldest_event_cursor"] is None


@pytest.mark.parametrize("identity", [SAME_TENANT_OTHER, OTHER_TENANT], ids=["same-tenant-nonowner", "cross-tenant"])
async def test_nonowner_reads_are_refused_as_absent(client, caller, contract, identity) -> None:
    """T6-AC03: a nonowner cannot read, and cannot tell the task exists.

    The same-tenant case is the one that matters most: it is the access model v1
    deliberately does not grant. Both must produce the *same* 404 as a genuine miss,
    because a distinguishable refusal is an existence oracle for other principals'
    tasks.
    """
    caller[0] = identity

    response = await client.get(f"/v1/tasks/{TASK}")
    missing = await client.get(f"/v1/tasks/{OTHER_TASK}")

    assert response.status_code == 404
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "not_found"
    assert {k: v for k, v in response.json().items() if k != "request_id"} == {k: v for k, v in missing.json().items() if k != "request_id"}


async def test_read_without_read_scope_is_refused(client, caller, contract) -> None:
    caller[0] = NO_SCOPES

    response = await client.get(f"/v1/tasks/{TASK}")

    assert response.status_code == 403
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "disallowed_scope"


async def test_storage_outage_denies_rather_than_reporting_absence(client, store, contract) -> None:
    """An unavailable dependency is 503, never a 404.

    Design section 4: an unavailable authorization dependency denies and cannot turn
    into anonymous access. Reporting absence would be worse than an outage — it would
    tell a client its task is gone.
    """
    store.fail = True

    response = await client.get(f"/v1/tasks/{TASK}")

    assert response.status_code == 503
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "prerequisite_unavailable"
    assert response.headers["retry-after"] == "1"


async def test_unconfigured_store_is_an_unavailable_prerequisite(client, contract) -> None:
    """No store installed is 503, not a crash.

    T1 owns the DynamoDB store; until it is wired, the honest answer is an
    unavailable prerequisite. A 500 here would be indistinguishable from a bug in the
    route.
    """
    routes_module.set_store(None)

    response = await client.get(f"/v1/tasks/{TASK}")

    assert response.status_code == 503
    assert contract(response.json(), ERROR_SCHEMA) == []


# ---------------------------------------------------------------------------
# Feature gating
# ---------------------------------------------------------------------------


async def test_disabled_surface_refuses_before_touching_the_credential(client, monkeypatch, contract) -> None:
    """A disabled route is not a token oracle.

    The flag check runs before authentication, so an environment with the Task API
    off is not a place where credentials get exercised. Asserting it by making
    authentication explode is the only way to prove the ordering — a 503 alone is
    consistent with authenticating first and refusing after.
    """
    monkeypatch.delenv(http.FLAG_READ, raising=False)

    def explode(request):
        raise AssertionError("the credential must not be validated on a disabled surface")

    monkeypatch.setattr(authz, "authenticate", explode)

    response = await client.get(f"/v1/tasks/{TASK}")

    assert response.status_code == 503
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "prerequisite_unavailable"
    # No Retry-After: a feature flag will not flip in a second, and advertising a
    # delay would invite a conforming client into an indefinite poll.
    assert "retry-after" not in response.headers
    assert "retry_after_ms" not in response.json()


async def test_disabled_surface_also_closes_the_stream_route(client, monkeypatch) -> None:
    monkeypatch.delenv(http.FLAG_READ, raising=False)

    response = await client.get(f"/v1/tasks/{TASK}/events")

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Cursor resolution
# ---------------------------------------------------------------------------


async def test_conflicting_cursor_forms_are_refused(client, store, contract) -> None:
    """Design section 5: conflicting cursor forms return 400.

    Preferring one silently would resume a confused client at a position it did not
    ask for; preferring the lower — which looks safer — would replay events an
    automatic ``Last-Event-ID`` says it already holds.
    """
    emit(store, 5)

    response = await client.get(f"/v1/tasks/{TASK}/events?after={TASK}:2", headers={"Last-Event-ID": f"{TASK}:4"})

    assert response.status_code == 400
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "invalid_cursor"


async def test_agreeing_cursor_forms_are_accepted(client, store, contract) -> None:
    """The same position in both forms is not a conflict.

    A client that persists its cursor *and* lets its SSE library resend
    ``Last-Event-ID`` sends both, identically. Refusing that would break the most
    careful class of consumer.
    """
    emit(store, 3)
    finish(store)

    response = await client.get(f"/v1/tasks/{TASK}/events?after={TASK}:2", headers={"Last-Event-ID": f"{TASK}:2"})

    assert response.status_code == 200
    assert sequences(frames(response.content)) == [3, 4]


@pytest.mark.parametrize(
    "cursor",
    [
        "not-a-cursor",
        f"{TASK}:0",
        f"{TASK}:abc",
        f"{OTHER_TASK}:2",
        f"{TASK}:2:3",
    ],
    ids=["garbage", "zero-sequence", "nonnumeric", "foreign-task", "trailing"],
)
async def test_malformed_and_foreign_cursors_are_refused_without_echo(client, store, contract, cursor) -> None:
    """A bad cursor is 400, and the refusal never quotes it back.

    The foreign-task case is the security-relevant one: a cursor confers no read
    authority, so one naming another task must not be reinterpreted against the task
    the caller *is* authorized for. Reflecting the value would additionally put
    caller-controlled text into a response body and every log line recording it.
    """
    emit(store, 5)

    response = await client.get(f"/v1/tasks/{TASK}/events?after={cursor}")

    assert response.status_code == 400
    body = response.json()
    assert contract(body, ERROR_SCHEMA) == []
    assert body["code"] == "invalid_cursor"
    assert cursor not in body["message"]


async def test_cursor_ahead_of_history_is_refused(client, store, contract) -> None:
    """A future cursor is a mistake, not a wait condition.

    Accepting it would open a stream that correctly delivers nothing — which a client
    cannot distinguish from a task that has gone quiet, so it would wait forever on a
    position that can never be reached by replay.
    """
    emit(store, 3)

    response = await client.get(f"/v1/tasks/{TASK}/events?after={TASK}:99")

    assert response.status_code == 400
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "invalid_cursor"


async def test_expired_cursor_is_410_with_bounds_and_an_explicit_gap(client, store, contract) -> None:
    """T6-AC03/AC04: expired history is explicit loss, before SSE opens.

    Answered as an HTTP status because after a ``200 text/event-stream`` has begun
    the only way to report it is a stream that stops — which a client cannot tell
    from a network fault, so it retries the same doomed cursor forever.

    The ``history_gap`` flag is what makes this honest rather than merely
    informative: without it a client could read the bounds as "start here and you
    have everything". The contract ships a rejected fixture for exactly the omission.
    """
    emit(store, 10)
    store.prune_before(TASK, 6)

    response = await client.get(f"/v1/tasks/{TASK}/events?after={TASK}:2")

    assert response.status_code == 410
    body = response.json()
    assert contract(body, "errors.schema.json#/$defs/history_expired_response") == []
    assert body["code"] == "history_expired"
    assert body["details"] == {
        "task_id": TASK,
        "current_status": "running",
        "oldest_event_cursor": f"{TASK}:6",
        "latest_event_cursor": f"{TASK}:10",
        "history_gap": True,
    }


async def test_cursor_at_the_retention_boundary_still_replays(client, store) -> None:
    """The oldest retained event is deliverable, not expired.

    ``after = oldest - 1`` means the next event the client wants is the oldest one
    still held, so replay is complete and a 410 would be wrong. An off-by-one here
    either rejects a resumable client or silently skips the oldest retained event.
    """
    emit(store, 10)
    store.prune_before(TASK, 6)
    finish(store)

    response = await client.get(f"/v1/tasks/{TASK}/events?after={TASK}:5")

    assert response.status_code == 200
    assert sequences(frames(response.content)) == [6, 7, 8, 9, 10, 11]


async def test_expired_cursor_is_refused_only_after_authorization(client, store, caller, contract) -> None:
    """A nonowner supplying an expired cursor learns nothing about retention.

    Ordering matters: resolving the cursor first would answer 410 with this task's
    real oldest and latest cursors — leaking both its existence and its event count
    to a caller entitled to neither.
    """
    emit(store, 10)
    store.prune_before(TASK, 6)
    caller[0] = OTHER_TENANT

    response = await client.get(f"/v1/tasks/{TASK}/events?after={TASK}:2")

    assert response.status_code == 404
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert "details" not in response.json()


# ---------------------------------------------------------------------------
# SSE stream
#
# Two harnesses, for a reason worth stating once. A stream against a *terminal*
# task closes itself, so an ordinary HTTP client can read it whole — and where the
# subject is content (which events, in what order, shaped how), that is the clearer
# test. A stream against a live task never ends, and the property T6-AC01 states is
# about *when* frames arrive, which no buffering client can observe. Those tests
# use ``StreamProbe`` and read the ASGI messages directly.
# ---------------------------------------------------------------------------


async def test_stream_headers_forbid_the_buffering_that_ac01_fails_on(client, store) -> None:
    """T6-AC01: the transport is told not to buffer.

    An intermediary that buffers this response turns incremental progress into one
    delivery at the end, which is the precise failure the criterion names. Correct
    frames are not enough if something in the path is free to coalesce them.
    """
    finish(store)

    response = await client.get(f"/v1/tasks/{TASK}/events")

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"


async def test_snapshot_frame_opens_the_stream_and_carries_no_id(probe, store, contract) -> None:
    """T6-AC02: the handoff frame states state without claiming a position.

    A client that connects, receives state and immediately drops must resume from its
    last *real* event. If the snapshot carried an ``id``, a conforming SSE client
    would set Last-Event-ID from it and skip everything committed between the
    snapshot being built and the drop — invisibly, because the stream looked
    continuous. The contract ships ``sse-snapshot-frame-advances-cursor`` as a
    rejected fixture for this exact shape.

    Read against a live task on purpose: this is the case where a wrong cursor does
    damage, because there is more history still to come.
    """
    emit(store, 4)

    async with probe(f"/v1/tasks/{TASK}/events?after={TASK}:4") as stream:
        opening = await stream.next_frame()

    assert opening["event"] == "snapshot"
    assert "id" not in opening
    body = json.loads(opening["data"])
    assert contract(body, "events.schema.json#/$defs/sse_snapshot_frame") == []
    assert body["advances_last_event_id"] is False
    assert body["high_water_cursor"] == f"{TASK}:4"
    assert body["oldest_event_cursor"] == f"{TASK}:1"


async def test_two_authored_updates_arrive_before_the_run_exits(probe, store, contract) -> None:
    """T6-AC01: two substantive progress events are externally observable mid-run.

    The criterion's failure modes are heartbeat-only traffic and buffered final
    output, so this asserts what distinguishes a pass from both:

    * two *distinct authored* ``progress.updated`` events, with different messages —
      not one event seen twice, and not keepalive traffic;
    * each delivered as its own write while the task is still ``running`` and the
      route is still executing, which is what ``StreamProbe`` observes and a
      read-to-completion client structurally cannot.

    The second event is committed *after* the first is received, so the ordering is
    load-bearing rather than incidental: the stream is demonstrably still open and
    still delivering when new progress appears.
    """
    emit(store, 1, data={"message": "Reproduced the 503 against the staging pool.", "stage": "evidence_inventory"})

    async with probe(f"/v1/tasks/{TASK}/events") as stream:
        assert (await stream.next_frame())["event"] == "snapshot"
        first = await stream.next_frame()

        emit(store, 1, data={"message": "Pool acquisition timeout correlates with the deploy.", "stage": "analysis"})
        second = await stream.next_frame()

        assert store.tasks[TASK].status == "running", "both markers must arrive before the run exits"

    bodies = [json.loads(first["data"]), json.loads(second["data"])]
    for body in bodies:
        assert contract(body, "events.schema.json#/$defs/event") == []
        assert body["type"] == "progress.updated"
    assert [body["sequence"] for body in bodies] == [1, 2]
    assert len({body["data"]["message"] for body in bodies}) == 2, "two distinct authored updates, not one repeated"


async def test_delivered_events_are_contract_valid_and_carry_their_cursor(client, store, contract) -> None:
    """T6-AC05: every delivered event validates, and its id is its cursor."""
    emit(store, 2)
    finish(store)

    response = await client.get(f"/v1/tasks/{TASK}/events")

    delivered = [frame for frame in frames(response.content) if "id" in frame]
    assert len(delivered) == 3
    for frame in delivered:
        event = json.loads(frame["data"])
        assert contract(event, "events.schema.json#/$defs/event") == []
        assert frame["id"] == event["event_id"] == f"{TASK}:{event['sequence']}"
        assert frame["event"] == "event"


async def test_terminal_task_replays_history_then_the_server_closes(client, store) -> None:
    """T6-AC02: a completed task is still fully readable, and the stream ends.

    Terminal replay is what makes a late subscriber a first-class consumer: it gets
    the whole history and the outcome, then an orderly close rather than a socket held
    open on a task that will never speak again.
    """
    emit(store, 2)
    finish(store)

    response = await client.get(f"/v1/tasks/{TASK}/events")

    parsed = frames(response.content)
    assert sequences(parsed) == [1, 2, 3]
    assert json.loads(parsed[-1]["data"])["type"] == "task.completed"


async def test_subscribe_before_start_then_resume_loses_nothing(probe, client, store) -> None:
    """T6-AC02: subscribe-before-start, drop, reconnect — no silent gap.

    The sequence exercised is the one a real client hits: connect while the task has
    no events at all, receive the first two, drop, then reconnect with the cursor of
    the last frame actually received. The union must be consecutive from 1 with no
    hole, which is the only externally checkable statement of "no silent handoff
    gaps".
    """
    async with probe(f"/v1/tasks/{TASK}/events") as stream:
        opening = json.loads((await stream.next_frame())["data"])
        assert opening["high_water_cursor"] is None, "no events yet is null, not a zero cursor"

        emit(store, 2)
        received = await stream.take(2)
        resume = received[-1]["id"]

    finish(store)
    reconnected = await client.get(f"/v1/tasks/{TASK}/events", headers={"Last-Event-ID": resume})

    seen = sequences(received) + sequences(frames(reconnected.content))
    assert seen == [1, 2, 3]


async def test_a_client_disconnect_closes_the_stream_and_frees_its_slot(probe, store) -> None:
    """T6-AC04: a departing subscriber does not hold resources or block execution.

    A disconnect abandons the generator rather than returning from it, so the slot is
    released in its ``finally``. Without that, the most common way a stream ends would
    leak a slot, and the caps would tighten over the life of the pod until every
    stream was refused — a slow failure in the mechanism that exists to prevent
    exhaustion.
    """
    emit(store, 1)

    async with probe(f"/v1/tasks/{TASK}/events") as stream:
        await stream.take(2)
        assert routes_module._STREAMS.total == 1
        await stream.disconnect()

    assert routes_module._STREAMS.total == 0
    assert routes_module._STREAMS.per_task == {}


@pytest.mark.parametrize(
    "revoke",
    [
        lambda caller: caller.__setitem__(0, OTHER_TENANT),
        lambda caller: caller.__setitem__(0, NO_SCOPES),
    ],
    ids=["ownership-lost", "scope-withdrawn"],
)
async def test_revoked_access_terminates_a_live_stream(probe, store, caller, monkeypatch, revoke) -> None:
    """T6-AC03: revocation closes an open stream, whichever check withdraws it.

    Design section 4 requires *all four* checks on every operation, and a stream is a
    sequence of operations rather than one. The two cases here fail different checks:
    ownership no longer matches the record, and the credential no longer carries read
    scope. An implementation that re-read only the task row would pass the first and
    hang on the second, which is the more likely revocation in practice — tokens and
    aliases change far more often than task ownership does.

    The interval is driven to zero rather than waited out: the real value is 15
    seconds and the bound is 30, so a real-time test would spend a quarter-minute
    proving a timer fires. The arithmetic that the interval fits inside the bound is
    verified against a fake clock in ``test_streaming``; what only this test can show
    is that the recheck re-derives the decision from the credential rather than
    reusing the caller resolved at open.
    """
    emit(store, 1)

    async with probe(f"/v1/tasks/{TASK}/events") as stream:
        await stream.take(2)
        monkeypatch.setattr(streaming_module, "STREAM_AUTHORIZATION_RECHECK_SECONDS", 0)
        revoke(caller)

        assert await stream.next_frame() is None, "a revoked stream must end, not continue"

    # Still running: the close is attributable to revocation, not to the task having
    # reached a terminal state and closed the stream for the ordinary reason.
    assert store.tasks[TASK].status == "running"
    assert routes_module._STREAMS.total == 0


async def test_stream_refuses_a_nonowner_before_opening(client, caller, contract) -> None:
    """T6-AC03: a nonowner gets a JSON 404, not an empty event stream.

    Authorization happens before the response begins, so the refusal is a status and a
    body. A stream that opened and then delivered nothing would be a denial the client
    could only read as silence.
    """
    caller[0] = OTHER_TENANT

    response = await client.get(f"/v1/tasks/{TASK}/events")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert contract(response.json(), ERROR_SCHEMA) == []


async def test_stream_over_the_per_task_cap_is_refused_with_a_body(client, store, contract) -> None:
    """T6-AC04: the concurrency cap answers with a contract error, not a dropped socket.

    The slot is taken before the generator starts precisely so the refusal can carry a
    body — once a 200 event-stream has begun there is no way to say "too many streams"
    that a client can act on. Slots are pre-taken rather than held by real connections
    because the subject is the refusal, and holding live ten-minute streams to observe
    it would make the test's duration the thing being measured.
    """
    for index in range(SSE_MAX_STREAMS_PER_TASK):
        routes_module._STREAMS.acquire(task_id=TASK, principal_id=f"svc-alpha-{index}")

    response = await client.get(f"/v1/tasks/{TASK}/events")

    assert response.status_code == 429
    body = response.json()
    assert contract(body, ERROR_SCHEMA) == []
    assert body["code"] == "rate_limited"
    assert body["retry_after_ms"] == 1000
    assert response.headers["retry-after"] == "1"


async def test_repeated_streams_do_not_exhaust_the_cap(client, store) -> None:
    """Every completed stream returns its slot, so the cap bounds concurrency only."""
    finish(store)

    for _ in range(SSE_MAX_STREAMS_PER_TASK + 2):
        response = await client.get(f"/v1/tasks/{TASK}/events")
        assert response.status_code == 200

    assert routes_module._STREAMS.total == 0
    assert routes_module._STREAMS.per_task == {}


async def test_storage_outage_before_the_stream_opens_is_a_json_503(client, store, contract) -> None:
    """An outage at open is a status, not a stream that never speaks.

    The generator translates ``TaskStoreError`` rather than letting it propagate: an
    unhandled exception inside a StreamingResponse reaches the ASGI layer, which
    renders a body this contract does not define.
    """
    store.fail = True

    response = await client.get(f"/v1/tasks/{TASK}/events")

    assert response.status_code == 503
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "prerequisite_unavailable"


async def test_a_mid_stream_storage_outage_closes_the_stream(probe, store) -> None:
    """T6-AC04: loss is explicit; the stream stops rather than going quiet.

    Holding the socket open would present an idle-but-healthy stream while events
    accumulated unread — indistinguishable to the client from a task that has stopped
    producing, which is the opposite of explicit.
    """
    emit(store, 1)

    async with probe(f"/v1/tasks/{TASK}/events") as stream:
        await stream.take(2)
        store.fail = True
        assert await stream.next_frame() is None

    assert routes_module._STREAMS.total == 0


async def test_a_store_error_type_is_not_confused_with_a_route_bug() -> None:
    """``TaskStoreError`` is a plain exception, so a bare ``except`` cannot miss it.

    Guards the seam rather than a route: if ``TaskStoreError`` ever became a
    ``TaskApiError`` subclass, ``http.contract_errors`` would render it with whatever
    status that subclass carried, and an outage would silently start reporting as a
    caller error.
    """
    assert not issubclass(TaskStoreError, errors.TaskApiError)


async def test_an_unexpected_route_failure_does_not_leak_internals(client, store, monkeypatch, contract) -> None:
    """T6-AC05: task text never escapes through an error body.

    Task state carries caller instructions and tool output, so an internal exception
    message is not safe to echo. The bare handler answers as an unavailable
    prerequisite — the honest statement that the request did not complete — and logs
    the traceback server-side instead.
    """

    def boom(*args, **kwargs):
        raise RuntimeError("secret-instruction-text-from-the-task")

    monkeypatch.setattr(routes_module.snapshot, "render", boom)

    response = await client.get(f"/v1/tasks/{TASK}")

    assert response.status_code == 503
    body = response.json()
    assert contract(body, ERROR_SCHEMA) == []
    assert "secret-instruction-text-from-the-task" not in json.dumps(body)


async def test_snapshot_of_a_task_with_lost_heartbeats_reports_unknown(client, store, contract) -> None:
    """Invariant LC-08: unknown evidence is nullable, never zero or success.

    A lost heartbeat is neither an exit nor a completion, so the honest snapshot is
    ``running`` with ``execution_health: unknown`` and ``recovery_required: true``.
    Reporting ``failed`` would fabricate an outcome; reporting ``healthy`` would
    fabricate evidence. The contract ships
    ``task-snapshot-unknown-health-reported-success`` as a rejected fixture.
    """
    store.tasks[TASK] = make_record(execution_health="unknown", recovery_required=True)

    response = await client.get(f"/v1/tasks/{TASK}")

    body = response.json()
    assert contract(body, SNAPSHOT_SCHEMA) == []
    assert (body["status"], body["execution_health"], body["recovery_required"]) == ("running", "unknown", True)
    assert body["result"] is None and body["error"] is None
