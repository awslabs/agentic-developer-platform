"""Exercise installed resources, mocking database, HTTP and AWS boundaries only."""

import io
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from botocore.credentials import Credentials
from botocore.hooks import HierarchicalEmitter
from sqlalchemy import select

from l5_services import runtime_support, runtime_well_architected
from l5_services.adapters.well_architected.runtime_callbacks import PublicationClient, compare_source_statement
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration
from l5_services.models.evidence import AssessmentResult
from l5_services.models.publication import Publication
from l5_services.models.run import Run
from l5_services.publication.recovery import repair_due_publications
from l5_services.workers.execution import AuthorityRevoked, load_service
from l5_services.workers.relay import repair_once
from test_wa_runtime import NOW, SCOPE, assembled_worker


CONTRACTS = {
    destination: {"kind": "well_architected_result", "schema_version": "1.0",
                  "private_read_fencing": True, "path": f"/internal/v1/{peer}/wa-contributions"}
    for destination, peer in (("product-context", "context"), ("engineering-insights", "insights"))
}


def mount_environment(monkeypatch, tmp_path):
    for name, value in {
        "L5_SERVICE_ID": "well_architected",
        "L5_WORKER_FACTORY": "l5_services.runtime_well_architected:factory",
        "L5_WA_PEER_CONTRACTS": json.dumps(CONTRACTS),
        "AWS_REGION": "us-east-1", "AWS_LAMBDA_FUNCTION_NAME": "fictional-wa-worker",
        "AWS_ACCESS_KEY_ID": "fictional", "AWS_SECRET_ACCESS_KEY": "fictional",
        "AWS_SESSION_TOKEN": "fictional", "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null", "AWS_EC2_METADATA_DISABLED": "true",
        "L5_GATEWAY_URL": "https://testapi.execute-api.us-east-1.amazonaws.com/dev",
        "L5_RDS_HOST": "database.example.invalid", "L5_RDS_PORT": "5432",
        "L5_RDS_DATABASE": "example", "L5_RDS_USER": "wa_worker",
        "L5_ARCHIVE_BUCKET": "fictional-wa-bucket",
        "L5_WORK_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/000000000000/wa-work",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
async def installed(monkeypatch, tmp_path, request):
    engine, sessions, _, provider, _, _, archive = await assembled_worker(tmp_path)
    mount_environment(monkeypatch, tmp_path)
    peers = getattr(request, "param", tuple(CONTRACTS))
    if peers is None:
        monkeypatch.delenv("L5_WA_PEER_CONTRACTS")
    else:
        monkeypatch.setenv("L5_WA_PEER_CONTRACTS", json.dumps({peer: CONTRACTS[peer] for peer in peers}))
    content = archive.get_archive.return_value
    identity = {"account_id": "000000000000", "region": "us-east-1",
                "role_arn": "arn:aws:iam::000000000000:role/broker-example"}
    async with sessions() as session:
        run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
        run.admission = {**run.admission, "provider_identity": identity}
        reference = {**run.manifest["archive"], "artifact_id":
                     f"l5/well_architected/work/{run.run_id}/{run.manifest['archive']['sha256']}.zip"}
        run.manifest = {**run.manifest, "archive": reference,
                        "provider_source_uri": f"s3://fictional-wa-bucket/{reference['artifact_id']}",
                        "archive_readback": {"artifact": reference, "sha256": reference["sha256"],
                                             "bytes": reference["bytes"]}}
        snapshot = run.admission["source_snapshots"][0]
        await session.commit()
    state = SimpleNamespace(broker_status=200, source_status=200, identity=identity,
                            expires_at=datetime.now(UTC) + timedelta(minutes=10),
                            queue=[], requests=[], configs=[], objects=[], broker_extra={}, consumer=None)

    def http_boundary(request):
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        state.requests.append(request)
        if "wa-contributions" in request.url.path:
            return state.consumer(request)
        body = json.loads(request.content)
        assert body["lease_fence"] > 0 and body["lease_owner"]
        if request.url.path.endswith("/credential-session"):
            if state.broker_status != 200:
                return httpx.Response(state.broker_status)
            return httpx.Response(200, json={
                "operation_id": body["operation_id"], "attempt_id": body["attempt_id"],
                "delegation_revision": body["delegation_revision"], "provider_identity": state.identity,
                "expires_at": state.expires_at.isoformat(),
                "access_key_id": "fictional", "secret_access_key": "fictional",
                "session_token": "fictional", **state.broker_extra})
        assert request.url.path.endswith("/source-snapshots/snapshot-example/read")
        assert request.headers["X-L5-Delegation-ID"] == "delegation-example"
        return httpx.Response(state.source_status, json=snapshot)

    real_http_client = httpx.AsyncClient
    monkeypatch.setattr(runtime_support.httpx, "AsyncClient", lambda **options: real_http_client(
        **options, transport=httpx.MockTransport(http_boundary)))
    monkeypatch.setattr(runtime_support, "create_async_engine", lambda *args, **kwargs: engine)

    class AWSBoundary:
        def __init__(self, **credentials):
            if credentials:
                assert credentials["aws_access_key_id"] == "fictional"
                assert credentials["region_name"] == identity["region"]

        def get_credentials(self):
            return Credentials("fictional", "fictional", "fictional")

        def client(self, service, *, region_name, config=None):
            assert service == "wellarchitected" and region_name == identity["region"]
            provider.meta = SimpleNamespace(config=config, events=HierarchicalEmitter())
            state.configs.append(config)
            return provider

    def get_object(**request):
        assert request == {"Bucket": "fictional-wa-bucket", "Key": reference["artifact_id"],
                           "VersionId": reference["version"]}
        state.objects.append(request)
        return {"VersionId": reference["version"], "ContentLength": len(content), "Body": io.BytesIO(content)}

    def aws_client(service, *, region_name):
        assert region_name == "us-east-1"
        if service == "s3":
            return SimpleNamespace(meta=SimpleNamespace(region_name=region_name), get_object=get_object)
        assert service == "sqs"
        return SimpleNamespace(send_message=lambda **request: state.queue.append(json.loads(request["MessageBody"])))

    monkeypatch.setattr(runtime_support.boto3, "Session", AWSBoundary)
    monkeypatch.setattr(runtime_support.boto3, "client", aws_client)
    worker = load_service()
    clock = [NOW]
    worker.clock = lambda: clock[0]
    try:
        yield SimpleNamespace(worker=worker, sessions=sessions, provider=provider, state=state, clock=clock)
    finally:
        await worker.source_client.transport.aclose()
        await engine.dispose()


async def complete_run(installed):
    worker, state = installed.worker, installed.state
    envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1}
    assert worker.service_id == "well_architected"
    assert await worker.execute({**envelope, "work_id": "foreign-work"}) is False
    assert await worker.execute({**envelope, "generation": 2}) is False
    assert not state.requests
    assert await worker.execute(envelope) is True
    assert await worker.execute(envelope) is False
    assert len(installed.provider.starts) == 1
    assert state.objects
    mutation = next(config for config in state.configs if config is not None)
    assert mutation.retries["total_max_attempts"] == 1
    for _ in range(12):
        if not state.queue:
            break
        installed.clock[0] += timedelta(seconds=10)
        await worker.execute(state.queue.pop(0))
    assert len(installed.provider.starts) == 1
    async with installed.sessions() as session:
        results = (await session.scalars(select(AssessmentResult).order_by(AssessmentResult.version))).all()
        assert [result.version for result in results] == [1, 2]
        assert results[1].payload["based_on_result_id"] == results[0].result_id
        assert results[1].payload["comparison"][0]["status"] == "not_verifiable"
        assert (await session.scalars(select(WorkerAttempt))).one().status == "accepted"


