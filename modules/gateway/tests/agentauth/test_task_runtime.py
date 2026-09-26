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
def runtime(store, request):
    request = _request(persona=getattr(request, "param", "agent-task-investigator"))
    if request.persona != "agent-task-investigator":
        from src.tasks.records import task_authority_partition, task_policy_sort_key
        from src.tasks.store import _serialize

        store._client.update_item(
            TableName=store.authority_table_name,
            Key=_serialize({"pk": task_authority_partition(request.tenant), "sk": task_policy_sort_key(request.canonical_principal)}),
            UpdateExpression="SET personas = :personas",
            ExpressionAttributeValues={":personas": {"SS": ["agent-task-investigator", request.persona]}},
        )
    store.accept(request)
    runtime = TaskRuntime(store, env={"AGENT_RUN_CREDENTIAL_KEY": "task-runtime-test-key-01234567890123456789"}, clock=lambda: NOW)
    pod = SimpleNamespace(uid=str(uuid.uuid4()), namespace="adp-agents")
    body = {
        "task_id": request.task_id,
        "invocation_id": request.invocation_id,
        "envelope_digest": envelope_digest(request.envelope),
        "workload": {"pod_uid": pod.uid, "namespace": pod.namespace},
    }

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
    attempt = {"task_id": identity.task_id, "invocation_id": identity.invocation_id, "generation": identity.generation, "runtime_attempt_id": first}
    service.register_attempt(identity=identity, body=attempt)
    from src.tasks.records import run_sort_key, task_run_partition

    history_key = run_sort_key(invocation_id=identity.invocation_id, generation=identity.generation) + "#ATTEMPT#" + first
    history = service.repository._get(task_run_partition(identity.task_id), history_key)
    version = service.repository.read_task(identity.task_id)["version"]
    service.register_attempt(identity=identity, body=attempt)  # Lost response retry.
    assert service.repository._get(task_run_partition(identity.task_id), history_key) == history
    assert service.repository.read_task(identity.task_id)["version"] == version
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
    policy_store.put(
        tenant_id="tenant-a",
        canonical_principal_id="svc-principal-1",
        expected_version=0,
        updated_by="operator",
        policy={
            "status": "active",
            "allowed_personas": ["agent-task-investigator"],
            "task_scopes": ["submit", "read"],
            "model_policy_version": "model-v1",
            "limits": {"max_duration_minutes": 30, "max_turns": 8, "max_output_tokens_per_turn": 4096, "max_usd_per_task": 1},
        },
    )
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
    service.register_attempt(
        identity=identity,
        body={
            "task_id": identity.task_id,
            "invocation_id": identity.invocation_id,
            "generation": identity.generation,
            "runtime_attempt_id": str(uuid.uuid4()),
        },
    )
    service.clock = lambda: NOW + timedelta(minutes=31)
    with pytest.raises(CredentialError):
        service.authenticate(credential=result["run_credential"], pod=pod)
    assert service.authenticate(credential=result["run_credential"], pod=pod, stop_only=True).task_id == identity.task_id
    with pytest.raises(BootstrapRefusedError):
        service.authenticate(credential=result["run_credential"], pod=SimpleNamespace(uid=str(uuid.uuid4()), namespace=pod.namespace), stop_only=True)


def test_principal_execution_limit_is_atomic(runtime):
    service, pod, body, delivery = runtime
    service.bootstrap(body=body, pod=pod, delivery=delivery)
    for number in (2, 3):
        request = _request(idempotency_key=f"task-{number}")
        service.repository.accept(request)
        next_pod = SimpleNamespace(uid=str(uuid.uuid4()), namespace=pod.namespace)
        next_body = {
            "task_id": request.task_id,
            "invocation_id": request.invocation_id,
            "envelope_digest": envelope_digest(request.envelope),
            "workload": {"pod_uid": next_pod.uid, "namespace": next_pod.namespace},
        }
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
    service.register_attempt(
        identity=identity,
        body={
            "task_id": identity.task_id,
            "invocation_id": identity.invocation_id,
            "generation": identity.generation,
            "runtime_attempt_id": str(uuid.uuid4()),
        },
    )
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


