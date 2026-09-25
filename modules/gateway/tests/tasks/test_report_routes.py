"""The host-authenticated report ingest: where authority comes from on a write.

This is the T6-AC01 and T6-AC03 evidence on the write side. The read tests prove a
subscriber sees progress; these prove that what a subscriber sees was authored by
the attempt the transport proved, and that nothing else can put a record into a
task's durable history.

The substituted seam is the attempt authenticator, and substituting it is what makes
the central property testable at all: the route must compare every field of the
body's binding against whatever the transport proved, so a test needs to be able to
present a body that disagrees with it. A real credential would only ever produce
agreement.

Covers T6-AC01 (durable authored progress), T6-AC03 (wrong and stale attempts cannot
fabricate events), T6-AC04 (budget exhaustion is explicit) and T6-AC05 (durable,
correctly scoped records).
"""

from __future__ import annotations

import json

import pytest

from src.tasks import http, report_routes
from src.tasks.events import REQUIRED_EVENT_DATA
from src.tasks.limits import (
    MAX_EVENTS_PER_TASK,
    MAX_PROGRESS_EVENT_BYTES,
    MAX_REPORT_FRAME_BYTES,
    RESERVED_TERMINAL_EVENT_SLOTS,
)

from .conftest import (
    ATTEMPT,
    INVOCATION,
    OTHER_ATTEMPT,
    OTHER_TASK,
    REPORT,
    TASK,
    make_record,
    report_body,
)

PATH = "/internal/v1/agent/task/report"
ERROR_SCHEMA = "errors.schema.json#/$defs/error_response"
REPORT_SCHEMA = "internal-adapters.schema.json#/$defs/report_response"


# ---------------------------------------------------------------------------
# The accepted path
# ---------------------------------------------------------------------------


async def test_a_report_is_committed_and_returns_its_position(client, store, contract) -> None:
    """T6-AC01/AC05: the event is durable, and its allocated cursor comes back.

    Returning the sequence is what makes the protocol usable rather than
    fire-and-forget: the host learns its progress is committed, and at which
    position, so a later reconnecting reader and the host agree on the same ordering
    without a second read.
    """
    response = await client.post(PATH, json=report_body())

    assert response.status_code == 200
    body = response.json()
    assert contract(body, REPORT_SCHEMA) == []
    assert (body["report_id"], body["sequence"], body["event_id"]) == (REPORT, 1, f"{TASK}:1")

    committed = store.events[TASK]
    assert [(event.sequence, event.type) for event in committed] == [(1, "progress.updated")]
    assert committed[0].data["message"] == report_body()["data"]["message"]


async def test_the_producer_timestamp_is_retained_beside_the_server_timestamp(client, store) -> None:
    """A producer's clock is evidence, never the ordering key.

    Both are kept and they are not interchangeable: retention and ordering use the
    server's time, and the producer's is retained as what the host claimed. A host
    with a skewed clock therefore cannot reorder durable history — the worst it can
    do is report a time that disagrees with the server's, visibly.
    """
    await client.post(PATH, json=report_body(producer_timestamp="2020-01-01T00:00:00Z"))

    event = store.events[TASK][0]
    assert event.producer_timestamp == "2020-01-01T00:00:00Z"
    assert event.timestamp != event.producer_timestamp
    assert event.timestamp.endswith("Z")


async def test_a_null_producer_timestamp_is_accepted(client, store) -> None:
    """The contract permits null, so a host with no clock evidence is not blocked."""
    response = await client.post(PATH, json=report_body(producer_timestamp=None))

    assert response.status_code == 200
    assert store.events[TASK][0].producer_timestamp is None


async def test_the_event_inherits_run_identity_from_the_record_not_the_body(client, store) -> None:
    """T6-AC03: the durable event's run fields are the gateway's, not the caller's.

    The body's binding is compared and then discarded; the committed event takes its
    invocation, generation and attempt from the task row. That is the difference
    between a body that *binds* and a body that *selects* — the latter would let a
    verified worker write an event attributed to another execution.
    """
    await client.post(PATH, json=report_body())

    event = store.events[TASK][0]
    record = store.tasks[TASK]
    assert (event.invocation_id, event.generation, event.runtime_attempt_id) == (
        record.invocation_id,
        record.generation,
        record.runtime_attempt_id,
    )


