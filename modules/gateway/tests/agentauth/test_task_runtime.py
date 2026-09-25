"""Task acceptance, workload ownership and current-attempt authorization."""
# ruff: noqa: F811
import json
import uuid
from types import SimpleNamespace

import pytest

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.task_runtime import TaskRuntime
from tests.tasks.test_store import NOW, _request, client, store  # noqa: F401


@pytest.fixture
def runtime(store):
    request = _request()
    store.accept(request)
    runtime = TaskRuntime(store, env={"AGENT_RUN_CREDENTIAL_KEY": "task-runtime-test-key-01234567890123456789"}, clock=lambda: NOW)
    pod = SimpleNamespace(uid=str(uuid.uuid4()), namespace="adp-agents")
    body = {"task_id": request.task_id, "invocation_id": request.invocation_id,
            "envelope_digest": envelope_digest(request.envelope), "workload": {"pod_uid": pod.uid, "namespace": pod.namespace}}

    class Delivery:
        def require_assignment(self, pod_uid, invocation_id, digest):
            assert invocation_id == request.invocation_id
            assert digest == body["envelope_digest"]

        def read(self, pod_uid):
            return {"body": json.dumps(request.envelope)}

    return runtime, pod, body, Delivery()


def test_bootstrap_from_accepted_task_needs_no_github(runtime):
    service, pod, body, delivery = runtime
    result = service.bootstrap(body=body, pod=pod, delivery=delivery)
    assert result["task_id"] == body["task_id"]
    identity = service.authenticate(credential=result["run_credential"], pod=pod, require_attempt=False)
    assert identity.task_id == body["task_id"]
    assert identity.runtime_attempt_id is None
    assert result["input"]["instructions"] == "investigate"


def test_second_pod_cannot_claim_same_generation(runtime):
    service, pod, body, delivery = runtime
    service.bootstrap(body=body, pod=pod, delivery=delivery)
    other = SimpleNamespace(uid=str(uuid.uuid4()), namespace=pod.namespace)
    with pytest.raises(BootstrapRefusedError, match="already owned"):
        service.bootstrap(body={**body, "workload": {"pod_uid": other.uid, "namespace": other.namespace}}, pod=other, delivery=delivery)


def test_stolen_credential_fails_pod_binding(runtime):
    service, pod, body, delivery = runtime
    result = service.bootstrap(body=body, pod=pod, delivery=delivery)
    other = SimpleNamespace(uid=str(uuid.uuid4()), namespace=pod.namespace)
    with pytest.raises(BootstrapRefusedError):
        service.authenticate(credential=result["run_credential"], pod=other, require_attempt=False)


def test_attempt_is_atomically_bound_and_replaced(runtime):
    service, pod, body, delivery = runtime
    result = service.bootstrap(body=body, pod=pod, delivery=delivery)
    identity = service.authenticate(credential=result["run_credential"], pod=pod, require_attempt=False)
    first = str(uuid.uuid4())
    attempt = {"task_id": identity.task_id, "invocation_id": identity.invocation_id,
               "generation": identity.generation, "runtime_attempt_id": first}
    service.register_attempt(identity=identity, body=attempt)
    current = service.authenticate(credential=result["run_credential"], pod=pod)
    assert current.runtime_attempt_id == first
    service.register_attempt(identity=current, body={**attempt, "runtime_attempt_id": str(uuid.uuid4())})
    assert service.authenticate(credential=result["run_credential"], pod=pod).runtime_attempt_id != first


def test_bootstrap_body_cannot_name_another_task(runtime):
    service, pod, body, delivery = runtime
    with pytest.raises(BootstrapRefusedError):
        service.bootstrap(body={**body, "task_id": "tsk_" + str(uuid.uuid4())}, pod=pod, delivery=delivery)


