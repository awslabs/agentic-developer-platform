"""Acceptance, fencing and event allocation against real DynamoDB semantics (#5794).

These tests run on ``moto``'s DynamoDB rather than stubs, because every property
under test *is* a DynamoDB semantic: whether ``attribute_not_exists`` in a
transaction really serialises concurrent writers, whether a failed transaction
really leaves nothing behind, whether a conditional update really refuses a stale
version. A stub asserting request shapes would pass while the behaviour was wrong.

Where a fault has to be injected (a transaction that fails after being issued),
the client is wrapped so the call reaches DynamoDB or fails exactly as configured —
the point being to observe committed state afterwards, not to assert a call shape.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Lock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from src.tasks.records import (
    META_SORT_KEY,
    WORK_DUE_ATTRIBUTE,
    WORK_INDEX_NAME,
    WORK_SHARD_ATTRIBUTE,
    TaskState,
    command_sort_key,
    idempotency_partition,
    task_commands_partition,
    task_events_partition,
    task_partition,
    task_work_partition,
    work_shard,
)
from src.tasks.store import (
    TTL_ATTRIBUTE,
    AcceptanceRequest,
    IdempotencyConflictError,
    StaleGenerationError,
    TaskStateConflictError,
    TaskStore,
    TaskStoreError,
    assert_ttl_permitted,
)

TABLE = "adp-test-webhook-events"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def client():
    """A table with the legacy GSIs *and* the new work index, as production has.

    The legacy indexes are declared deliberately: their presence is what makes the
    invisibility assertions meaningful. Against a table without them, a task record
    carrying ``tenant_id`` would pass unnoticed.
    """
    with mock_aws():
        ddb = boto3.client(
            "dynamodb",
            region_name="us-east-1",
            aws_access_key_id="testing",  # noqa: S106 — moto requires non-empty credentials
            aws_secret_access_key="testing",  # noqa: S106
        )
        ddb.create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "arrived_at", "AttributeType": "S"},
                {"AttributeName": "tenant_id", "AttributeType": "S"},
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "correlation_id", "AttributeType": "S"},
                {"AttributeName": "root_human_id", "AttributeType": "S"},
                {"AttributeName": "engine_command_status", "AttributeType": "S"},
                {"AttributeName": WORK_SHARD_ATTRIBUTE, "AttributeType": "S"},
                {"AttributeName": WORK_DUE_ATTRIBUTE, "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "event_id", "KeyType": "HASH"},
                {"AttributeName": "arrived_at", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[
                _gsi("tenant-index", "tenant_id"),
                _gsi("user-index", "user_id"),
                _gsi("correlation-index", "correlation_id"),
                _gsi("root-human-index", "root_human_id"),
                _gsi("engine-command-index", "engine_command_status"),
                {
                    "IndexName": WORK_INDEX_NAME,
                    "KeySchema": [
                        {"AttributeName": WORK_SHARD_ATTRIBUTE, "KeyType": "HASH"},
                        {"AttributeName": WORK_DUE_ATTRIBUTE, "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        )
        yield ddb


def _gsi(name: str, attribute: str) -> dict:
    return {
        "IndexName": name,
        "KeySchema": [
            {"AttributeName": attribute, "KeyType": "HASH"},
            {"AttributeName": "arrived_at", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    }


@pytest.fixture
def store(client) -> TaskStore:
    return TaskStore(table_name=TABLE, dynamodb_client=client, clock=lambda: NOW)


@pytest.fixture
def concurrent_store(client) -> TaskStore:
    """A store usable from several threads against moto. See :class:`_SerializedClient`."""
    return TaskStore(table_name=TABLE, dynamodb_client=_SerializedClient(client), clock=lambda: NOW)


def _request(**overrides) -> AcceptanceRequest:
    defaults = {
        "task_id": f"tsk_{uuid.uuid4()}",
        "invocation_id": str(uuid.uuid4()),
        "dispatch_id": str(uuid.uuid4()),
        "tenant": "tenant-a",
        "canonical_principal": "svc-principal-1",
        "idempotency_key": "key-1",
        "persona": "agent-task-investigator",
        "request_payload": {"instructions": "investigate", "inputs": {"a": 1}},
        "deadline_at": NOW + timedelta(hours=1),
        "grant_reference": "GRANT#abc123",
    }
    return AcceptanceRequest(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# T1-AC01: idempotent concurrency yields one stable task
# ---------------------------------------------------------------------------


def test_acceptance_commits_every_record_in_one_transaction(store, client):
    """Metadata, idempotency, run, first event and dispatch intent are all durable."""
    request = _request()
    accepted = store.accept(request)

    assert accepted.state is TaskState.ACCEPTED
    assert accepted.replayed is False
    assert accepted.version == 1

    assert store.read_task(request.task_id) is not None
    assert _item(
        client,
        idempotency_partition(tenant=request.tenant, canonical_principal=request.canonical_principal, idempotency_key=request.idempotency_key),
        META_SORT_KEY,
    )
    assert _query_count(client, task_events_partition(request.task_id)) == 1
    assert _query_count(client, task_work_partition(request.task_id)) == 1


def test_concurrent_same_key_submissions_yield_one_task_and_one_dispatch_intent(concurrent_store, client):
    """The core race: N clients, one idempotency key, one task.

    Each thread generates its own task and dispatch IDs, exactly as N independent
    API calls would. Only the winner's IDs may become durable; every caller must be
    told about that same task.
    """
    store = concurrent_store
    requests = [_request(idempotency_key="shared-key") for _ in range(8)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(store.accept, requests))

    task_ids = {result.task_id for result in results}
    assert len(task_ids) == 1, f"expected one task, got {task_ids}"

    winner = task_ids.pop()
    assert sum(1 for result in results if not result.replayed) == 1, "exactly one caller created the task"
    assert all(result.request_digest == results[0].request_digest for result in results)

    # Exactly one dispatch intent exists overall — no loser's intent leaked
    # through, which would dispatch the same work twice.
    intents = sum(_query_count(client, task_work_partition(request.task_id)) for request in requests)
    assert intents == 1
    assert _query_count(client, task_work_partition(winner)) == 1

    # And exactly one task metadata row across every attempted ID.
    tasks = sum(1 for request in requests if store.read_task(request.task_id) is not None)
    assert tasks == 1


def test_replay_of_the_same_request_returns_the_original_task(store):
    """A retry after a lost response converges on the first task, not a new one."""
    first = store.accept(_request(idempotency_key="k"))
    second = store.accept(_request(idempotency_key="k"))

    assert second.task_id == first.task_id
    assert second.invocation_id == first.invocation_id
    assert second.dispatch_id == first.dispatch_id
    assert second.replayed is True
    assert first.replayed is False


def test_replayed_result_reports_current_state_not_the_original(store):
    """A replay of a task that has since started reports `running`, honestly."""
    first = store.accept(_request(idempotency_key="k"))
    store.transition(task_id=first.task_id, expected_version=1, target_state=TaskState.QUEUED)
    store.transition(task_id=first.task_id, expected_version=2, target_state=TaskState.RUNNING)

    replay = store.accept(_request(idempotency_key="k"))
    assert replay.state is TaskState.RUNNING
    assert replay.version == 3


def test_reusing_a_key_with_a_different_payload_is_a_defined_conflict(store):
    """Same key, different request: 409, naming the task the key already refers to."""
    first = store.accept(_request(idempotency_key="k", request_payload={"instructions": "one"}))

    with pytest.raises(IdempotencyConflictError) as raised:
        store.accept(_request(idempotency_key="k", request_payload={"instructions": "two"}))

    assert raised.value.task_id == first.task_id
    assert raised.value.stored_digest == first.request_digest
    assert raised.value.supplied_digest != first.request_digest


def test_a_semantically_equal_payload_replays_rather_than_conflicting(store):
    """Key order and formatting must not turn a retry into a false conflict."""
    first = store.accept(_request(idempotency_key="k", request_payload={"a": 1, "b": {"x": 1, "y": 2}}))
    second = store.accept(_request(idempotency_key="k", request_payload={"b": {"y": 2, "x": 1}, "a": 1}))
    assert second.task_id == first.task_id
    assert second.replayed is True


def test_idempotency_is_scoped_per_tenant_and_principal(store):
    """The same key string from a different tenant is a different task."""
    a = store.accept(_request(idempotency_key="same", tenant="tenant-a"))
    b = store.accept(_request(idempotency_key="same", tenant="tenant-b"))
    c = store.accept(_request(idempotency_key="same", canonical_principal="svc-other"))

    assert len({a.task_id, b.task_id, c.task_id}) == 3
    assert not any(result.replayed for result in (a, b, c))


# ---------------------------------------------------------------------------
# T1-AC02: a failed or partial write is never reported accepted
# ---------------------------------------------------------------------------


def test_a_failed_acceptance_transaction_leaves_no_task_and_no_dispatch_intent(store, client):
    """The property that matters: no executable unbound assignment survives.

    The transaction is made to fail after being issued. Afterwards nothing may be
    readable — not the task, not the idempotency row, and above all not the
    dispatch intent, which a publisher would otherwise turn into real work for a
    task that has no metadata and no authority behind it.
    """
    request = _request()
    store._client = _FailingClient(client, fail_on="transact_write_items")

    with pytest.raises(TaskStoreError):
        store.accept(request)

    store._client = client
    assert store.read_task(request.task_id) is None
    assert _query_count(client, task_work_partition(request.task_id)) == 0
    assert _query_count(client, task_events_partition(request.task_id)) == 0
    assert _query_count(client, task_partition(request.task_id)) == 0
    assert not _item(
        client,
        idempotency_partition(tenant=request.tenant, canonical_principal=request.canonical_principal, idempotency_key=request.idempotency_key),
        META_SORT_KEY,
    )


def test_an_unconfirmed_acceptance_raises_rather_than_reporting_accepted(store, client):
    """Unknown must surface as unknown (503), never as a 202 the caller can trust."""
    store._client = _FailingClient(client, fail_on="transact_write_items")
    with pytest.raises(TaskStoreError, match="not confirmed"):
        store.accept(_request())


def test_a_retryable_failure_is_not_reported_as_a_conflict(store, client):
    """A transport fault must not masquerade as a definitive idempotency conflict."""
    store._client = _FailingClient(client, fail_on="transact_write_items", code="ProvisionedThroughputExceededException")
    with pytest.raises(TaskStoreError):
        store.accept(_request())


def test_the_same_key_can_be_accepted_after_a_failed_attempt(store, client):
    """A failed attempt must not burn the idempotency key permanently."""
    request = _request(idempotency_key="k")
    store._client = _FailingClient(client, fail_on="transact_write_items")
    with pytest.raises(TaskStoreError):
        store.accept(request)

    store._client = client
    accepted = store.accept(_request(idempotency_key="k"))
    assert accepted.replayed is False


def test_acceptance_rejects_a_forged_task_identifier(store):
    with pytest.raises(Exception, match="task_id"):
        store.accept(_request(task_id="tsk_../../etc/passwd"))


# ---------------------------------------------------------------------------
# T1-AC03: legacy invisibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "index,attribute",
    [
        ("tenant-index", "tenant_id"),
        ("user-index", "user_id"),
        ("correlation-index", "correlation_id"),
        ("root-human-index", "root_human_id"),
        ("engine-command-index", "engine_command_status"),
    ],
)
def test_task_records_are_absent_from_every_legacy_index(store, client, index, attribute):
    """Structural, not conventional: the indexes are queried and return nothing.

    A full task lifecycle is written, then each legacy index is scanned. Because
    task records omit these hash-key attributes entirely, DynamoDB never projects
    them — so no Activity query can surface a task as a phantom run.
    """
    request = _request()
    store.accept(request)
    store.transition(task_id=request.task_id, expected_version=1, target_state=TaskState.QUEUED, event_kind="task.queued")
    store.transition(task_id=request.task_id, expected_version=2, target_state=TaskState.RUNNING, event_kind="task.running")
    store.append_event(task_id=request.task_id, kind="task.progress")

    projected = client.scan(TableName=TABLE, IndexName=index)
    assert projected["Count"] == 0, f"task records leaked into {index}"


def test_legacy_invocation_queries_are_byte_identical_before_and_after_task_records(store, client):
    """An existing invocation's query result must not change at all.

    The real legacy read path is `Query(event_id == invocation_id)`. A pre-existing
    webhook row is recorded, a full task lifecycle is written alongside it, and the
    identical query is replayed: same items, same count.
    """
    invocation_id = str(uuid.uuid4())
    client.put_item(
        TableName=TABLE,
        Item={
            "event_id": {"S": invocation_id},
            "arrived_at": {"S": "2026-09-24T11:00:00Z"},
            "tenant_id": {"S": "tenant-a"},
            "user_id": {"S": "user-1"},
            "correlation_id": {"S": "corr-1"},
            "root_human_id": {"S": "human-1"},
            "engine_command_status": {"S": "pending"},
        },
    )

    def legacy_query() -> dict:
        return client.query(
            TableName=TABLE,
            KeyConditionExpression="event_id = :id",
            ExpressionAttributeValues={":id": {"S": invocation_id}},
            ScanIndexForward=False,
        )

    before = legacy_query()

    # The task deliberately reuses the same invocation ID, the worst case: if any
    # task record used the bare invocation ID as its partition, this query would
    # return it and the task would be counted as a run.
    request = _request(invocation_id=invocation_id)
    store.accept(request)
    store.transition(task_id=request.task_id, expected_version=1, target_state=TaskState.QUEUED, event_kind="task.queued")

    after = legacy_query()
    assert after["Items"] == before["Items"]
    assert after["Count"] == before["Count"] == 1


def test_no_task_record_uses_a_bare_invocation_id_as_its_partition(store, client):
    """Every task partition is namespaced, so no legacy exact-key read can reach it."""
    request = _request()
    store.accept(request)
    rows = client.scan(TableName=TABLE)["Items"]
    task_rows = [row for row in rows if "record_type" in row]
    assert task_rows
    for row in task_rows:
        assert "#" in row["event_id"]["S"], row["event_id"]["S"]
        assert row["event_id"]["S"].split("#")[0].startswith("TASK")


def test_tenant_scope_travels_nested_and_never_as_a_top_level_attribute(store, client):
    request = _request()
    store.accept(request)
    for row in client.scan(TableName=TABLE)["Items"]:
        assert "tenant_id" not in row
        if "scope" in row:
            assert row["scope"]["M"]["tenant"]["S"] == request.tenant


# ---------------------------------------------------------------------------
# State transitions: version and generation fencing
# ---------------------------------------------------------------------------


def test_a_stale_version_loses_and_learns_the_actual_state(store):
    """Two writers, one version: the loser is told what actually won.

    The second call requests a transition that is legal from the current state, so
    the *only* thing that can refuse it is the version fence — not the lifecycle
    guard. That distinction is the point of the test.
    """
    accepted = store.accept(_request())
    store.transition(task_id=accepted.task_id, expected_version=1, target_state=TaskState.QUEUED)

    # `queued -> running` is permitted, but version 1 is stale: the write must
    # still be refused, purely on the fence.
    with pytest.raises(TaskStateConflictError) as raised:
        store.transition(task_id=accepted.task_id, expected_version=1, target_state=TaskState.RUNNING)

    assert raised.value.current_state == TaskState.QUEUED.value
    assert raised.value.current_version == 2
    assert store.read_task(accepted.task_id)["state"] == TaskState.QUEUED.value


def test_completion_and_cancellation_racing_resolve_to_exactly_one_outcome(concurrent_store):
    """The design's honesty rule, as a race rather than an assumption."""
    store = concurrent_store
    accepted = store.accept(_request())
    store.transition(task_id=accepted.task_id, expected_version=1, target_state=TaskState.QUEUED)
    store.transition(task_id=accepted.task_id, expected_version=2, target_state=TaskState.RUNNING)

    def complete():
        return store.transition(task_id=accepted.task_id, expected_version=3, target_state=TaskState.COMPLETED)

    def request_cancel():
        return store.transition(task_id=accepted.task_id, expected_version=3, target_state=TaskState.CANCEL_REQUESTED)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [pool.submit(complete), pool.submit(request_cancel)]
    results = []
    for future in outcomes:
        try:
            results.append(future.result())
        except TaskStateConflictError as conflict:
            results.append(conflict)

    succeeded = [result for result in results if isinstance(result, dict)]
    conflicted = [result for result in results if isinstance(result, TaskStateConflictError)]
    assert len(succeeded) == 1 and len(conflicted) == 1, "exactly one of the two must win"

    final = store.read_task(accepted.task_id)
    assert final["state"] == succeeded[0]["state"]
    # Whichever lost, it was told the winning state, so it can report reality:
    # cancellation surfaces the existing completion, or completion is refused
    # because cancellation latched first.
    assert conflicted[0].current_state == final["state"]
    assert final["state"] in {TaskState.COMPLETED.value, TaskState.CANCEL_REQUESTED.value}