# ---------------------------------------------------------------------------
# Binding: the body may not select its own authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"run": {"task_id": OTHER_TASK, "invocation_id": INVOCATION, "generation": 1}, "runtime_attempt_id": ATTEMPT},
        {"run": {"task_id": TASK, "invocation_id": "0f1e2d3c-4b5a-4968-8776-655443322110", "generation": 1}, "runtime_attempt_id": ATTEMPT},
        {"run": {"task_id": TASK, "invocation_id": INVOCATION, "generation": 2}, "runtime_attempt_id": ATTEMPT},
        {"run": {"task_id": TASK, "invocation_id": INVOCATION, "generation": 1}, "runtime_attempt_id": OTHER_ATTEMPT},
    ],
    ids=["foreign-task", "foreign-invocation", "wrong-generation", "wrong-attempt"],
)
async def test_a_binding_that_disagrees_with_the_proven_attempt_is_refused(client, store, contract, override) -> None:
    """T6-AC03: every field of the binding is compared, and none selects a target.

    The foreign-task case is the one with teeth: a worker holding a valid credential
    for its own run must not be able to append to another tenant's task by naming it.
    All four are parametrized because a check that compared three fields would pass a
    narrower test while leaving the fourth as a selector.
    """
    response = await client.post(PATH, json=report_body(attempt=override))

    assert response.status_code == 409
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "state_conflict"
    assert store.events[TASK] == []


async def test_binding_refusals_do_not_distinguish_which_field_was_wrong(client) -> None:
    """One message for all four mismatches.

    A response that named the offending field would let a worker probe the gateway's
    view of other executions one field at a time — learning a live generation number
    or attempt ID by elimination. The correct action is identical in every case: stop
    and re-bootstrap.
    """
    wrong_task = await client.post(
        PATH,
        json=report_body(attempt={"run": {"task_id": OTHER_TASK, "invocation_id": INVOCATION, "generation": 1}, "runtime_attempt_id": ATTEMPT}),
    )
    wrong_generation = await client.post(
        PATH,
        json=report_body(attempt={"run": {"task_id": TASK, "invocation_id": INVOCATION, "generation": 2}, "runtime_attempt_id": ATTEMPT}),
    )

    assert wrong_task.json()["message"] == wrong_generation.json()["message"]
    assert OTHER_TASK not in wrong_task.json()["message"]


async def test_a_superseded_attempt_cannot_report(client, store, contract) -> None:
    """Invariant LC-10: late output from a replaced worker alters no outcome.

    The attempt is genuinely proven here — the transport is not lying — but the task
    row has moved on to a replacement. This is the case a pre-read check would get
    wrong under concurrency, so the fence travels into the conditional write; the
    assertion is that the refusal happens and consumes no sequence.
    """
    store.tasks[TASK] = make_record(runtime_attempt_id=OTHER_ATTEMPT)

    response = await client.post(PATH, json=report_body())

    assert response.status_code == 409
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert store.events[TASK] == []
    assert store.tasks[TASK].latest_sequence == 0, "a fenced report must not consume a sequence"


async def test_a_superseded_generation_cannot_report(client, store, attempt) -> None:
    """The generation fence, exercised with the attempt ID agreeing.

    Separated from the attempt case because they fail in different places: the
    binding check compares the body against the credential, while this compares the
    credential against the task row. A worker whose generation was superseded but
    whose attempt ID still matched would pass the first and must fail the second.
    """
    store.tasks[TASK] = make_record(generation=3)
    attempt[0] = report_routes.VerifiedAttempt(task_id=TASK, invocation_id=INVOCATION, generation=1, runtime_attempt_id=ATTEMPT)

    response = await client.post(PATH, json=report_body())

    assert response.status_code == 409
    assert store.events[TASK] == []


