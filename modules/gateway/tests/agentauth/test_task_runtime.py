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
