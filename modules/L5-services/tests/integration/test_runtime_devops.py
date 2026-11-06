"""Installed loader and scoped authority checks against the published shared helper."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import test_devops_investigation_lifecycle as lifecycle_fixtures
from botocore.credentials import Credentials

import l5_services
from l5_services import runtime_support
from l5_services.runtime_devops import DevopsOperations
from l5_services.workers.execution import AuthorityRevoked, ExecutionService, load_service


lifecycle = lifecycle_fixtures.lifecycle


@pytest.fixture
def installed_runtime(tmp_path, monkeypatch):
    if os.environ.get("L5_EXPECTED_PACKAGE_ROOT"):
        assert l5_services.__file__.startswith(os.environ["L5_EXPECTED_PACKAGE_ROOT"] + "/")
    support = runtime_support
    values = {"L5_WORKER_FACTORY": "l5_services.runtime_devops:factory", "L5_SERVICE_ID": "devops",
              "AWS_REGION": "us-east-1", "AWS_LAMBDA_FUNCTION_NAME": "example-devops-worker",
              "L5_GATEWAY_URL": "https://example.execute-api.us-east-1.amazonaws.com/dev",
              "L5_RDS_HOST": "database.example.org", "L5_RDS_DATABASE": "example", "L5_RDS_USER": "example",
              "L5_RDS_PORT": "5432", "L5_ARCHIVE_BUCKET": "example-archive",
              "L5_WORK_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/example-devops"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    infrastructure = {name: Mock() for name in ("s3", "sqs", "rds")}
    monkeypatch.setattr(support.boto3, "client", lambda name, **kwargs: infrastructure[name])
    requests = []
    source = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "source-one",
              "record_type": "repository_commit", "record_id": "repository-one", "record_revision": "a" * 40}
    snapshot = {"source_ref": source, "snapshot_id": "snapshot-one"}
    scope = {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one"}
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/example",
                "region": "us-east-1"}
    connection = {**source, "record_type": "connection", "record_id": "connection-one"}
    claim = SimpleNamespace(work_id="work-one", owner="lease-owner", fence=2)
    admission = {"scope": scope, "connection_ref": connection, "provider_identity": identity,
                 "delegation_id": "delegation-one", "delegation_version": 2, "source_snapshots": [snapshot]}
    run = SimpleNamespace(**scope, service_id="devops", run_id="run-one", admission=admission,
                          input={"capability": "release_readiness", "sources": [source]})
    state = {"denied": False}

    def gateway(request):
        requests.append(request)
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        body = json.loads(request.content)
        assert body["delegation_id"] == admission["delegation_id"]
        assert body["operation_id"] == run.run_id and body["attempt_id"] == claim.work_id
        assert body["lease_owner"] == claim.owner and body["lease_fence"] == claim.fence
        if state["denied"]:
            return httpx.Response(403, json={"code": "delegation_denied"})
        if request.url.path.endswith("/credential-session"):
            return httpx.Response(200, json={"operation_id": run.run_id, "attempt_id": claim.work_id,
                "delegation_revision": 2, "provider_identity": identity,
                "access_key_id": "scoped-access", "secret_access_key": "scoped-secret", "session_token": "scoped-token",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat()})
        assert request.url.path.endswith("/source-snapshots/snapshot-one/read")
        return httpx.Response(200, json=snapshot)

    client_class = httpx.AsyncClient
    transport = Mock(side_effect=lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(gateway)))
    monkeypatch.setattr(support.httpx, "AsyncClient", transport)
    native = Mock(get_agent_space=Mock(return_value={"agentSpace": {"agentSpaceId": "space-one"}}))
    provider = Mock(client=Mock(return_value=native), get_credentials=Mock(return_value=Credentials("transport-key", "transport-secret")))
    provider_factory = Mock(return_value=provider)
    monkeypatch.setattr(support.boto3, "Session", provider_factory)
    service = load_service()
    return SimpleNamespace(service=service, support=support, run=run, claim=claim, native=native,
                           requests=requests, state=state, provider_factory=provider_factory,
                           infrastructure=infrastructure, transport=service.source_client.transport)


async def test_no_argument_loader_assembles_installed_resources_and_native_adapters(installed_runtime):
    fixture = installed_runtime
    service = fixture.service
    try:
        assert isinstance(service, ExecutionService) and service.service_id == "devops"
        assert service.sessions.kw["bind"].dialect.name == "postgresql"
        assert service.archive_store.bucket == "example-archive"
        assert set(service.adapter.adapters) == {"investigation", "release_readiness"}
        await service.publish({"schema_version": "1.0", "work_id": "work-one", "generation": 1}, 3)
        sent = fixture.infrastructure["sqs"].send_message.call_args.kwargs
        assert sent["QueueUrl"].endswith("/example-devops") and sent["DelaySeconds"] == 3
        assert json.loads(sent["MessageBody"])["work_id"] == "work-one"
    finally:
        await fixture.transport.aclose()


async def test_scoped_source_and_native_access_rechecks_shared_authority(installed_runtime):
    fixture = installed_runtime
    try:
        assert await fixture.service.authorize(fixture.run, fixture.claim, "materialize") is None
        session = await fixture.service.authorize(fixture.run, fixture.claim, "submit")
        result = await asyncio.to_thread(session.client("devops-agent").get_agent_space, agentSpaceId="space-one")
        assert result["agentSpace"]["agentSpaceId"] == "space-one"
        assert sum(request.url.path.endswith("/credential-session") for request in fixture.requests) == 1
        assert sum(request.url.path.endswith("/snapshot-one/read") for request in fixture.requests) == 2
        assert any(call.kwargs.get("aws_access_key_id") == "scoped-access" for call in fixture.provider_factory.call_args_list)
        fixture.state["denied"] = True
        with pytest.raises(AuthorityRevoked):
            await asyncio.to_thread(session.client("devops-agent").get_agent_space, agentSpaceId="space-one")
        assert fixture.native.get_agent_space.call_count == 1
        with pytest.raises(AuthorityRevoked, match="client outside service scope"):
            session.client("iam")
    finally:
        await fixture.transport.aclose()


@pytest.mark.parametrize("service,capability", [("security", "release_readiness"), ("devops", "cost_inquiry")])
async def test_unowned_work_is_rejected_before_credentials(installed_runtime, service, capability):
    fixture = installed_runtime
    fixture.run.service_id = service
    fixture.run.input["capability"] = capability
    try:
        with pytest.raises(AuthorityRevoked):
            await fixture.service.authorize(fixture.run, fixture.claim, "submit")
        assert fixture.requests == []
        fixture.native.get_agent_space.assert_not_called()
    finally:
        await fixture.transport.aclose()


async def test_source_free_investigation_uses_scoped_broker_authority(installed_runtime):
    fixture = installed_runtime
    fixture.run.input = {"capability": "investigation", "devops_input": {"kind": "investigation"}}
    fixture.run.admission["source_snapshots"] = []
    fixture.run.admission["native_space_ref"] = {
        "tenant_id": fixture.run.tenant_id, "project_id": fixture.run.project_id,
        "source_connection_id": fixture.run.admission["connection_ref"]["source_connection_id"],
        "record_type": "agent_space", "record_id": "space-one", "record_revision": "1"}
    try:
        session = await fixture.service.authorize(fixture.run, fixture.claim, "submit")
        assert session.account_id == fixture.run.admission["provider_identity"]["account_id"]
        await asyncio.to_thread(session.client("devops-agent").get_agent_space, agentSpaceId="space-one")
        assert all(request.url.path.endswith("/credential-session") for request in fixture.requests)
        fixture.state["denied"] = True
        with pytest.raises(AuthorityRevoked):
            await asyncio.to_thread(session.client("devops-agent").get_agent_space, agentSpaceId="space-one")
        assert fixture.native.get_agent_space.call_count == 1
    finally:
        await fixture.transport.aclose()


async def test_installed_operation_router_uses_real_native_lifecycle(lifecycle):
    _, run, attempt, task, provider, stub = lifecycle
    adapter = DevopsOperations()
    stub.add_response("get_backlog_task", {"task": {**task, "status": "COMPLETED"}},
                      {"agentSpaceId": "space-one", "taskId": "task-one"})
    outcome = await adapter.observe(run, run.native_job_ref, attempt, provider)
    assert outcome.classification == "completed"


async def test_installed_devops_factory_mounts_only_qualified_consumer_contracts(installed_runtime, monkeypatch):
    from l5_services.publication.installed import InstalledPublicationClient
    fixture = installed_runtime
    assert fixture.service.devops_context_client is None and fixture.service.devops_insights_client is None
    contract = {"kind": "devops_result", "schema_version": "1.0", "private_read_fencing": True,
                "qualification_ref": "qualification-example"}
    monkeypatch.setenv("L5_CONSUMER_CONTRACTS", json.dumps({"devops": {
        "product-context": {**contract, "path": "/internal/v1/context/l5-devops-contributions"},
        "engineering-insights": {**contract, "path": "/internal/v1/insights/l5-devops-contributions"},
    }}))
    mounted = load_service()
    try:
        for client in (mounted.devops_context_client, mounted.devops_insights_client):
            assert isinstance(client, InstalledPublicationClient)
            assert client.service_kind == "devops" and client.devops
            assert client.sessions is mounted.sessions
            assert client.transport is mounted.source_client.transport
    finally:
        await fixture.transport.aclose()
        await mounted.source_client.transport.aclose()