@pytest.mark.parametrize("runtime,autonomous", [("agent-task-investigator", False), ("agent-task-cyber", True)], indirect=["runtime"])
def test_turn_consumes_pending_input_with_event_atomically(runtime, autonomous):
    from src.agentauth.task_turns import TaskTurnStore
    from src.tasks.records import command_sort_key, task_commands_partition
    from src.tasks.store import _serialize

    identity = _attempt_identity(runtime)
    repository = runtime[0].repository
    turns = TaskTurnStore(repository, clock=lambda: NOW)
    turns.commit(identity=identity, request_id=str(uuid.uuid4()), expected_transcript_version=1)
    command_id = str(uuid.uuid4())
    repository._client.put_item(
        TableName=repository.table_name,
        Item=_serialize(
            {
                "event_id": task_commands_partition(identity.task_id),
                "arrived_at": command_sort_key(command_id),
                "task_id": identity.task_id,
                "command_id": command_id,
                "kind": "input",
                "payload": {"text": "next question"},
                "command_sequence": 1,
                "status": "accepted",
                "authority_expires_at": "2026-09-24T12:30:00Z",
            }
        ),
    )
    turn_id = str(uuid.uuid4())
    result = turns.commit(identity=identity, request_id=turn_id, expected_transcript_version=2, allow_autonomous=autonomous)
    if autonomous:
        assert result["messages"] == []
        assert result["pending_input_count"] == 1
        assert repository.read_commands(task_id=identity.task_id)[0]["status"] == "accepted"
        turn_id = str(uuid.uuid4())
        result = turns.commit(identity=identity, request_id=turn_id, expected_transcript_version=3)
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
            repository._client.put_item(
                TableName=repository.table_name,
                Item=_serialize(
                    {
                        "event_id": task_commands_partition(identity.task_id),
                        "arrived_at": command_sort_key(command_id),
                        "task_id": identity.task_id,
                        "command_id": command_id,
                        "kind": "input",
                        "payload": {"text": "next"},
                        "command_sequence": number,
                        "status": "accepted",
                        "authority_expires_at": "2026-09-24T12:30:00Z",
                    }
                ),
            )
        committed = turns.commit(identity=identity, request_id=str(uuid.uuid4()), expected_transcript_version=number)
        assert committed["turn"]["turn_number"] == number
    with pytest.raises(TaskStoreError, match="budget"):
        turns.commit(identity=identity, request_id=str(uuid.uuid4()), expected_transcript_version=9)
    assert len(turns.list_turns(identity.task_id)) == 8


def _settlement_fixture(runtime):
    from src.tasks.store import _serialize

    service, pod, body, delivery = runtime
    result = service.bootstrap(body=body, pod=pod, delivery=delivery)
    identity = service.authenticate(credential=result["run_credential"], pod=pod, require_attempt=False)
    service.register_attempt(
        identity=identity,
        body={
            "task_id": identity.task_id,
            "invocation_id": identity.invocation_id,
            "generation": identity.generation,
            "runtime_attempt_id": str(uuid.uuid4()),
        },
    )
    service.repository._client.put_item(
        TableName=service.repository.authority_table_name, Item=_serialize({"pk": f"PODTASK#{pod.uid}", "sk": "DELIVERY", **delivery.read(pod.uid)})
    )
    return service, pod, result


def test_credentialless_settlement_only_resolves_retained_assignment(runtime):
    from datetime import timedelta

    from src.agentauth.run_credential import CredentialError

    service, pod, _ = _settlement_fixture(runtime)
    service.clock = lambda: NOW + timedelta(minutes=31)
    identity = service.authenticate_settlement(pod=pod)
    assert identity.runtime_attempt_id is not None
    with pytest.raises(CredentialError):
        service.authenticate(credential="", pod=pod)
    other = SimpleNamespace(uid=str(uuid.uuid4()), namespace=pod.namespace)
    with pytest.raises(BootstrapRefusedError):
        service.authenticate_settlement(pod=other)
    with pytest.raises(BootstrapRefusedError):
        service.authenticate_settlement(pod=SimpleNamespace(uid=pod.uid, namespace="other"))


def test_settlement_rejects_mismatched_current_attempt(runtime):
    service, pod, _ = _settlement_fixture(runtime)
    identity = service.authenticate_settlement(pod=pod)
    from src.tasks.records import task_authority_partition, task_run_grant_sort_key
    from src.tasks.store import _serialize

    service.repository._client.update_item(
        TableName=service.repository.authority_table_name,
        Key=_serialize(
            {
                "pk": task_authority_partition(identity.tenant),
                "sk": task_run_grant_sort_key(invocation_id=identity.invocation_id, generation=identity.generation),
            }
        ),
        UpdateExpression="SET runtime_attempt_id = :other",
        ExpressionAttributeValues=_serialize({":other": str(uuid.uuid4())}),
    )
    with pytest.raises(BootstrapRefusedError):
        service.authenticate_settlement(pod=pod)