async def test_installed_factory_runs_one_scoped_generation_and_collects_original(installed):
    await complete_run(installed)


@pytest.mark.parametrize("installed", [None, (), ("product-context",), ("engineering-insights",)], indirect=True)
async def test_missing_peers_preserve_native_results_and_block_publication(installed):
    await complete_run(installed)
    clients = {"product-context": installed.worker.well_architected_context_client,
               "engineering-insights": installed.worker.well_architected_insights_client}
    if all(client is None for client in clients.values()):
        await repair_once(installed.worker, now=installed.clock[0])
        assert len(installed.provider.starts) == 1
    async with installed.sessions() as session:
        publications = (await session.scalars(select(Publication).where(Publication.result_version == 2))).all()
        assert {publication.destination for publication in publications} == set(clients)
        for publication in publications:
            assert publication.receipt is None and publication.attempts == 0
            if clients[publication.destination] is None:
                assert publication.state == "unavailable"
                assert publication.last_error == "consumer_schema_unadmitted"
            else:
                assert publication.state == "pending"
    assert not any("wa-contributions" in request.url.path for request in installed.state.requests)


async def test_installed_factory_recovers_lost_start_response_without_resubmission(installed):
    from botocore.exceptions import EndpointConnectionError

    provider = installed.provider
    start = provider.start_agent_recommendation_generation

    def lost_response(**request):
        start(**request)
        raise EndpointConnectionError(endpoint_url="https://example.invalid")

    provider.start_agent_recommendation_generation = lost_response
    provider.list_agent_recommendation_generations = lambda **request: {"items": [
        {"id": "original-generation", "profileArn": provider.native["profile_arn"],
         "name": provider.native["name"], "createdAt": NOW}] if provider.starts else []}
    envelope = {"schema_version": "1.0", "work_id": "work-example", "generation": 1}
    assert await installed.worker.execute(envelope)
    async with installed.sessions() as session:
        attempt = (await session.scalars(select(WorkerAttempt))).one()
        assert attempt.status == "outcome_unknown" and attempt.native_job_ref is None
    assert not await installed.worker.execute(envelope)
    installed.clock[0] += timedelta(seconds=600)
    assert await installed.worker.execute(installed.state.queue.pop(0))
    assert len(provider.starts) == 1
    async with installed.sessions() as session:
        attempt = (await session.scalars(select(WorkerAttempt))).one()
        assert attempt.status == "accepted" and attempt.reconcile_checks == 1
        assert attempt.native_job_ref["generation_id"] == "original-generation"


