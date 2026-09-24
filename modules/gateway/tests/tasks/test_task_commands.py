"""T7 transactions over the actual T1 acceptance store and DynamoDB semantics."""

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
import test_store as t1_fixtures
from test_store import NOW, _request

from src.tasks import errors
from src.tasks.records import META_SORT_KEY, task_ops_partition, task_partition, task_turns_partition
from src.tasks.store import _serialize
from src.tasks.task_commands import TaskCommands

client = t1_fixtures.client
store = t1_fixtures.store


def accepted(store):
    req = _request()
    store.accept(req)
    return req, TaskCommands(store)


def admit(service, req, *, kind="input", command_id=None, text="clarification"):
    return service.admit(
        task_id=req.task_id,
        command_id=command_id or str(uuid.uuid4()),
        kind=kind,
        payload={"text": text} if kind == "input" else {"reason": text},
        principal=req.canonical_principal,
        tenant=req.tenant,
        expires_at=NOW + timedelta(minutes=5),
    )


def test_cancellation_latches_atomically_and_replays_after_latch(store):
    req, service = accepted(store)
    cmd = str(uuid.uuid4())
    first = admit(service, req, kind="cancel", command_id=cmd)
    assert service.snapshot(req.task_id)["state"] == "cancel_requested"
    assert admit(service, req, kind="cancel", command_id=cmd) == first
    event = store.read_events(task_id=req.task_id)[-1]
    assert event["data"]["status"] == "cancel_requested"
    assert event["data"]["version"] == service.snapshot(req.task_id)["version"]
    with pytest.raises(errors.TaskApiError):
        admit(service, req)


def test_replay_is_same_receipt_but_changed_content_conflicts(store):
    req, service = accepted(store)
    cmd = str(uuid.uuid4())
    receipt = admit(service, req, command_id=cmd)
    assert admit(service, req, command_id=cmd) == receipt
    with pytest.raises(errors.TaskApiError, match="different content"):
        admit(service, req, command_id=cmd, text="different")
    assert len(service.commands(req.task_id)) == 1


def test_pending_limit_preserves_cancel_slot(store):
    req, service = accepted(store)
    for _ in range(10):
        admit(service, req)
    with pytest.raises(errors.TaskApiError):
        admit(service, req)
    assert admit(service, req, kind="cancel")["kind"] == "cancel"


def test_authority_revocation_prevents_command_commit(store):
    req, service = accepted(store)
    from src.tasks.records import task_authority_partition, task_policy_sort_key

    store._client.update_item(
        TableName=store.authority_table_name,
        Key=_serialize({"pk": task_authority_partition(req.tenant), "sk": task_policy_sort_key(req.canonical_principal)}),
        UpdateExpression="SET #s = :revoked",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=_serialize({":revoked": "revoked"}),
    )
    with pytest.raises(errors.TaskApiError):
        admit(service, req)
    assert service.commands(req.task_id) == []


def running(store, req):
    identity = SimpleNamespace(
        task_id=req.task_id,
        invocation_id=req.invocation_id,
        generation=1,
        runtime_attempt_id=str(uuid.uuid4()),
        tenant=req.tenant,
        canonical_principal=req.canonical_principal,
    )
    store._client.update_item(
        TableName=store.table_name,
        Key=_serialize({"event_id": task_partition(req.task_id), "arrived_at": META_SORT_KEY}),
        UpdateExpression="SET #s = :running, runtime_attempt_id = :attempt",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues=_serialize({":running": "running", ":attempt": identity.runtime_attempt_id}),
    )
    from src.tasks.records import task_authority_partition, task_run_grant_sort_key

    store._client.update_item(
        TableName=store.authority_table_name,
        Key=_serialize({"pk": task_authority_partition(req.tenant), "sk": task_run_grant_sort_key(invocation_id=req.invocation_id, generation=1)}),
        UpdateExpression="SET runtime_attempt_id = :attempt",
        ExpressionAttributeValues=_serialize({":attempt": identity.runtime_attempt_id}),
    )
    return identity


def final_body(identity):
    return {
        "schema_version": "1.0",
        "attempt": {key: getattr(identity, key) for key in ("task_id", "invocation_id", "generation", "runtime_attempt_id")},
        "final_report_id": str(uuid.uuid4()),
        "child_exit": {"confirmed": True, "exit_code": 0, "signal": None, "stopped_at": "2026-09-24T12:00:00Z"},
        "outcome": "completed",
        "result": {
            "schema_version": "1.0",
            "outcome": "completed",
            "report": {"summary": "done", "findings": [], "uncertainties": [], "recommendations": [], "evidence_refs": []},
            "committed_at": "2026-09-24T12:00:00Z",
            "process_exit_validated": True,
        },
        "error": None,
        "committed_result_refs": [],
    }


def model(store, req, turn_id):
    store._client.put_item(
        TableName=store.table_name,
        Item=_serialize(
            {"event_id": task_ops_partition(req.task_id), "arrived_at": "MODEL#" + turn_id, "turn_id": turn_id, "operation_status": "confirmed"}
        ),
    )


def turn(store, req, n):
    tid = str(uuid.uuid4())
    store._client.put_item(
        TableName=store.table_name, Item=_serialize({"event_id": task_turns_partition(req.task_id), "arrived_at": f"TURN#{n:020}", "turn_id": tid})
    )
    return tid