def test_reported_clarification_is_public_replyable_and_consumed_once(runtime):
    from datetime import timedelta

    from src.agentauth.task_turns import TaskTurnStore
    from src.tasks.dynamo_read_store import DynamoTaskReadStore
    from src.tasks.errors import TaskApiError
    from src.tasks.task_commands import TaskCommands

    identity = _attempt_identity(runtime)
    repository = runtime[0].repository
    turns = TaskTurnStore(repository, clock=lambda: NOW)
    turns.commit(identity=identity, request_id=str(uuid.uuid4()), expected_transcript_version=1)
    request_id = str(uuid.uuid4())
    report = dict(
        task_id=identity.task_id,
        invocation_id=identity.invocation_id,
        generation=identity.generation,
        runtime_attempt_id=identity.runtime_attempt_id,
        report_id=str(uuid.uuid4()),
        kind="input.required",
        data={"input_request_id": request_id, "prompt": "Which environment?"},
    )
    first = repository.append_report(**report)
    snapshot = repository.read_task(identity.task_id)
    assert snapshot["state"] == "waiting_for_input"
    adapter = DynamoTaskReadStore(repository, s3_client=None, artifact_bucket="unused")
    assert adapter.load_task(task_id=identity.task_id).input_request == {
        "input_request_id": request_id,
        "prompt": "Which environment?",
        "requested_at": "2026-09-24T12:00:00Z",
    }
    assert repository.append_report(**report) == first
    assert repository.read_task(identity.task_id)["version"] == snapshot["version"]
    command_id = str(uuid.uuid4())
    commands = TaskCommands(repository)
    command = dict(
        task_id=identity.task_id,
        command_id=command_id,
        kind="input",
        payload={"text": "staging", "reply_to": request_id},
        principal=snapshot["scope"]["canonical_principal"],
        tenant=identity.tenant,
        expires_at=NOW + timedelta(minutes=5),
    )
    commands.admit(**command)
    assert repository.read_task(identity.task_id)["state"] == "waiting_for_input"
    turn_id = str(uuid.uuid4())
    result = turns.commit(identity=identity, request_id=turn_id, expected_transcript_version=2)
    assert result["messages"] == [{"command_id": command_id, "text": "staging", "reply_to": request_id}]
    assert repository.read_task(identity.task_id)["state"] == "running"
    assert repository.read_task(identity.task_id).get("input_request") is None
    assert turns.commit(identity=identity, request_id=turn_id, expected_transcript_version=2)["turn"] == result["turn"]
    commands.admit(**command)
    assert sum(row["type"] == "input.consumed" for row in repository.read_events(task_id=identity.task_id)) == 1
    with pytest.raises(TaskApiError, match="no longer current"):
        commands.admit(**(command | {"command_id": str(uuid.uuid4())}))