@pytest.mark.parametrize("destination", ["product-context", "engineering-insights"])
@pytest.mark.parametrize("response_kind", ["valid", "revoked", "changed_projection"])
async def test_installed_publication_requires_current_delegation_and_exact_readback(installed, destination, response_kind):
    await complete_run(installed)
    async with installed.sessions() as session:
        publication = await session.scalar(select(Publication).where(
            Publication.destination == destination, Publication.result_version == 2))
    published = []

    def consumer(request):
        assert request.headers["X-L5-Delegation-ID"] == "delegation-example"
        if response_kind == "revoked":
            return httpx.Response(403)
        if request.method == "POST":
            assert json.loads(request.content) == publication.payload
            published.append(request)
        elif not published:
            return httpx.Response(404)
        if request.url.path.endswith("/projection"):
            projection = {**publication.payload, "contribution_hash": publication.contribution_hash}
            if response_kind == "changed_projection":
                projection["source_refs"] = []
            return httpx.Response(200, json=projection)
        return httpx.Response(200, json={
            "receipt_id": "receipt-example", "contribution_id": publication.publication_id,
            "admitted_schema": "1.0", "consumer_revision": "1",
            "record_refs": [publication.payload["result_ref"]]})

    installed.state.consumer = consumer
    client = (installed.worker.well_architected_context_client if destination == "product-context"
              else installed.worker.well_architected_insights_client)
    count = await repair_due_publications(installed.sessions, client, now=installed.clock[0])
    assert count == 1
    async with installed.sessions() as session:
        observed = await session.get(Publication, (publication.tenant_id, publication.publication_id))
        assert observed.state == {"valid": "published", "revoked": "unavailable",
                                  "changed_projection": "retry_wait"}[response_kind]
    assert len(installed.provider.starts) == 1
    foreign = deepcopy(publication)
    foreign.payload = {**publication.payload, "scope": {**SCOPE, "project_id": "foreign-project"}}
    calls = len(installed.state.requests)
    with pytest.raises(ValueError, match="durable Product work"):
        await client.read_receipt(foreign)
    assert len(installed.state.requests) == calls
    if response_kind == "valid":
        publication.state = "invalidation_pending"
        publication.invalidation = {"reason": "source_revoked"}
        publication.invalidation_watermark = 2

        def invalidate(request):
            assert request.url.path.endswith("/invalidate")
            assert request.headers["X-L5-Delegation-ID"] == "delegation-example"
            assert json.loads(request.content)["invalidation_watermark"] == 2
            return httpx.Response(200, json={"receipt_id": "invalidation-example", "applied_watermark": 2,
                                            "record_refs": [publication.payload["result_ref"]]})

        installed.state.consumer = invalidate
        assert (await client.invalidate(publication))["applied_watermark"] == 2


@pytest.mark.parametrize("change", ["broker_revoked", "source_revoked", "foreign_role", "expired_session",
                                   "binding_revoked", "stale_configuration", "expired_run"])
async def test_installed_factory_denies_invalid_authority_before_start(installed, change):
    if change == "broker_revoked":
        installed.state.broker_status = 403
    elif change == "source_revoked":
        installed.state.source_status = 403
    elif change == "foreign_role":
        installed.state.identity = {**installed.state.identity, "role_arn": "arn:aws:iam::000000000000:role/other"}
    elif change == "expired_session":
        installed.state.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        async with installed.sessions() as session:
            if change == "binding_revoked":
                binding = await session.get(Binding, (SCOPE["tenant_id"], "binding-example"))
                binding.state = "blocked"
            elif change == "stale_configuration":
                configuration = await session.get(Configuration, (SCOPE["tenant_id"], "configuration-example"))
                configuration.current_revision = 2
            else:
                run = await session.get(Run, (SCOPE["tenant_id"], "run-example"))
                run.deadline_at = NOW - timedelta(seconds=1)
            await session.commit()
    await installed.worker.execute({"schema_version": "1.0", "work_id": "work-example", "generation": 1})
    assert not installed.provider.starts