def test_a_lost_update_is_refused_even_when_the_transition_is_still_legal(store):
    """Isolates the version fence from the lifecycle guard.

    A stale writer is usually also caught by the lifecycle guard, because the state
    it remembers is no longer current. This case defeats that second line of
    defence deliberately: `waiting_for_input -> cancel_requested` is permitted, so
    the guard has no objection, and the *only* thing that can refuse the stale
    write is the version condition. Without it the update would silently land on
    top of the committed one — a lost update.
    """
    accepted = store.accept(_request())
    store.transition(task_id=accepted.task_id, expected_version=1, target_state=TaskState.QUEUED)
    store.transition(task_id=accepted.task_id, expected_version=2, target_state=TaskState.RUNNING)
    # Two writers both hold version 3. The first commits.
    store.transition(task_id=accepted.task_id, expected_version=3, target_state=TaskState.WAITING_FOR_INPUT)

    with pytest.raises(TaskStateConflictError) as raised:
        store.transition(task_id=accepted.task_id, expected_version=3, target_state=TaskState.CANCEL_REQUESTED)

    assert raised.value.reason == "version_conflict", "the fence must be what refuses this, not the lifecycle guard"
    assert store.read_task(accepted.task_id)["state"] == TaskState.WAITING_FOR_INPUT.value
    assert store.read_task(accepted.task_id)["version"] == 4