async def test_an_unverified_caller_is_refused_before_the_body_is_read(client, monkeypatch, contract) -> None:
    """No attempt authenticator configured is a 503, and the body is never parsed.

    Asserted by making the body unreadable: if the route buffered and parsed up to
    64 KiB before checking who was calling, an unproven caller could make it do that
    work at will.
    """
    monkeypatch.setattr(report_routes, "_AUTHENTICATOR", None)

    response = await client.post(PATH, content=b"{ this is not json", headers={"Content-Type": "application/json"})

    assert response.status_code == 503
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "prerequisite_unavailable"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_an_identical_retry_replays_rather_than_double_reporting(client, store, contract) -> None:
    """T6-AC01: retrying after a lost response is free.

    A host cannot distinguish a lost response from a lost request, so the only safe
    protocol is one where retrying costs nothing. Both calls return the same
    sequence, and exactly one event exists — without this the host would have to
    choose between duplicating progress and dropping it.
    """
    first = await client.post(PATH, json=report_body())
    second = await client.post(PATH, json=report_body())

    assert first.json() == second.json()
    assert contract(second.json(), REPORT_SCHEMA) == []
    assert len(store.events[TASK]) == 1


async def test_the_same_report_id_with_different_content_conflicts(client, store, contract) -> None:
    """A reused report ID carrying new content is a producer bug, not a retry.

    Committing it would give one report ID two meanings in durable history; silently
    keeping the first would leave the host believing it had reported something the
    record never carried. 409 names the disagreement.
    """
    await client.post(PATH, json=report_body())

    response = await client.post(PATH, json=report_body(data={"message": "different text entirely", "stage": "synthesis"}))

    assert response.status_code == 409
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "idempotency_conflict"
    assert len(store.events[TASK]) == 1


async def test_distinct_report_ids_each_commit_in_order(client, store) -> None:
    """T6-AC01: two authored updates become two ordered durable events."""
    await client.post(PATH, json=report_body())
    second = report_body(report_id="4e963a33-5cf8-442a-a9d8-e0d2c42b6b30", data={"message": "second update", "stage": "synthesis"})
    await client.post(PATH, json=second)

    assert [(event.sequence, event.data["stage"]) for event in store.events[TASK]] == [(1, "analysis"), (2, "synthesis")]


# ---------------------------------------------------------------------------
# What a host may author
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("task.completed", {"status": "completed", "version": 5, "outcome": "completed"}),
        ("task.failed", {"status": "failed", "version": 5, "outcome": "failed", "error_code": "process_failed"}),
        ("run.completed", {"outcome": "completed"}),
        ("history.gap", {"omitted_from_cursor": f"{TASK}:1", "omitted_to_cursor": f"{TASK}:2", "omitted_report_ids": [], "reason": "retention"}),
        ("task.queued", {"status": "queued", "version": 2}),
        ("input.consumed", {"command_id": "cmd_1", "command_status": "consumed", "handoff": "confirmed", "turn_id": "t1", "turn_number": 1}),
    ],
    ids=["task-completed", "task-failed", "run-completed", "history-gap", "task-queued", "input-consumed"],
)
async def test_a_host_may_not_author_server_owned_evidence(client, store, contract, kind, data) -> None:
    """T6-AC03/AC05: a worker cannot declare its own task's state.

    Every kind here carries a ``status``, ``version``, terminal ``outcome`` or command
    receipt — all server-owned. Accepting ``task.completed`` would let a worker
    declare itself complete with its own version number, bypassing the finalization
    path that first stores and verifies result artifacts.

    403 rather than 400 on purpose: the kind is valid and simply not the host's to
    author, and naming that distinction is what stops the refusal being "fixed" by
    widening the allowlist.
    """
    response = await client.post(PATH, json=report_body(event_type=kind, data=data))

    assert response.status_code == 403
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "disallowed_scope"
    assert store.events[TASK] == []


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("progress.updated", {"message": "Correlating pool timeouts.", "stage": "analysis"}),
        ("artifact.created", {"artifact_id": "art_7c1e4d92-5a6b-4c8d-9e01-2f3a4b5c6d70", "content_sha256": "a" * 64}),
        ("input.required", {"input_request_id": "9f1b2044-5c6d-4e7f-8a91-2b3c4d5e6f70", "prompt": "Which environment?"}),
    ],
    ids=sorted(report_routes.HOST_REPORTABLE_EVENT_TYPES),
)
async def test_every_host_reportable_kind_is_actually_reportable(client, store, kind, data) -> None:
    """The allowlist is reachable, not aspirational.

    Guards the pairing between ``HOST_REPORTABLE_EVENT_TYPES`` and the data rules: a
    kind on the allowlist whose required data a host cannot supply would be a
    permission granted in name only, and the failure would surface the first time a
    producer tried it.
    """
    response = await client.post(PATH, json=report_body(event_type=kind, data=data))

    assert response.status_code == 200, response.json()
    assert store.events[TASK][0].type == kind


