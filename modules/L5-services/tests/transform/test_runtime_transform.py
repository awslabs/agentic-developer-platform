"""Installed Transform factory exercised through SQL work with only external boundaries mocked."""

import base64
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services import runtime_transform
from l5_services.models import Configuration, ConfigurationVersion, Run, Work
from l5_services.models.base import Base
from l5_services.workers.execution import load_service
from test_executor_source import fixture as source_fixture
from test_isolated_executor import IMAGE, NODE_UID, KubernetesBoundary, S3Boundary, observation
from test_provider_job import admitted
from l5_services.adapters.transform.provider_job import NDJSON_BINARY_SHA256, NDJSON_INTERFACE


@pytest.mark.asyncio
async def test_installed_factory_runs_durable_source_baseline_provider_candidate_and_retention(monkeypatch, tmp_path):
    resources = pytest.importorskip("l5_services.runtime_support", reason="requires the published shared runtime integration")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    clock = [now]
    client, manifest, _, _ = source_fixture()
    receipt = await client.read_source(None, None, None, None)
    receipt["expires_at"] = (now + timedelta(hours=1)).isoformat()
    receipt["content"] = {**receipt["content"], "version": "source-content-v1"}
    receipt["file_manifest"] = {**receipt["file_manifest"], "version": "source-manifest-v1"}
    inventory = await client.read_source_manifest(None, None, None, None)
    archive = b"".join([chunk async for chunk in client.stream_source(None, None, None, None)])
    run_fixture, _, _ = admitted()
    admission = deepcopy(run_fixture.admission)
    identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/example", "region": "us-east-1"}
    connection_ref = {**manifest["sources"][0], "record_type": "connection", "record_id": "repository"}
    admission.update(source_snapshots=[receipt], connection_ref=connection_ref, provider_identity=identity,
        delegation_id="grant-a", delegation_version=1, binding_generation=1,
        configuration_hash="e" * 64, profile_ref={"record_id": "product-a"}, adapter_revision="transform-v1",
        exclusions=[], monitoring_policy={"poll_interval_seconds": 1, "poll_grace_seconds": 60},
        limits={"agent_minutes": 10, "deadline_seconds": 3600, "max_polls": 20})
    admission["transform_settings"].update(connection_ref=connection_ref, region="us-east-1", write_paths=["src/example.py"])
    admission["transform_qualification"].update(interface=NDJSON_INTERFACE, cli_version="3.19.0", binary_sha256=NDJSON_BINARY_SHA256)
    policy = admission["transform_test_policy"]
    policy["commands"][0].update(argv=["pytest", "-q"], timeout_seconds=120)
    policy["oracle_version"] = "pytest-v1"
    async with sessions() as session:
        session.add(Configuration(tenant_id="tenant-a", configuration_id="config-a", product_id="product-a",
            project_id="project-a", service_id="transform", relationship_revision="1", owner_id="owner-a",
            current_revision=1, handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a", revision=1,
            settings={}, inherited_fields=[], gaps=[], state="ready", hash="e" * 64))
        session.add(Run(tenant_id="tenant-a", run_id="run-a", product_id="product-a", project_id="project-a",
            service_id="transform", configuration_id="config-a", configuration_revision=1, binding_id="binding-a",
            trigger_id="manual-a", binding_generation=1, admission=admission,
            input={"sources": manifest["sources"], "capability": "refactor", "transform_input": run_fixture.input["transform_input"]},
            deadline_at=now + timedelta(hours=1), capture_limits={"max_bytes": 65536, "max_files": 10}))
        session.add(Work(tenant_id="tenant-a", work_id="execute-a", run_id="run-a", kind="execute", generation=1,
                         step=0, due_at=now))
        await session.commit()
    requests = []
    source_ranges = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(request.url.path)
        assert request.method == "POST"
        assert body["operation_id"] == "run-a" and body["lease_fence"] >= 1
        if request.url.path == "/internal/v1/l5/credential-session":
            return httpx.Response(200, json={"operation_id": body["operation_id"], "attempt_id": body["attempt_id"],
                "delegation_revision": 1, "provider_identity": identity,
                "expires_at": (now + timedelta(minutes=30)).isoformat(),
                "access_key_id": "fixture-access", "secret_access_key": "fixture-secret", "session_token": "fixture-token"})
        expected = "/internal/v1/products/product-a/projects/project-a/source-snapshots/sealed-a/"
        assert request.url.path.startswith(expected)
        assert request.headers["X-L5-Delegation-ID"] == body["delegation_id"] == "grant-a"
        resource = request.url.path.removeprefix(expected)
        if resource == "read":
            assert "Range" not in request.headers
            return httpx.Response(200, json=receipt)
        assert resource in {"manifest", "content"}
        artifact = receipt["file_manifest" if resource == "manifest" else "content"]
        contents = inventory if resource == "manifest" else archive
        selected = request.headers["Range"]
        assert selected.startswith("bytes=")
        start, end = map(int, selected.removeprefix("bytes=").split("-"))
        assert 0 <= start <= end < artifact["bytes"] == len(contents)
        assert end - start + 1 <= 1024 * 1024
        source_ranges.append((resource, start, end))
        return httpx.Response(206, content=contents[start:end + 1], headers={
            "Content-Range": f"bytes {start}-{end}/{artifact['bytes']}",
            "X-Artifact-Sha256": artifact["sha256"], "X-Artifact-Version": artifact["version"],
            "X-Artifact-Size": str(artifact["bytes"]), "Content-Length": str(end - start + 1)})

    transport = httpx.AsyncClient(base_url="https://example.invalid", transport=httpx.MockTransport(respond))
    api, s3 = KubernetesBoundary(), S3Boundary()
    published = []

    async def publish(envelope, delay):
        published.append((envelope, delay))

    monkeypatch.setattr(resources, "gateway_transport", lambda: transport)
    monkeypatch.setattr(resources, "sessions", lambda: sessions)
    monkeypatch.setattr(resources, "archive_store", lambda service_id: object())
    monkeypatch.setattr(resources, "queue_publisher", lambda service_id: publish)
    monkeypatch.setattr(runtime_transform, "TransformKubernetesAPI", lambda **kwargs: api)
    monkeypatch.setattr(runtime_transform.boto3, "client", lambda *args, **kwargs: s3)
    for key, value in {"L5_SERVICE_ID": "transform", "L5_WORKER_FACTORY": "l5_services.runtime_transform:factory",
        "AWS_REGION": "us-east-1", "L5_TRANSFORM_CLUSTER_ENDPOINT": "https://example.eks.amazonaws.com",
        "L5_TRANSFORM_CLUSTER_NAME": "example", "L5_TRANSFORM_CLUSTER_CA": "fixture-ca",
        "L5_TRANSFORM_NODE_NAME": "isolated-example", "L5_TRANSFORM_NODE_UID": NODE_UID,
        "L5_TRANSFORM_PROVIDER_NAMESPACE": "transform-provider", "L5_TRANSFORM_CHECK_NAMESPACE": "transform-checks",
        "L5_TRANSFORM_PROVIDER_IMAGE": IMAGE, "L5_TRANSFORM_CHECK_IMAGE": IMAGE,
        "L5_TRANSFORM_ARTIFACT_BUCKET": "example-artifacts"}.items():
        monkeypatch.setenv(key, value)
    service = load_service()
    service.clock = lambda: clock[0]
    assert service.service_id == "transform"
    assert service.transform_adapter.require_original_patch is True
    assert service.runtime_connectivity_probe.args == (api,)
    assert service.runtime_connectivity_probe.keywords == {
        "node_name": "isolated-example", "node_uid": NODE_UID,
        "provider_namespace": "transform-provider", "check_namespace": "transform-checks"}
    monkeypatch.delenv("L5_TRANSFORM_PROVIDER_IMAGE")
    with pytest.raises(ValueError, match="L5_TRANSFORM_PROVIDER_IMAGE"):
        load_service()
    assert api.calls == [] and s3.objects == {}
    monkeypatch.setenv("L5_TRANSFORM_PROVIDER_IMAGE", IMAGE)
    patch = b"diff --git a/src/example.py b/src/example.py\n--- a/src/example.py\n+++ b/src/example.py\n@@ -1 +1 @@\n-original\n+changed\n"
    try:
        for _ in range(12):
            async with sessions() as session:
                work = await session.scalar(select(Work).where(Work.completed_at.is_(None), Work.blocked_at.is_(None)))
                if work is None:
                    break
                clock[0] = max(clock[0], work.due_at.replace(tzinfo=UTC)) + timedelta(seconds=1)
                envelope = {"schema_version": "1.0", "work_id": work.work_id, "generation": work.generation}
            assert await service.execute(envelope) is True
            assert await service.execute(envelope) is False
            for path, pod in list(api.objects.items()):
                if pod["kind"] != "Pod" or path + "/log" in api.logs:
                    continue
                name = pod["spec"]["containers"][0]["name"]
                pod["status"] = {"containerStatuses": [{"name": name, "imageID": IMAGE, "state": {"terminated": {
                    "startedAt": clock[0].isoformat(), "finishedAt": (clock[0] + timedelta(milliseconds=100)).isoformat(),
                    "exitCode": 0, "reason": "Completed"}}}]}
                if name == "check":
                    api.logs[path + "/log"] = b"2 passed in 0.1s\n"
                else:
                    chunks = []
                    for projection in pod["spec"]["volumes"][0]["projected"]["sources"]:
                        config = api.objects["/api/v1/namespaces/transform-provider/configmaps/" + projection["configMap"]["name"]]
                        chunks.extend(config["data"].values())
                    intent = json.loads("".join(chunks))["intent"]
                    api.logs[path + "/log"] = observation(intent, "completed", patch=base64.b64encode(patch).decode(),
                        candidate_files={"src/example.py": base64.b64encode(b"changed\n").decode()},
                        candidate_modes={"src/example.py": "100644"})
        async with sessions() as session:
            run = await session.get(Run, ("tenant-a", "run-a"))
            assert run.state == "succeeded"
            assert run.native_job_ref["native_id"] == "native-a"
        launches = [call for call in api.calls if call[0] == "POST" and call[1] == "/api/v1/namespaces/transform-provider/pods"]
        assert len(launches) == 1
        assert published and "/internal/v1/l5/credential-session" in requests
        assert {resource for resource, _, _ in source_ranges} == {"manifest", "content"}
        assert any(value["Body"] == patch for value in s3.objects.values())
    finally:
        await transport.aclose()
        await engine.dispose()