def test_a_cancel_requested_task_can_never_be_completed(store):
    accepted = store.accept(_request())
    store.transition(task_id=accepted.task_id, expected_version=1, target_state=TaskState.QUEUED)
    store.transition(task_id=accepted.task_id, expected_version=2, target_state=TaskState.RUNNING)
    store.transition(task_id=accepted.task_id, expected_version=3, target_state=TaskState.CANCEL_REQUESTED)

    with pytest.raises(TaskStateConflictError) as raised:
        store.transition(task_id=accepted.task_id, expected_version=4, target_state=TaskState.COMPLETED)

    assert raised.value.reason == "transition_not_permitted"
    assert raised.value.current_state == TaskState.CANCEL_REQUESTED.value
    assert store.read_task(accepted.task_id)["state"] == TaskState.CANCEL_REQUESTED.value


def test_a_terminal_task_cannot_be_transitioned_again(store):
    accepted = store.accept(_request())
    store.transition(task_id=accepted.task_id, expected_version=1, target_state=TaskState.QUEUED)
    store.transition(task_id=accepted.task_id, expected_version=2, target_state=TaskState.RUNNING)
    store.transition(task_id=accepted.task_id, expected_version=3, target_state=TaskState.COMPLETED)

    with pytest.raises(TaskStateConflictError) as raised:
        store.transition(task_id=accepted.task_id, expected_version=4, target_state=TaskState.FAILED)

    # The refusal carries the outcome, so a caller can report the completion
    # rather than only "that failed".
    assert raised.value.current_state == TaskState.COMPLETED.value