def test_finalize_fences_pending_or_consumed_but_unmodeled_input(store):
    req, service = accepted(store)
    identity = running(store, req)
    model(store, req, turn(store, req, 1))
    turn(store, req, 2)
    with pytest.raises(errors.TaskApiError, match="Every committed turn"):
        service.finalize(identity, final_body(identity))
    assert service.snapshot(req.task_id)["state"] == "running"


def test_completion_commits_once_and_cancellation_cannot_overwrite(store):
    req, service = accepted(store)
    identity = running(store, req)
    model(store, req, turn(store, req, 1))
    body = final_body(identity)
    result = service.finalize(identity, body)
    assert result["status"] == "completed"
    assert service.finalize(identity, body) == result
    with pytest.raises(errors.TaskApiError):
        admit(service, req, kind="cancel")


def test_stale_attempt_cannot_poll_or_finalize(store):
    req, service = accepted(store)
    identity = running(store, req)
    stale = SimpleNamespace(**(vars(identity) | {"runtime_attempt_id": str(uuid.uuid4())}))
    with pytest.raises(errors.TaskApiError):
        service.control(stale)
    with pytest.raises(errors.TaskApiError):
        service.finalize(stale, final_body(stale))


def test_cancel_then_complete_is_refused_and_cancel_finalizes_receipts(store):
    req, service = accepted(store)
    identity = running(store, req)
    admit(service, req)
    admit(service, req, kind="cancel")
    with pytest.raises(errors.TaskApiError):
        service.finalize(identity, final_body(identity))
    body = final_body(identity)
    body.update(
        outcome="cancelled",
        result=None,
        error={
            "schema_version": "1.0",
            "outcome": "cancelled",
            "code": "cancelled_by_client",
            "message": "Stopped by caller",
            "committed_at": "2026-09-24T12:00:00Z",
            "child_exit_confirmed": True,
            "recovery_required": False,
        },
    )
    assert service.finalize(identity, body)["status"] == "cancelled"
    rows = service.commands(req.task_id)
    assert all(row["status"] == "cancelled" for row in rows)
    assert all(row["handoff"] == "not_started" for row in rows)
    events = store.read_events(task_id=req.task_id)
    assert [row["type"] for row in events[-3:]] == ["command.updated", "command.updated", "task.cancelled"]


def test_settlement_only_releases_capacity_on_confirmed_stop_once(store):
    from src.tasks.records import task_authority_partition, task_run_grant_sort_key

    req, service = accepted(store)
    identity = running(store, req)
    grantkey = {"pk": task_authority_partition(req.tenant), "sk": task_run_grant_sort_key(invocation_id=req.invocation_id, generation=1)}
    keys = ["TASK_CAPACITY#" + "1" * 64, "TASK_CAPACITY#" + "2" * 64, "TASK_CAPACITY#" + "3" * 64]
    for key in keys:
        store._client.put_item(
            TableName=store.authority_table_name,
            Item=_serialize({"pk": key, "sk": "ACTIVE", "active_count": 1, "capacity_limit": 4, "reservations": {req.task_id: req.invocation_id}}),
        )
    store._client.update_item(
        TableName=store.authority_table_name,
        Key=_serialize(grantkey),
        UpdateExpression="SET execution_capacity_keys = :keys",
        ExpressionAttributeValues=_serialize({":keys": keys}),
    )
    body = {
        "stop_evidence": {"child_exit_confirmed": False, "workload_terminated": False, "observed_at": "2026-09-24T12:00:00Z"},
        "queue_ack_status": "unknown",
    }
    assert service.settlement(identity, body)["operation_status"] == "unknown"
    assert store._get_authority(keys[0], "ACTIVE")["active_count"] == 1
    body["stop_evidence"]["child_exit_confirmed"] = True
    body["queue_ack_status"] = "confirmed"
    assert service.settlement(identity, body)["operation_status"] == "confirmed"
    assert service.settlement(identity, body)["operation_status"] == "confirmed"
    assert all(store._get_authority(key, "ACTIVE")["active_count"] == 0 for key in keys)
    body["stop_evidence"]["child_exit_confirmed"] = False
    body["queue_ack_status"] = "unknown"
    assert service.settlement(identity, body)["queue_ack_status"] == "confirmed"
    assert service.snapshot(req.task_id)["stop_evidence"]["child_exit_confirmed"] is True
    assert service.snapshot(req.task_id)["state"] == "running"  # a stop receipt is not a successful task


def test_settlement_cannot_release_new_attempt_capacity(store):
    req, service = accepted(store)
    identity = running(store, req)
    old = SimpleNamespace(**(vars(identity) | {"runtime_attempt_id": str(uuid.uuid4())}))
    with pytest.raises(errors.TaskApiError):
        service.settlement(old, {"stop_evidence": {"child_exit_confirmed": True, "workload_terminated": True}, "queue_ack_status": "confirmed"})


def test_input_versus_final_result_version_fence(store, monkeypatch):
    req, service = accepted(store)
    identity = running(store, req)
    model(store, req, turn(store, req, 1))
    original = service._write
    raced = False

    def race(items):
        nonlocal raced
        if not raced:
            raced = True
            other = TaskCommands(store)
            admit(other, req)
        return original(items)

    monkeypatch.setattr(service, "_write", race)
    with pytest.raises(errors.TaskApiError):
        service.finalize(identity, final_body(identity))
    assert service.snapshot(req.task_id)["state"] == "running"
    assert len(service.commands(req.task_id)) == 1