def test_admin_policy_is_consumed_by_actual_acceptance(client, store):
    from src.agentauth.task_service_policy import TaskServicePolicyStore
    from tests.tasks.test_store import AUTHORITY_TABLE
    policy_store = TaskServicePolicyStore(table_name=AUTHORITY_TABLE, client=client, clock=lambda: NOW)
    client.delete_item(TableName=AUTHORITY_TABLE, Key={"pk": {"S": "TENANT#tenant-a"}, "sk": {"S": "TASK_POLICY#svc-principal-1"}})
    policy_store.put(tenant_id="tenant-a", canonical_principal_id="svc-principal-1", expected_version=0,
        updated_by="operator", policy={"status": "active", "allowed_personas": ["agent-task-investigator"],
            "task_scopes": ["submit", "read"], "model_policy_version": "model-v1",
            "limits": {"max_duration_minutes": 30, "max_turns": 8, "max_output_tokens_per_turn": 4096, "max_usd_per_task": 1}})
    request = _request()
    assert store.accept(request).task_id == request.task_id
    assert store.resolve_work(request.dispatch_id)["envelope"] == request.envelope


def test_bootstrap_replay_keeps_execution_reservations_once(runtime):
    service, pod, body, delivery = runtime
    service.bootstrap(body=body, pod=pod, delivery=delivery)
    service.bootstrap(body=body, pod=pod, delivery=delivery)
    task = service.repository.read_task(body["task_id"])
    grant = service._grant(task["scope"]["tenant"], task["invocation_id"], task["generation"])
    assert len(grant["execution_capacity_keys"]) == 3
    for key in grant["execution_capacity_keys"]:
        assert service.repository._get_authority(key, "ACTIVE")["active_count"] == 1


def test_expired_credential_only_allows_bound_stop_evidence(runtime):
    from datetime import timedelta

    from src.agentauth.run_credential import CredentialError
    service, pod, body, delivery = runtime
    result = service.bootstrap(body=body, pod=pod, delivery=delivery)
    identity = service.authenticate(credential=result["run_credential"], pod=pod, require_attempt=False)
    service.register_attempt(identity=identity, body={"task_id": identity.task_id,
        "invocation_id": identity.invocation_id, "generation": identity.generation, "runtime_attempt_id": str(uuid.uuid4())})
    service.clock = lambda: NOW + timedelta(minutes=31)
    with pytest.raises(CredentialError):
        service.authenticate(credential=result["run_credential"], pod=pod)
    assert service.authenticate(credential=result["run_credential"], pod=pod, stop_only=True).task_id == identity.task_id
    with pytest.raises(BootstrapRefusedError):
        service.authenticate(credential=result["run_credential"],
            pod=SimpleNamespace(uid=str(uuid.uuid4()), namespace=pod.namespace), stop_only=True)


def test_principal_execution_limit_is_atomic(runtime):
    service, pod, body, delivery = runtime
    service.bootstrap(body=body, pod=pod, delivery=delivery)
    for number in (2, 3):
        request = _request(idempotency_key=f"task-{number}")
        service.repository.accept(request)
        next_pod = SimpleNamespace(uid=str(uuid.uuid4()), namespace=pod.namespace)
        next_body = {"task_id": request.task_id, "invocation_id": request.invocation_id,
            "envelope_digest": envelope_digest(request.envelope), "workload": {"pod_uid": next_pod.uid, "namespace": next_pod.namespace}}
        next_delivery = SimpleNamespace(require_assignment=lambda *args: None, read=lambda uid: {"body": json.dumps(request.envelope)})
        if number == 2:
            service.bootstrap(body=next_body, pod=next_pod, delivery=next_delivery)
        else:
            with pytest.raises(BootstrapRefusedError, match="capacity"):
                service.bootstrap(body=next_body, pod=next_pod, delivery=next_delivery)
            assert "workload_uid" not in service._grant(request.tenant, request.invocation_id, 1)
    task = service.repository.read_task(body["task_id"])
    for key in service._grant(task["scope"]["tenant"], task["invocation_id"], 1)["execution_capacity_keys"]:
        assert service.repository._get_authority(key, "ACTIVE")["active_count"] == 2


def _attempt_identity(runtime):
    service, pod, body, delivery = runtime
    result = service.bootstrap(body=body, pod=pod, delivery=delivery)
    identity = service.authenticate(credential=result["run_credential"], pod=pod, require_attempt=False)
    service.register_attempt(identity=identity, body={"task_id": identity.task_id,
        "invocation_id": identity.invocation_id, "generation": identity.generation, "runtime_attempt_id": str(uuid.uuid4())})
    return service.authenticate(credential=result["run_credential"], pod=pod)