def test_a_superseded_generation_cannot_write_to_the_live_run(store, client):
    """A replaced worker's late write is refused even with a valid version.

    Generation fencing is independent of the version fence: without it, a
    superseded worker that happened to guess the current version could overwrite
    the live run's state.
    """
    accepted = store.accept(_request(generation=2))
    store.transition(task_id=accepted.task_id, expected_version=1, target_state=TaskState.QUEUED)

    with pytest.raises(StaleGenerationError) as raised:
        store.transition(task_id=accepted.task_id, expected_version=2, target_state=TaskState.RUNNING, generation=1)

    assert raised.value.supplied == 1
    assert raised.value.current == 2
    assert store.read_task(accepted.task_id)["state"] == TaskState.QUEUED.value


def test_a_transition_cannot_rewrite_identity_or_the_fence_itself(store):
    accepted = store.accept(_request())
    for attribute in ("version", "state", "event_id", "scope", "task_id"):
        with pytest.raises(TaskStoreError, match="cannot be set"):
            store.transition(
                task_id=accepted.task_id,
                expected_version=1,
                target_state=TaskState.QUEUED,
                attributes={attribute: "forged"},
            )


def test_transitioning_an_unknown_task_is_a_conflict_not_a_crash(store):
    with pytest.raises(TaskStateConflictError) as raised:
        store.transition(task_id=f"tsk_{uuid.uuid4()}", expected_version=1, target_state=TaskState.QUEUED)
    assert raised.value.current_state is None


def test_a_storage_fault_during_a_transition_is_not_reported_as_a_conflict(store, client):
    """A 503 must not be dressed up as a state the caller can act on."""
    accepted = store.accept(_request())
    store._client = _FailingClient(client, fail_on="transact_write_items", code="InternalServerError")
    with pytest.raises(TaskStoreError):
        store.transition(task_id=accepted.task_id, expected_version=1, target_state=TaskState.QUEUED)


# ---------------------------------------------------------------------------
# Event sequence allocation
# ---------------------------------------------------------------------------