def test_six_hour_task_renews_short_credentials_without_extending_deadline(store):
    from datetime import timedelta

    from src.agentauth.run_credential import CredentialError

    request = _request(deadline_at=NOW + timedelta(hours=6))
    store.accept(request)
    current = [NOW]
    service = TaskRuntime(store, env={"AGENT_RUN_CREDENTIAL_KEY": "task-runtime-test-key-01234567890123456789"}, clock=lambda: current[0])
    pod = SimpleNamespace(uid=str(uuid.uuid4()), namespace="adp-agents")
    body = {
        "task_id": request.task_id,
        "invocation_id": request.invocation_id,
        "envelope_digest": envelope_digest(request.envelope),
        "workload": {"pod_uid": pod.uid, "namespace": pod.namespace},
    }
    delivery = SimpleNamespace(require_assignment=lambda *args: None, read=lambda uid: {"body": json.dumps(request.envelope)})
    first = service.bootstrap(body=body, pod=pod, delivery=delivery)
    initial_identity = service.authenticate(credential=first["run_credential"], pod=pod, require_attempt=False)
    attempt_id = str(uuid.uuid4())
    service.register_attempt(
        identity=initial_identity,
        body={"task_id": request.task_id, "invocation_id": request.invocation_id, "generation": 1, "runtime_attempt_id": attempt_id},
    )
    assert first["run_credential_expires_at"] == (NOW + timedelta(seconds=900)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for elapsed in (timedelta(minutes=16), timedelta(hours=5, minutes=59)):
        current[0] = NOW + elapsed
        with pytest.raises(CredentialError):
            service.authenticate(credential=first["run_credential"], pod=pod)
        renewed = service.bootstrap(body=body, pod=pod, delivery=delivery)
        expected_expiry = min(current[0] + timedelta(seconds=900), request.deadline_at)
        assert renewed["run_credential_expires_at"] == expected_expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert service.authenticate(credential=renewed["run_credential"], pod=pod).runtime_attempt_id == attempt_id
        assert renewed["deadline_at"] == request.deadline_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    current[0] = request.deadline_at + timedelta(seconds=1)
    with pytest.raises(BootstrapRefusedError, match="expired"):
        service.bootstrap(body=body, pod=pod, delivery=delivery)
    with pytest.raises(CredentialError):
        service.authenticate(credential=renewed["run_credential"], pod=pod)


@pytest.mark.parametrize("runtime", ["agent-task-cyber", "agent-task-claude-developer", "agent-task-codex-developer"], indirect=True)
def test_sdk_autonomous_turns_are_explicit_bounded_and_replayed(runtime):
    from src.agentauth.task_turns import TaskTurnStore
    from src.tasks.store import TaskStoreError

    identity = _attempt_identity(runtime)
    turns = TaskTurnStore(runtime[0].repository, clock=lambda: NOW)
    for number in range(1, 9):
        turn_id = str(uuid.uuid4())
        result = turns.commit(identity=identity, request_id=turn_id, expected_transcript_version=number, allow_autonomous=True)
        assert result["operation_status"] == "committed"
        assert result["turn"]["turn_number"] == number
        assert result["messages"] == []
        replay = turns.commit(identity=identity, request_id=turn_id, expected_transcript_version=number, allow_autonomous=True)
        assert replay["turn"] == result["turn"]
        if number == 1:
            assert turns.commit(identity=identity, request_id=str(uuid.uuid4()), expected_transcript_version=2)["operation_status"] == "waiting"
    with pytest.raises(TaskStoreError, match="budget"):
        turns.commit(identity=identity, request_id=str(uuid.uuid4()), expected_transcript_version=9, allow_autonomous=True)
    assert len(turns.list_turns(identity.task_id)) == 8


def test_investigator_cannot_request_autonomous_turn(runtime):
    from src.agentauth.task_turns import TaskTurnStore
    from src.tasks.store import TaskStoreError

    identity = _attempt_identity(runtime)
    turns = TaskTurnStore(runtime[0].repository, clock=lambda: NOW)
    with pytest.raises(TaskStoreError, match="SDK Task persona"):
        turns.commit(identity=identity, request_id=str(uuid.uuid4()), expected_transcript_version=1, allow_autonomous=True)
    assert turns.list_turns(identity.task_id) == []


@pytest.mark.parametrize("runtime", ["agent-task-cyber", "agent-task-claude-developer", "agent-task-codex-developer"], indirect=True)
def test_autonomous_turn_cannot_bypass_deadline(runtime):
    from datetime import timedelta

    from src.agentauth.task_turns import TaskTurnStore
    from src.tasks.store import TaskStoreError

    identity = _attempt_identity(runtime)
    turns = TaskTurnStore(runtime[0].repository, clock=lambda: NOW + timedelta(hours=2))
    with pytest.raises(TaskStoreError, match="deadline"):
        turns.commit(identity=identity, request_id=str(uuid.uuid4()), expected_transcript_version=1, allow_autonomous=True)
    assert turns.list_turns(identity.task_id) == []


@pytest.mark.parametrize("value", ["true", 1, None])
def test_autonomous_turn_flag_requires_a_boolean(value):
    from pydantic import ValidationError

    from src.agentauth.task_runtime_routes import TurnBody

    payload = {
        "schema_version": "1.0",
        "attempt": {
            "run": {"task_id": "tsk_" + str(uuid.uuid4()), "invocation_id": str(uuid.uuid4()), "generation": 1},
            "runtime_attempt_id": str(uuid.uuid4()),
        },
        "request_id": str(uuid.uuid4()),
        "expected_transcript_version": 1,
    }
    assert TurnBody.model_validate(payload).allow_autonomous is False
    with pytest.raises(ValidationError):
        TurnBody.model_validate({**payload, "allow_autonomous": value})


@pytest.mark.parametrize("runtime", ["agent-task-cyber", "agent-task-claude-developer", "agent-task-codex-developer"], indirect=True)
def test_autonomous_turn_does_not_resume_waiting_for_input(runtime):
    from src.agentauth.task_turns import TaskTurnStore
    from src.tasks.records import task_partition
    from src.tasks.store import _serialize

    identity = _attempt_identity(runtime)
    repository = runtime[0].repository
    turns = TaskTurnStore(repository, clock=lambda: NOW)
    turns.commit(identity=identity, request_id=str(uuid.uuid4()), expected_transcript_version=1, allow_autonomous=True)
    repository._client.update_item(
        TableName=repository.table_name,
        Key=_serialize({"event_id": task_partition(identity.task_id), "arrived_at": "META"}),
        UpdateExpression="SET #state = :waiting",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues=_serialize({":waiting": "waiting_for_input"}),
    )
    result = turns.commit(identity=identity, request_id=str(uuid.uuid4()), expected_transcript_version=2, allow_autonomous=True)
    assert result["operation_status"] == "waiting"
    assert len(turns.list_turns(identity.task_id)) == 1