def test_the_allowlist_excludes_every_kind_carrying_server_owned_data() -> None:
    """Derived from the data rules rather than restated.

    A kind whose contract data requires ``status``, ``version``, a ``command_id`` or
    an ``outcome`` is authored from the gateway's own committed state. Checking the
    allowlist against that rule means adding such a kind to it fails here, rather
    than becoming a way for a producer to write state.
    """
    server_owned = {"status", "version", "command_id", "outcome"}

    for kind in report_routes.HOST_REPORTABLE_EVENT_TYPES:
        assert not server_owned & set(REQUIRED_EVENT_DATA.get(kind, ())), f"{kind} carries server-owned data"


async def test_malformed_event_data_is_a_client_error_not_a_permission_error(client, store, contract) -> None:
    """A permitted kind with bad data is 400 — the host can fix it.

    Distinguished from the 403 above because the actions differ: a 400 means correct
    the payload and retry, a 403 means this is not yours to send. Conflating them
    would send a host into a retry loop over a report that can never be accepted, or
    have it abandon one that would succeed.
    """
    response = await client.post(PATH, json=report_body(data={"message": "m", "stage": "not-a-real-stage"}))

    assert response.status_code == 400
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "invalid_request"
    assert store.events[TASK] == []


async def test_unknown_data_keys_are_refused_rather_than_dropped(client, store) -> None:
    """Free-form diagnostics cannot ride along into a client-visible history.

    The durable event is externally readable, so an unrecognized key is a refusal. A
    passthrough would put whatever the producer felt like sending — model reasoning,
    raw tool output — into a record the contract says is a closed shape.
    """
    response = await client.post(PATH, json=report_body(data={"message": "m", "stage": "analysis", "internal_reasoning": "chain of thought"}))

    assert response.status_code == 400
    assert store.events[TASK] == []


async def test_a_refusal_does_not_echo_the_reported_text(client) -> None:
    """T6-AC05: agent-authored text never comes back in an error body.

    Report data is untrusted content that may carry task instructions and tool
    output. Pydantic echoes offending values into its messages by default, so the
    detail is deliberately dropped — reflecting it would put that content into the
    response and into every log line recording one.
    """
    secret = "tenant-confidential-instruction-text"

    response = await client.post(PATH, json=report_body(data={"message": secret, "stage": "not-a-stage"}))

    assert response.status_code == 400
    assert secret not in json.dumps(response.json())


async def test_an_unknown_body_field_is_refused(client, contract) -> None:
    """``extra="forbid"``, mirroring the schema.

    A producer sending a field this gateway does not implement is a version mismatch.
    Ignoring it would let the host believe it had reported something the durable
    record never carried.
    """
    response = await client.post(PATH, json={**report_body(), "priority": "high"})

    assert response.status_code == 400
    assert contract(response.json(), ERROR_SCHEMA) == []


async def test_a_malformed_body_is_400_and_never_422(client, contract) -> None:
    """422 is not in the Task API's status table.

    A declared FastAPI body parameter would render every validation failure as 422 —
    a status and body ``errors.schema.json`` has no code for, from a path that
    otherwise looks like it works. This is why the route parses the body itself.
    """
    response = await client.post(PATH, content=b"not json at all", headers={"Content-Type": "application/json"})

    assert response.status_code == 400
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# Size and budget: T6-AC04
# ---------------------------------------------------------------------------