def test_event_sequences_are_dense_and_ordered(store):
    accepted = store.accept(_request())
    for index in range(5):
        store.append_event(task_id=accepted.task_id, kind="task.progress", data={"n": index})

    events = store.read_events(task_id=accepted.task_id, limit=50)
    assert [event["sequence"] for event in events] == [1, 2, 3, 4, 5, 6]
    assert events[0]["type"] == "task.accepted"


def test_concurrent_reporters_never_share_a_sequence_number(concurrent_store):
    """The counter update and the event row are one conditioned transaction."""
    store = concurrent_store
    accepted = store.accept(_request())

    def emit(index: int):
        try:
            return store.append_event(task_id=accepted.task_id, kind="task.progress", data={"n": index})
        except TaskStateConflictError:
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = [result for result in pool.map(emit, range(6)) if result]

    sequences = [result["sequence"] for result in results]
    assert len(sequences) == len(set(sequences)), "a sequence number was issued twice"

    stored = store.read_events(task_id=accepted.task_id, limit=50)
    stored_sequences = [event["sequence"] for event in stored]
    assert len(stored_sequences) == len(set(stored_sequences))
    # Dense from 1: the counter and the rows never disagree.
    assert stored_sequences == list(range(1, len(stored_sequences) + 1))


def test_a_failed_event_write_consumes_no_sequence_number(store, client):
    """A gap in history would break replay; the design forbids consuming on failure."""
    accepted = store.accept(_request())
    store._client = _FailingClient(client, fail_on="transact_write_items")
    with pytest.raises(TaskStoreError):
        store.append_event(task_id=accepted.task_id, kind="task.progress")

    store._client = client
    assert store.read_task(accepted.task_id)["event_sequence"] == 1
    result = store.append_event(task_id=accepted.task_id, kind="task.progress")
    assert result["sequence"] == 2


def test_a_stale_reporter_cannot_reuse_a_sequence_number_already_taken(store, client):
    """Deterministic interleaving, not a hopeful thread race.

    A thread race under a serialising fake can never produce a stale reader, so it
    cannot test this at all. Here the interleaving is forced: the reporter reads the
    counter, then a competing reporter commits sequence 2 *before* the first one's
    transaction is issued. The first writer is therefore stale by construction and
    must be refused rather than overwriting committed history.

    Two independent conditions each prevent this — the counter's expected-value
    check and the event row's ``attribute_not_exists`` — and mutation testing
    confirms either alone suffices, so this test fails only if both are lost. That
    redundancy is deliberate: the sequence is what replay depends on.
    """
    accepted = store.accept(_request())

    competitor = TaskStore(table_name=TABLE, dynamodb_client=client, clock=lambda: NOW)

    def commit_competing_event():
        competitor.append_event(task_id=accepted.task_id, kind="task.progress", data={"who": "competitor"})

    store._client = _InterleavingClient(client, before="transact_write_items", action=commit_competing_event)

    with pytest.raises(TaskStateConflictError):
        store.append_event(task_id=accepted.task_id, kind="task.progress", data={"who": "stale"})

    store._client = client
    events = store.read_events(task_id=accepted.task_id, limit=50)
    sequences = [event["sequence"] for event in events]
    assert sequences == [1, 2], "the stale write must not have landed"
    assert len(sequences) == len(set(sequences))
    # The competitor's event survived intact — the loser overwrote nothing.
    assert events[1]["data"]["who"] == "competitor"


def test_an_expected_sequence_mismatch_is_refused(store):
    accepted = store.accept(_request())
    with pytest.raises(TaskStateConflictError):
        store.append_event(task_id=accepted.task_id, kind="task.progress", expected_sequence=99)


def test_events_page_from_a_cursor_without_loss_or_repetition(store):
    accepted = store.accept(_request())
    for index in range(7):
        store.append_event(task_id=accepted.task_id, kind="task.progress", data={"n": index})

    collected: list[int] = []
    cursor = 0
    while True:
        page = store.read_events(task_id=accepted.task_id, after_sequence=cursor, limit=3)
        if not page:
            break
        collected.extend(event["sequence"] for event in page)
        cursor = page[-1]["sequence"]

    assert collected == list(range(1, 9))
    assert len(collected) == len(set(collected))


def test_the_first_page_includes_the_first_event(store):
    """An off-by-one in the cursor would silently hide `task.accepted`."""
    accepted = store.accept(_request())
    page = store.read_events(task_id=accepted.task_id, after_sequence=0, limit=10)
    assert page[0]["sequence"] == 1


def test_event_ids_use_the_public_cursor_form(store):
    accepted = store.accept(_request())
    event = store.append_event(task_id=accepted.task_id, kind="task.progress")
    assert event["event_id"] == f"{accepted.task_id}:2"


def test_a_state_transition_and_its_event_are_atomic(store, client):
    """Neither half may be observable without the other."""
    accepted = store.accept(_request())
    store._client = _FailingClient(client, fail_on="transact_write_items")
    with pytest.raises(TaskStoreError):
        store.transition(task_id=accepted.task_id, expected_version=1, target_state=TaskState.QUEUED, event_kind="task.queued")

    store._client = client
    assert store.read_task(accepted.task_id)["state"] == TaskState.ACCEPTED.value
    assert [event["type"] for event in store.read_events(task_id=accepted.task_id)] == ["task.accepted"]


# ---------------------------------------------------------------------------
# Commands and turns: the exactly-once consumption boundary
# ---------------------------------------------------------------------------


def _running_task(store) -> str:
    accepted = store.accept(_request())
    store.transition(task_id=accepted.task_id, expected_version=1, target_state=TaskState.QUEUED)
    store.transition(task_id=accepted.task_id, expected_version=2, target_state=TaskState.RUNNING)
    return accepted.task_id


