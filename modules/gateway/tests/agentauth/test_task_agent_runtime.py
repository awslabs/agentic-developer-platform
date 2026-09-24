"""Real Task runtime factory selects independent workload proof and rollout gate."""
# ruff: noqa: F811
import pytest
from fastapi import HTTPException

from src.agentauth import task_agent_runtime as factory
from src.agentauth import workload
from src.agentauth.routes import get_agent_runtime
from src.agentauth.task_runtime_routes import task_runtime
from src.agentauth.workload import WorkloadRefusedError
from tests.agentauth.test_bootstrap_routes import DIGEST, kubernetes, store  # noqa: F401


@pytest.fixture
def configured(monkeypatch, store, kubernetes):
    verifier, state, token_path, seen = kubernetes
    factory.get_task_agent_runtime.cache_clear()
    get_agent_runtime.cache_clear()
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    monkeypatch.setenv("ADP_TASK_API_WORKER_ENABLED", "true")
    monkeypatch.setenv("AGENT_AUTHORITY_TABLE", store.table)
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "tasks")
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", "task-test-key")
    monkeypatch.setenv("ADP_TASK_WORKER_IMAGE_DIGESTS", DIGEST)
    monkeypatch.setenv("AGENT_WORKER_IMAGE_DIGESTS", "sha256:" + "b" * 64)
    monkeypatch.setenv("AGENT_WORKER_SERVICE_ACCOUNT", "generic-must-not-be-used")
    monkeypatch.setattr(factory.boto3, "client", lambda *args, **kwargs: store.client)
    monkeypatch.setattr(workload.ssl, "create_default_context", lambda **kwargs: object())
    monkeypatch.setattr(workload.httpx, "Client", lambda **kwargs: verifier._client)
    state["spec"] = {"containers": [{"name": "agent-worker", "env": [
        {"name": "ADP_TASK_API_WORKER_ENABLED", "value": "true"}, {"name": "ADP_AGENT_AUTHORITY_ENABLED", "value": "false"}]}]}
    runtime = factory.get_task_agent_runtime()
    runtime.workloads._gateway_token_path = token_path
    yield runtime, state
    factory.get_task_agent_runtime.cache_clear()
    get_agent_runtime.cache_clear()


def test_task_factory_works_while_generic_authority_stays_disabled(configured):
    runtime, state = configured
    assert runtime.workloads.verify("pod-token").service_account == "agent-scaledjob-sa"
    with pytest.raises(HTTPException) as refused:
        get_agent_runtime()
    assert refused.value.status_code == 503
    state["spec"]["containers"][0]["env"] = [{"name": "ADP_AGENT_AUTHORITY_ENABLED", "value": "true"}]
    with pytest.raises(WorkloadRefusedError):
        runtime.workloads.verify("pod-token")


def test_task_verifier_rejects_generic_or_unapproved_image(configured):
    runtime, state = configured
    state["digest"] = "sha256:" + "b" * 64
    with pytest.raises(WorkloadRefusedError):
        runtime.workloads.verify("pod-token")


def test_factory_after_task_flag_disable_supports_only_stop_route(configured, monkeypatch):
    runtime, _ = configured
    monkeypatch.setenv("ADP_TASK_API_WORKER_ENABLED", "false")
    assert factory.get_task_agent_runtime() is runtime
    assert runtime.workloads.verify("pod-token").uid == "pod-a"
    with pytest.raises(HTTPException):
        task_runtime(runtime)
    assert task_runtime(runtime, stop_only=True).repository


def test_task_image_allowlist_does_not_fallback_to_generic(configured, monkeypatch):
    monkeypatch.delenv("ADP_TASK_WORKER_IMAGE_DIGESTS")
    factory.get_task_agent_runtime.cache_clear()
    with pytest.raises(HTTPException) as refused:
        factory.get_task_agent_runtime()
    assert refused.value.status_code == 503