@pytest.mark.parametrize("installed", [None, tuple(CONTRACTS)], indirect=True)
async def test_installed_setup_requires_broker_role_qualification(installed):
    authorization = {"delegation_id": "delegation-example", "delegation_revision": 1}
    intent = SimpleNamespace(service_id="well_architected", tenant_id=SCOPE["tenant_id"],
                             product_id=SCOPE["product_id"], project_id=SCOPE["project_id"],
                             configuration_id="configuration-example", operation_id="setup-example",
                             request={"configuration_revision": 1}, scope=SCOPE, authorization=authorization)
    claim = SimpleNamespace(operation_id=intent.operation_id, work_id="setup-work", owner="owner", fence=1)
    verified = {"configuration_id": intent.configuration_id, "configuration_revision": 1,
                "scope": SCOPE, "authorization": authorization,
                "connection_ref": {"record_id": "connection-example", "record_type": "credential"},
                **installed.state.identity}
    installed.state.broker_extra = {"setup_verified": verified}
    with pytest.raises(AuthorityRevoked, match="qualification unavailable"):
        await installed.worker.binding_authorize(intent, claim)
    verified["source_role_qualification"] = {
        "account_id": installed.state.identity["account_id"], "region": "us-east-1",
        "broker_role_arn": installed.state.identity["role_arn"],
        "execution_role_arn": "arn:aws:iam::000000000000:role/reader-example",
        "trust": "qualified", "source_permissions": "qualified", "support": "eligible"}
    observed, adapter = await installed.worker.binding_authorize(intent, claim)
    assert observed == verified
    async with installed.sessions() as session:
        binding = await session.get(Binding, (SCOPE["tenant_id"], "binding-example"))
    assert (await adapter.read(binding.native_resource_ref, observed))["state"] == "ready"
    installed.state.broker_status = 403
    with pytest.raises(AuthorityRevoked, match="current setup work unavailable"):
        await adapter.read(binding.native_resource_ref, observed)
    assert not installed.provider.starts


@pytest.mark.parametrize("missing", ["L5_RDS_HOST", "L5_ARCHIVE_BUCKET", "L5_WORK_QUEUE_URL"])
def test_factory_rejects_missing_mounted_dependencies(monkeypatch, tmp_path, missing):
    mount_environment(monkeypatch, tmp_path)
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match=missing):
        load_service()


def test_factory_rejects_foreign_install_and_allow_override(monkeypatch, tmp_path):
    mount_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("L5_SERVICE_ID", "security")
    with pytest.raises(ValueError, match="service identity"):
        runtime_well_architected.factory()
    contracts = deepcopy(CONTRACTS)
    contracts["product-context"]["allow"] = True
    with pytest.raises(ValueError, match="unsupported"):
        PublicationClient.validate_contracts(contracts)


@pytest.mark.parametrize("contracts", ["null", "[]", '{"unknown-peer": {}}', '{"product-context": null}'])
def test_factory_rejects_invalid_declared_peer_contracts(monkeypatch, tmp_path, contracts):
    mount_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("L5_WA_PEER_CONTRACTS", contracts)
    with pytest.raises(ValueError, match="unsupported"):
        load_service()


@pytest.mark.parametrize("path", ["https://other.example.invalid", "/internal/v1/context/../secrets",
                                  "/internal/v1/context/%2e%2e/secrets", "/internal/v1/insights/wa",
                                  "/internal/v1/context/wa?allow=true"])
def test_peer_contract_cannot_escape_destination(path):
    contracts = deepcopy(CONTRACTS)
    contracts["product-context"]["path"] = path
    with pytest.raises(ValueError, match="unsupported"):
        PublicationClient.validate_contracts(contracts)


def test_source_comparison_does_not_infer_claims_from_title_or_partial_text():
    files = {"sources/example/design.txt": {"lines": ["Encrypt stored data."]}}
    valid = compare_source_statement({"description": "Encrypt stored data."}, files, [])
    assert valid["status"] == "supported" and valid["citations"][0]["line"] == 1
    for recommendation in ({"title": "Encrypt stored data."}, {"description": "Encrypt stored data"},
                           {"description": "Stored data is encrypted."}, {"description": ""}):
        assert compare_source_statement(recommendation, files, ["goal-example"])["status"] == "not_verifiable"
