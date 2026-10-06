"""An installed worker is not the same thing as a configured binding or a task receipt."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import HTTPException

from src.internal import domain_operation_binding_proof_routes as proof
from src.internal.domain_operation_routes import DomainScope
from src.internal.domain_operation_store import DomainBinding


@pytest.fixture
def installed(tmp_path):
    role = "arn:aws:iam::123456789012:role/paid-worker"
    queue = "https://sqs.us-east-1.amazonaws.com/123456789012/paid-operations"
    binding = DomainBinding(
        domain="superplane",
        org_id="domain-org",
        adp_org_id="tenant",
        producer_registry_id="producer",
        worker_registry_id="worker",
        database_secret_id="domain-db",
        database_schema="superplane",
        queue_url=queue,
        worker_namespace="domain-system",
        worker_service_account="superplane-paid-worker",
        worker_container="paid-worker",
        worker_image_digests=("sha256:" + "a" * 64,),
        repo="example/source",
        observation_url="https://observation.example",
        observation_credential_secret_id="observation-secret",
    )
    job = {
        "metadata": {"name": binding.worker_scaled_job, "namespace": binding.worker_namespace},
        "spec": {
            "maxReplicaCount": 2,
            "triggers": [{"metadata": {"queueURL": queue, "awsRegion": "us-east-1"}}],
            "jobTargetRef": {
                "template": {
                    "spec": {
                        "serviceAccountName": binding.worker_service_account,
                        "containers": [
                            {
                                "name": binding.worker_container,
                                "image": "example/paid-worker@sha256:" + "a" * 64,
                                "env": [
                                    {"name": "ADP_AGENT_AUTHORITY_ENABLED", "value": "true"},
                                    {"name": "SUPERPLANE_PAID_WORKER_MODE", "value": "native-lifecycle"},
                                ],
                                "envFrom": [{"configMapRef": {"name": "superplane-paid-worker-config"}}],
                            }
                        ],
                    }
                }
            },
        },
    }
    account = {
        "metadata": {
            "namespace": binding.worker_namespace,
            "name": binding.worker_service_account,
            "annotations": {"eks.amazonaws.com/role-arn": role},
        }
    }
    config = {
        "metadata": {"name": "superplane-paid-worker-config", "namespace": binding.worker_namespace},
        "data": {"SUPERPLANE_OPERATION_SCHEMA": binding.database_schema},
    }
    responses = {"scaledjobs": job, "serviceaccounts": account, "configmaps": config}
    calls = []

    def transport(request):
        calls.append(request.url.path)
        for kind, response in responses.items():
            if "/" + kind + "/" in request.url.path:
                return httpx.Response(200, json=response)
        return httpx.Response(404)

    token = tmp_path / "gateway-token"
    token.write_text("fictional-token")
    client = httpx.Client(base_url="https://kubernetes.default.svc", transport=httpx.MockTransport(transport))
    runtime = SimpleNamespace(workloads=SimpleNamespace(_gateway_token_path=token, _client=client))
    yield binding, responses, calls, runtime
    client.close()


@pytest.mark.asyncio
async def test_binding_proof_checks_installed_resources_registry_and_queue(installed, monkeypatch):
    binding, responses, calls, runtime = installed
    produced = Mock(return_value=binding)
    monkeypatch.setattr(proof, "producer", produced)

    @asynccontextmanager
    async def connect(_binding):
        async def mapped(_sql, _org):
            return binding.adp_org_id

        yield SimpleNamespace(fetchval=mapped)

    registry = Mock()
    registry.get_current_agent.return_value = {
        "org_id": binding.adp_org_id,
        "scope": "internal",
        "credential_scopes": [proof.EXECUTOR_SCOPE, proof.RECOVERY_SCOPE],
    }
    store = SimpleNamespace(table="authority-table", client=SimpleNamespace(describe_table=lambda **_: {"Table": {"TableStatus": "ACTIVE"}}))
    queue = SimpleNamespace(get_queue_attributes=lambda **_: {"Attributes": {"QueueArn": "arn:aws:sqs:us-east-1:123456789012:paid-operations"}})
    monkeypatch.setattr(proof, "operation_connect", connect)
    monkeypatch.setattr(proof, "bootstrap_store", lambda: store)
    monkeypatch.setattr(proof, "runtime_for", lambda _binding: runtime)
    monkeypatch.setattr(proof, "get_agent_registry_service", lambda: registry)
    monkeypatch.setattr(proof, "aws_client", lambda _service: queue)
    result = await proof.binding_proof(DomainScope(domain="superplane", org_id=binding.org_id), Mock())
    assert result["installed"] is True
    assert result["worker_image_digest"] == binding.worker_image_digests[0]
    assert result["worker_role_arn"] == "arn:aws:iam::123456789012:role/paid-worker"
    assert result["operation_schema"] == binding.database_schema
    assert result["producer_registry_id"] == binding.producer_registry_id
    assert result["worker_registry_id"] == binding.worker_registry_id
    assert len(calls) == 3
    assert all("task" not in path and "pod" not in path for path in calls)
    assert produced.call_count == 2
    registry.get_current_agent.assert_called_once_with(binding.worker_registry_id, result["worker_role_arn"])

    registry.get_current_agent.return_value = {"org_id": binding.adp_org_id, "scope": "internal", "credential_scopes": []}
    with pytest.raises(HTTPException) as revoked:
        await proof.binding_proof(DomainScope(domain="superplane", org_id=binding.org_id), Mock())
    assert revoked.value.status_code == 503

    registry.get_current_agent.return_value = {
        "org_id": binding.adp_org_id,
        "scope": "internal",
        "credential_scopes": [proof.EXECUTOR_SCOPE, proof.RECOVERY_SCOPE],
    }
    monkeypatch.setattr(
        proof,
        "aws_client",
        lambda _service: SimpleNamespace(get_queue_attributes=lambda **_: {"Attributes": {"QueueArn": "arn:aws:sqs:us-east-1:123456789012:other"}}),
    )
    with pytest.raises(HTTPException) as replaced_queue:
        await proof.binding_proof(DomainScope(domain="superplane", org_id=binding.org_id), Mock())
    assert replaced_queue.value.status_code == 503

    monkeypatch.setattr(proof, "aws_client", lambda _service: queue)
    produced.side_effect = [binding, SimpleNamespace(replaced=True)]
    with pytest.raises(HTTPException) as changed_producer:
        await proof.binding_proof(DomainScope(domain="superplane", org_id=binding.org_id), Mock())
    assert changed_producer.value.status_code == 403


@pytest.mark.parametrize(
    "change",
    [
        lambda values: values["scaledjobs"]["metadata"].update({"annotations": {"autoscaling.keda.sh/paused": "true"}}),
        lambda values: values["scaledjobs"]["spec"].update({"maxReplicaCount": 0}),
        lambda values: values["scaledjobs"]["spec"]["jobTargetRef"]["template"]["spec"]["containers"][0]["env"][1].update(
            {"value": "native-controller"}
        ),
        lambda values: values["scaledjobs"]["spec"]["jobTargetRef"]["template"]["spec"]["containers"][0].update(
            {"image": "example/paid-worker@sha256:" + "b" * 64}
        ),
        lambda values: values["scaledjobs"]["spec"].update({"triggers": [{"metadata": {"queueURL": "other", "awsRegion": "us-east-1"}}]}),
        lambda values: values["serviceaccounts"]["metadata"]["annotations"].update(
            {"eks.amazonaws.com/role-arn": "arn:aws:iam::999999999999:role/other"}
        ),
        lambda values: values["configmaps"]["data"].update({"SUPERPLANE_OPERATION_SCHEMA": "other"}),
    ],
)
def test_binding_proof_refuses_mismatched_or_inactive_installation(installed, change):
    binding, responses, calls, runtime = installed
    change(responses)
    with pytest.raises(HTTPException) as refusal:
        proof.installed_worker(binding, runtime)
    assert refusal.value.status_code == 503
    assert all("task" not in path for path in calls)


def test_binding_proof_refuses_missing_gateway_kubernetes_token(installed):
    binding, _, calls, runtime = installed
    runtime.workloads._gateway_token_path.unlink()
    with pytest.raises(HTTPException) as refusal:
        proof.installed_worker(binding, runtime)
    assert refusal.value.status_code == 503
    assert not calls


@pytest.mark.asyncio
async def test_binding_proof_refuses_unregistered_producer_without_installed_reads(installed, monkeypatch):
    binding, _, calls, _ = installed

    def denied(_request, _body):
        raise HTTPException(403, "producer refused")

    monkeypatch.setattr(proof, "producer", denied)
    with pytest.raises(HTTPException) as refusal:
        await proof.binding_proof(DomainScope(domain="superplane", org_id=binding.org_id), Mock())
    assert refusal.value.status_code == 403
    assert not calls


def test_real_producer_check_rejects_wrong_registry_before_worker_reads(installed, monkeypatch):
    from src.internal import domain_operation_routes

    binding, _, calls, _ = installed
    monkeypatch.setattr(domain_operation_routes, "binding_for", lambda _domain, _org: binding)
    monkeypatch.setattr(domain_operation_routes, "current_registry", lambda _request, _scope: "other")
    with pytest.raises(HTTPException) as refusal:
        domain_operation_routes.producer(Mock(), DomainScope(domain="superplane", org_id=binding.org_id))
    assert refusal.value.status_code == 403
    assert not calls


def test_proof_route_requires_internal_iam_authentication():
    from src.internal.auth_deps import verify_internal_or_irsa

    assert any(dependency.call is verify_internal_or_irsa for dependency in proof.router.routes[0].dependant.dependencies)