async def test_an_oversize_frame_is_refused_without_being_parsed(client, contract) -> None:
    """413 on the frame, checked against the bytes rather than the header.

    ``Content-Length`` is a claim and a chunked request carries none, so the limit is
    applied to what actually arrived. A limit enforced only on the header is one a
    client can opt out of.
    """
    response = await client.post(PATH, content=b"x" * (MAX_REPORT_FRAME_BYTES + 1), headers={"Content-Type": "application/json"})

    assert response.status_code == 413
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "payload_too_large"


async def test_oversize_event_data_is_refused_not_truncated(client, store, contract) -> None:
    """A truncated progress message would be a durable misquote.

    Refusing lets the host log it and shorten; truncating would commit an
    externally-readable record that misstates what the agent reported, with nothing
    marking it as incomplete.
    """
    response = await client.post(PATH, json=report_body(data={"message": "m" * 4000, "stage": "analysis", "reason": "r" * MAX_PROGRESS_EVENT_BYTES}))

    assert response.status_code == 413
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert store.events[TASK] == []


async def test_an_exhausted_event_budget_is_explicit_and_keeps_terminal_capacity(client, store, contract) -> None:
    """T6-AC04: the flood is refused; the outcome can still be recorded.

    429 rather than 413 — the individual report is fine, the task has used its
    allowance. The assertion that matters is the second one: the reserved tail still
    accepts a terminal event, so refusing progress here does not make the task's
    ending unrecordable, which would turn a bounded budget into missing terminal
    evidence.
    """
    store.tasks[TASK] = make_record(events_allocated=MAX_EVENTS_PER_TASK - RESERVED_TERMINAL_EVENT_SLOTS)

    response = await client.post(PATH, json=report_body())

    assert response.status_code == 429
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "rate_limited"
    assert response.headers["retry-after"]

    terminal = store.append_event(
        task_id=TASK,
        report_id=None,
        event_type="task.failed",
        data={"status": "failed", "version": 5, "outcome": "failed", "error_code": "event_budget_exhausted"},
        producer_timestamp=None,
        timestamp="2026-09-24T14:44:00Z",
    )
    assert terminal.event.type == "task.failed"


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------


async def test_a_disabled_worker_surface_refuses_every_report(client, monkeypatch, contract) -> None:
    """The write surface has its own flag; read being enabled does not enable it."""
    monkeypatch.delenv(http.FLAG_WORKER, raising=False)
    monkeypatch.setenv(http.FLAG_READ, "true")

    response = await client.post(PATH, json=report_body())

    assert response.status_code == 503
    assert contract(response.json(), ERROR_SCHEMA) == []


async def test_a_storage_outage_is_a_retryable_503(client, store, contract) -> None:
    """An outage is not a caller error.

    The host must be able to tell "stop, you are superseded" (409) from "try again"
    (503). Rendering an outage as a conflict would have a healthy worker abandon
    progress it could have delivered.
    """
    store.fail = True

    response = await client.post(PATH, json=report_body())

    assert response.status_code == 503
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert response.json()["code"] == "prerequisite_unavailable"


async def test_an_absent_task_row_does_not_become_a_committed_event(client, store, contract) -> None:
    """A proven attempt for a task that is gone is not appended anywhere.

    The store raises rather than creating the row, which matters because the
    alternative — an append that implicitly creates a task — would let a report
    conjure a task with no admission, no tenant and no ownership.
    """
    store.tasks.pop(TASK)
    store.events.pop(TASK)

    response = await client.post(PATH, json=report_body())

    assert response.status_code == 503
    assert contract(response.json(), ERROR_SCHEMA) == []
    assert TASK not in store.tasks


def test_the_record_is_the_only_source_of_run_identity() -> None:
    """``VerifiedAttempt`` is frozen, so nothing can reassign it post-verification.

    Guards the seam rather than a route: code that could set ``task_id`` after
    verification would have rebuilt the body-selects-authority defect the whole
    module is arranged to prevent, and it would look like ordinary assignment.
    """
    attempt = report_routes.VerifiedAttempt(task_id=TASK, invocation_id=INVOCATION, generation=1, runtime_attempt_id=ATTEMPT)

    with pytest.raises(AttributeError):
        attempt.task_id = OTHER_TASK  # type: ignore[misc]

    assert attempt.task_id == TASK