def test_initial_turn_is_stable_empty_and_counts_once(runtime):
    from src.agentauth.task_turns import TaskTurnStore
    identity = _attempt_identity(runtime)
    turns = TaskTurnStore(runtime[0].repository, clock=lambda: NOW)
    turn_id = str(uuid.uuid4())
    first = turns.commit(identity=identity, request_id=turn_id, expected_transcript_version=1)
    replay = turns.commit(identity=identity, request_id=turn_id, expected_transcript_version=1)
    assert first["turn"] == replay["turn"]
    assert first["turn"]["command_ids"] == []
    assert first["turn"]["transcript_version"] == 2
    assert len(turns.list_turns(identity.task_id)) == 1
    waiting = turns.commit(identity=identity, request_id=str(uuid.uuid4()), expected_transcript_version=2)
    assert waiting["operation_status"] == "waiting"


def test_turn_consumes_pending_input_with_event_atomically(runtime):
    from src.agentauth.task_turns import TaskTurnStore
    from src.tasks.records import command_sort_key, task_commands_partition
    from src.tasks.store import _serialize
    identity = _attempt_identity(runtime)
    repository = runtime[0].repository
    turns = TaskTurnStore(repository, clock=lambda: NOW)
    turns.commit(identity=identity, request_id=str(uuid.uuid4()), expected_transcript_version=1)
    command_id = str(uuid.uuid4())
    repository._client.put_item(TableName=repository.table_name, Item=_serialize({
        "event_id": task_commands_partition(identity.task_id), "arrived_at": command_sort_key(command_id),
        "task_id": identity.task_id, "command_id": command_id, "kind": "input", "payload": {"text": "next question"},
        "command_sequence": 1, "status": "accepted", "authority_expires_at": "2026-09-24T12:30:00Z"}))
    turn_id = str(uuid.uuid4())
    result = turns.commit(identity=identity, request_id=turn_id, expected_transcript_version=2)
    assert result["messages"] == [{"command_id": command_id, "text": "next question"}]
    stored = repository.read_commands(task_id=identity.task_id)[0]
    assert stored["status"] == "consumed"
    assert stored["turn_id"] == turn_id
    events = repository.read_events(task_id=identity.task_id)
    assert events[-1]["type"] == "input.consumed"
    assert events[-1]["data"]["turn_id"] == turn_id
    assert turns.commit(identity=identity, request_id=turn_id, expected_transcript_version=2)["turn"] == result["turn"]


def test_live_turn_route_cannot_skip_or_exceed_eight_turns(runtime):
    from src.agentauth.task_turns import TaskTurnStore
    from src.tasks.records import command_sort_key, task_commands_partition
    from src.tasks.store import TaskStoreError, _serialize
    identity = _attempt_identity(runtime)
    repository = runtime[0].repository
    turns = TaskTurnStore(repository, clock=lambda: NOW)
    with pytest.raises(TaskStoreError, match="transcript"):
        turns.commit(identity=identity, request_id=str(uuid.uuid4()), expected_transcript_version=99)
    for number in range(1, 9):
        if number > 1:
            command_id = str(uuid.uuid4())
            repository._client.put_item(TableName=repository.table_name, Item=_serialize({
                "event_id": task_commands_partition(identity.task_id), "arrived_at": command_sort_key(command_id),
                "task_id": identity.task_id, "command_id": command_id, "kind": "input", "payload": {"text": "next"},
                "command_sequence": number, "status": "accepted", "authority_expires_at": "2026-09-24T12:30:00Z"}))
        committed = turns.commit(identity=identity, request_id=str(uuid.uuid4()), expected_transcript_version=number)
        assert committed["turn"]["turn_number"] == number
    with pytest.raises(TaskStoreError, match="budget"):
        turns.commit(identity=identity, request_id=str(uuid.uuid4()), expected_transcript_version=9)
    assert len(turns.list_turns(identity.task_id)) == 8