def test_a_command_is_inserted_once_however_many_times_it_is_retried(store, client):
    task_id = _running_task(store)
    command_id = str(uuid.uuid4())
    kwargs = {
        "task_id": task_id,
        "command_id": command_id,
        "kind": "input",
        "payload": {"text": "more"},
        "author": "user-1",
        "authority_expires_at": NOW + timedelta(minutes=30),
    }

    first = store.insert_command(expected_version=3, **kwargs)
    second = store.insert_command(expected_version=4, **kwargs)

    assert second["command_id"] == first["command_id"]
    assert second["command_sequence"] == first["command_sequence"] == 1
    assert _query_count(client, task_commands_partition(task_id)) == 1


def test_concurrent_identical_commands_insert_exactly_one(concurrent_store, client):
    store = concurrent_store
    task_id = _running_task(store)
    command_id = str(uuid.uuid4())

    def insert(_):
        try:
            return store.insert_command(
                task_id=task_id,
                command_id=command_id,
                kind="input",
                payload={"text": "more"},
                author="user-1",
                authority_expires_at=NOW + timedelta(minutes=30),
                expected_version=3,
            )
        except (TaskStateConflictError, IdempotencyConflictError):
            return None

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(insert, range(5)))

    assert _query_count(client, task_commands_partition(task_id)) == 1


def test_reusing_a_command_id_with_different_content_is_refused(store):
    task_id = _running_task(store)
    command_id = str(uuid.uuid4())
    store.insert_command(
        task_id=task_id,
        command_id=command_id,
        kind="input",
        payload={"text": "one"},
        author="user-1",
        authority_expires_at=NOW + timedelta(minutes=30),
        expected_version=3,
    )

    with pytest.raises(IdempotencyConflictError):
        store.insert_command(
            task_id=task_id,
            command_id=command_id,
            kind="input",
            payload={"text": "two"},
            author="user-1",
            authority_expires_at=NOW + timedelta(minutes=30),
            expected_version=4,
        )


def test_new_input_is_refused_once_cancellation_is_requested(store):
    """Cancellation latches: no further work may be admitted behind it."""
    task_id = _running_task(store)
    store.transition(task_id=task_id, expected_version=3, target_state=TaskState.CANCEL_REQUESTED)

    with pytest.raises(TaskStateConflictError) as raised:
        store.insert_command(
            task_id=task_id,
            command_id=str(uuid.uuid4()),
            kind="input",
            payload={"text": "sneak"},
            author="user-1",
            authority_expires_at=NOW + timedelta(minutes=30),
            expected_version=4,
        )
    assert raised.value.current_state == TaskState.CANCEL_REQUESTED.value


def test_new_input_is_refused_on_a_terminal_task(store):
    task_id = _running_task(store)
    store.transition(task_id=task_id, expected_version=3, target_state=TaskState.COMPLETED)
    with pytest.raises(TaskStateConflictError):
        store.insert_command(
            task_id=task_id,
            command_id=str(uuid.uuid4()),
            kind="input",
            payload={"text": "late"},
            author="user-1",
            authority_expires_at=NOW + timedelta(minutes=30),
            expected_version=4,
        )


def test_a_command_is_consumed_by_exactly_one_turn(store, client):
    task_id = _running_task(store)
    command_id = str(uuid.uuid4())
    store.insert_command(
        task_id=task_id,
        command_id=command_id,
        kind="input",
        payload={"text": "go"},
        author="user-1",
        authority_expires_at=NOW + timedelta(minutes=30),
        expected_version=3,
    )

    store.commit_turn(task_id=task_id, turn_number=1, turn_id=str(uuid.uuid4()), command_ids=[command_id], expected_version=4)

    command = _item(client, task_commands_partition(task_id), command_sort_key(command_id))
    assert command["status"]["S"] == "consumed"
    assert int(command["turn_number"]["N"]) == 1

    # A second turn cannot claim the same command.
    with pytest.raises(TaskStateConflictError):
        store.commit_turn(task_id=task_id, turn_number=2, turn_id=str(uuid.uuid4()), command_ids=[command_id], expected_version=5)


def test_recovery_reads_an_existing_turn_instead_of_re_inserting_it(store, client):
    """Idempotent turn creation — the crash-recovery property V3 depends on."""
    task_id = _running_task(store)
    command_id = str(uuid.uuid4())
    store.insert_command(
        task_id=task_id,
        command_id=command_id,
        kind="input",
        payload={"text": "go"},
        author="user-1",
        authority_expires_at=NOW + timedelta(minutes=30),
        expected_version=3,
    )
    turn_id = str(uuid.uuid4())
    first = store.commit_turn(task_id=task_id, turn_number=1, turn_id=turn_id, command_ids=[command_id], expected_version=4)

    # Retry after a simulated crash: same turn number, freshly generated turn ID.
    second = store.commit_turn(task_id=task_id, turn_number=1, turn_id=str(uuid.uuid4()), command_ids=[command_id], expected_version=5)

    assert second["turn_id"] == first["turn_id"] == turn_id
    assert second["command_ids"] == [command_id]


def test_an_empty_turn_is_refused(store):
    task_id = _running_task(store)
    with pytest.raises(TaskStoreError, match="at least one command"):
        store.commit_turn(task_id=task_id, turn_number=1, turn_id=str(uuid.uuid4()), command_ids=[], expected_version=3)


# ---------------------------------------------------------------------------
# T1-AC05: recovery without scans, retention that protects active records
# ---------------------------------------------------------------------------


def test_due_work_is_found_by_a_sharded_index_query_not_a_scan(store, client):
    request = _request()
    store.accept(request)

    claimed = store.claim_due_work(shard=work_shard(request.task_id), now=NOW + timedelta(minutes=1))
    assert [record["dispatch_id"] for record in claimed] == [request.dispatch_id]


def test_the_work_index_holds_only_work_records(store, client):
    """Sparseness is the point: the index must not accumulate the whole table."""
    request = _request()
    store.accept(request)
    store.append_event(task_id=request.task_id, kind="task.progress")
    client.put_item(
        TableName=TABLE,
        Item={"event_id": {"S": str(uuid.uuid4())}, "arrived_at": {"S": "2026-09-24T11:00:00Z"}, "tenant_id": {"S": "t"}},
    )

    indexed = client.scan(TableName=TABLE, IndexName=WORK_INDEX_NAME)["Items"]
    assert len(indexed) == 1
    assert indexed[0]["record_type"]["S"] == "TASK_WORK"


def test_work_not_yet_due_is_not_claimed(store):
    request = _request()
    store.accept(request)
    assert store.claim_due_work(shard=work_shard(request.task_id), now=NOW - timedelta(minutes=5)) == []


def test_two_concurrent_recovery_passes_do_not_both_claim_the_same_work(store):
    """The lease is conditional, so one pass claims and the other sees nothing."""
    request = _request()
    store.accept(request)
    shard = work_shard(request.task_id)

    first = store.claim_due_work(shard=shard, now=NOW + timedelta(minutes=1))
    second = store.claim_due_work(shard=shard, now=NOW + timedelta(minutes=1))

    assert len(first) == 1
    assert second == []


def test_an_expired_lease_can_be_reclaimed(store):
    request = _request()
    store.accept(request)
    shard = work_shard(request.task_id)
    store.claim_due_work(shard=shard, now=NOW + timedelta(minutes=1), lease_seconds=30)
    reclaimed = store.claim_due_work(shard=shard, now=NOW + timedelta(minutes=5))
    assert len(reclaimed) == 1


def test_recovery_paging_is_bounded(store):
    with pytest.raises(TaskStoreError, match="between 1 and 100"):
        store.claim_due_work(shard="v1#00", now=NOW, limit=500)
    with pytest.raises(TaskStoreError, match="between 1 and 100"):
        store.claim_due_work(shard="v1#00", now=NOW, limit=0)


def test_an_active_task_carries_no_expiry_stamp(store, client):
    """No TTL while live — TTL deletion is asynchronous and cannot be recalled."""
    request = _request()
    store.accept(request)
    for row in client.scan(TableName=TABLE)["Items"]:
        assert TTL_ATTRIBUTE not in row, row["event_id"]["S"]


def test_a_terminal_task_is_stamped_with_the_content_retention_window(store):
    accepted = store.accept(_request())
    store.transition(task_id=accepted.task_id, expected_version=1, target_state=TaskState.QUEUED)
    store.transition(task_id=accepted.task_id, expected_version=2, target_state=TaskState.RUNNING)
    store.transition(task_id=accepted.task_id, expected_version=3, target_state=TaskState.COMPLETED)

    task = store.read_task(accepted.task_id)
    expected = int((NOW + timedelta(days=30)).timestamp())
    assert int(task[TTL_ATTRIBUTE]) == expected
    assert task["terminal_at"]


def test_the_idempotency_record_outlives_content_so_a_replay_is_not_a_new_task(store, client):
    """Content goes at day 30; the tombstone survives to day 90 to return 410."""
    request = _request(idempotency_key="k")
    store.accept(request)
    store.transition(task_id=request.task_id, expected_version=1, target_state=TaskState.QUEUED)
    store.transition(task_id=request.task_id, expected_version=2, target_state=TaskState.RUNNING)
    store.transition(task_id=request.task_id, expected_version=3, target_state=TaskState.COMPLETED)

    idempotency = _item(
        client,
        idempotency_partition(tenant=request.tenant, canonical_principal=request.canonical_principal, idempotency_key="k"),
        META_SORT_KEY,
    )
    assert TTL_ATTRIBUTE not in idempotency
    assert store.tombstone_expiry(NOW) > int((NOW + timedelta(days=30)).timestamp())


@pytest.mark.parametrize("record_type", ["TASK", "TASK_RUN", "TASK_IDEMP", "TASK_COMMANDS", "TASK_ARTIFACT"])
def test_an_expiry_stamp_on_a_live_record_is_refused(record_type):
    """The guard, not the convention: these records cannot be scheduled for deletion."""
    with pytest.raises(TaskStoreError, match="must not carry"):
        assert_ttl_permitted({"record_type": record_type, TTL_ATTRIBUTE: 1, "event_id": "TASK#x"})


@pytest.mark.parametrize("record_type", ["TASK", "TASK_IDEMP"])
def test_an_expiry_stamp_is_permitted_once_terminal(record_type):
    assert_ttl_permitted({"record_type": record_type, TTL_ATTRIBUTE: 1, "terminal_at": "2026-09-24T12:00:00Z"})


def test_marking_recovery_required_clears_any_expiry(store, client):
    """A task needing intervention must not be deleted while the question is open."""
    accepted = store.accept(_request())
    store.transition(task_id=accepted.task_id, expected_version=1, target_state=TaskState.QUEUED)
    store.transition(task_id=accepted.task_id, expected_version=2, target_state=TaskState.RUNNING)
    store.transition(task_id=accepted.task_id, expected_version=3, target_state=TaskState.FAILED)
    assert TTL_ATTRIBUTE in store.read_task(accepted.task_id)

    store.mark_recovery_required(task_id=accepted.task_id, reason="outcome_unconfirmed", expected_version=4)

    task = store.read_task(accepted.task_id)
    assert TTL_ATTRIBUTE not in task
    assert task["recovery_required"] is True
    assert task["execution_health"] == "unknown"


def test_marking_recovery_required_is_version_fenced(store):
    accepted = store.accept(_request())
    with pytest.raises(TaskStateConflictError):
        store.mark_recovery_required(task_id=accepted.task_id, reason="x", expected_version=99)


# ---------------------------------------------------------------------------
# Reads: consistency is a correctness requirement, not a preference
# ---------------------------------------------------------------------------


def test_task_reads_are_strongly_consistent(store, client):
    """A stale read could honour a superseded state or duplicate an execution."""
    recorder = _RecordingClient(client)
    store._client = recorder
    accepted = store.accept(_request())
    store.read_task(accepted.task_id)

    get_calls = [call for call in recorder.calls if call[0] == "get_item"]
    assert get_calls
    assert all(call[1].get("ConsistentRead") is True for call in get_calls)


def test_task_records_use_exact_key_reads_not_newest_row_queries(store, client):
    """`GetItem`, so a planted row under the same partition cannot decide the read."""
    recorder = _RecordingClient(client)
    store._client = recorder
    accepted = store.accept(_request())
    store.read_task(accepted.task_id)
    assert any(call[0] == "get_item" for call in recorder.calls)


def test_reading_an_unknown_task_returns_none_rather_than_raising(store):
    assert store.read_task(f"tsk_{uuid.uuid4()}") is None


def test_a_storage_fault_on_read_raises_rather_than_returning_none(store, client):
    """ "Unavailable" must not be indistinguishable from "no such task"."""
    store._client = _FailingClient(client, fail_on="get_item", code="InternalServerError")
    with pytest.raises(TaskStoreError):
        store.read_task(f"tsk_{uuid.uuid4()}")


def test_the_table_name_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "adp-prod-webhook-events")
    assert TaskStore(dynamodb_client=object()).table_name == "adp-prod-webhook-events"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FailingClient:
    """Passes calls through except one operation, which fails as configured.

    Used to observe committed state after a failure, which is the only way to test
    "a partial write is never reported accepted" — the assertion is about what
    survives in the table, not about how the call was made.
    """

    def __init__(self, inner, *, fail_on: str, code: str = "TransactionCanceledException"):
        self._inner = inner
        self._fail_on = fail_on
        self._code = code

    def __getattr__(self, name: str):
        if name != self._fail_on:
            return getattr(self._inner, name)

        def fail(**_kwargs):
            response = {"Error": {"Code": self._code, "Message": "injected failure"}}
            if self._code == "TransactionCanceledException":
                # Shaped like the real thing: no item-level condition failed, so
                # this is an ambiguous outcome rather than a decided conflict.
                response["CancellationReasons"] = [{"Code": "TransactionConflict"}]
            raise ClientError(response, name)

        return fail


class _SerializedClient:
    """Serialises calls into moto's backend while leaving the race intact.

    moto's ``transact_write_items`` takes a ``copy.deepcopy`` of the whole backend
    for rollback and holds no lock, so concurrent threads corrupt its internal
    dicts and crash inside moto itself. That is a limitation of the fake, not a
    property of DynamoDB.

    The lock covers only moto's bookkeeping. It does **not** pre-decide the
    outcome: threads still race to reach the call, and the winner is whichever
    transaction is evaluated first. What remains under test is the semantic that
    matters — that a conditional insert admits exactly one of N contending writers
    and cancels the rest. Real DynamoDB provides that serialisation server-side.

    Evidence note: this is mocked evidence of the condition semantics under
    contention, not evidence of real-DynamoDB concurrency behaviour. Genuine
    concurrent-admission proof belongs to V1 (#5802) against a deployed table.
    """

    _lock = Lock()

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name: str):
        inner = getattr(self._inner, name)

        def serialized(**kwargs):
            with _SerializedClient._lock:
                return inner(**kwargs)

        return serialized


class _InterleavingClient:
    """Runs ``action`` immediately before one operation, then passes the call through.

    Forces a specific interleaving that a thread race cannot reliably produce: the
    caller has already read state, and a competing writer commits in the gap before
    the caller's own write is issued. Used to make a writer stale by construction so
    the conditional write is the only thing that can refuse it.
    """

    def __init__(self, inner, *, before: str, action):
        self._inner = inner
        self._before = before
        self._action = action
        self._fired = False

    def __getattr__(self, name: str):
        inner = getattr(self._inner, name)
        if name != self._before:
            return inner

        def interleaved(**kwargs):
            if not self._fired:
                self._fired = True  # Fire once, so the competitor does not recurse.
                self._action()
            return inner(**kwargs)

        return interleaved


class _RecordingClient:
    """Passes calls through, recording operation names and keyword arguments."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name: str):
        inner = getattr(self._inner, name)

        def record(**kwargs):
            self.calls.append((name, kwargs))
            return inner(**kwargs)

        return record


def _item(client, partition: str, sort_key: str) -> dict | None:
    response = client.get_item(TableName=TABLE, Key={"event_id": {"S": partition}, "arrived_at": {"S": sort_key}}, ConsistentRead=True)
    return response.get("Item")


def _query_count(client, partition: str) -> int:
    return client.query(
        TableName=TABLE,
        KeyConditionExpression="event_id = :partition",
        ExpressionAttributeValues={":partition": {"S": partition}},
        Select="COUNT",
        ConsistentRead=True,
    )["Count"]
