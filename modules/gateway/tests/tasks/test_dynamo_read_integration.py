"""T6 adapters with T1 transactions and moto DynamoDB/S3, not live evidence."""

import hashlib
import uuid
from dataclasses import replace

import boto3
import pytest

from src.tasks.dynamo_read_store import DynamoTaskReadStore
from src.tasks.read_store import ArtifactRecord, ReportConflictError, SequenceFencedError, TaskStoreError
from tests.tasks import test_store as repository_tests
from tests.tasks.test_store import _request


@pytest.fixture
def client():
    yield from repository_tests.client.__wrapped__()


@pytest.fixture
def store(client):
    return repository_tests.store.__wrapped__(client)


@pytest.fixture
def adapter(store):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="task-inputs")
    return DynamoTaskReadStore(store, s3_client=s3, artifact_bucket="task-inputs")


def test_real_acceptance_is_readable_with_durable_events(adapter, store):
    request = _request(request_payload={"instructions": "inspect", "inputs": {"fraction": 1.5}})
    store.accept(request)
    record = adapter.load_task(task_id=request.task_id)
    assert record.status == "accepted"
    assert record.tenant_id == request.tenant
    assert record.owner_principal_id == request.canonical_principal
    assert record.latest_sequence == record.oldest_sequence == 1
    assert record.execution_health == "unknown"
    event = adapter.read_events(task_id=request.task_id, after_sequence=0, limit=10)[0]
    assert event.task_id == request.task_id and event.type == "task.accepted"
    assert event.data["status"] == "accepted"


def _artifact():
    content = b"input evidence"
    return ArtifactRecord(
        artifact_id="art_" + str(uuid.uuid4()),
        version=1,
        tenant_id="tenant-a",
        owner_principal_id="svc-principal-1",
        content_type="text/plain",
        content_sha256=hashlib.sha256(content).hexdigest(),
        content_length=len(content),
        created_at="2026-09-24T12:00:00Z",
        expires_at="2026-09-25T12:00:00Z",
    ), content


def test_artifact_bytes_and_binding_share_t1_key_and_survive_read(adapter):
    record, content = _artifact()
    stored = adapter.put_artifact(record=record, content=content)
    assert stored.storage_key.startswith("tasks/")
    assert adapter.read_artifact(record=adapter.load_artifact(artifact_id=record.artifact_id)) == content
    assert adapter.put_artifact(record=record, content=content) == stored
    changed = b"new evidence!"
    forged = replace(record, content_length=len(changed), content_sha256=hashlib.sha256(changed).hexdigest())
    with pytest.raises(TaskStoreError):
        adapter.put_artifact(record=forged, content=changed)
    assert adapter.read_artifact(record=stored) == content


def test_corrupt_s3_content_is_never_delivered(adapter):
    record, content = _artifact()
    stored = adapter.put_artifact(record=record, content=content)
    adapter.s3.put_object(Bucket=adapter.bucket, Key=stored.storage_key, Body=b"corrupt")
    with pytest.raises(TaskStoreError, match="integrity"):
        adapter.read_artifact(record=stored)


def test_report_uses_real_attempt_transaction_and_dedup(adapter, store):
    request = _request()
    store.accept(request)
    attempt = str(uuid.uuid4())
    store.bind_runtime_attempt(
        task_id=request.task_id, invocation_id=request.invocation_id, generation=1, runtime_attempt_id=attempt, expected_version=1
    )
    args = dict(
        task_id=request.task_id,
        report_id=str(uuid.uuid4()),
        event_type="progress.updated",
        data={"message": "inspecting", "stage": "analysis"},
        producer_timestamp=None,
        timestamp="2026-09-24T12:00:00Z",
        expect_generation=1,
        expect_runtime_attempt_id=attempt,
    )
    result = adapter.append_event(**args)
    assert result.event.sequence == 3
    assert adapter.append_event(**args).event.sequence == 3
    events = adapter.read_events(task_id=request.task_id, after_sequence=0, limit=10)
    assert [event.type for event in events] == ["task.accepted", "run.started", "progress.updated"]
    assert adapter.load_task(task_id=request.task_id).status == "running"
    with pytest.raises(ReportConflictError):
        adapter.append_event(**{**args, "data": {"message": "changed", "stage": "analysis"}})
    with pytest.raises(SequenceFencedError):
        adapter.append_event(**{**args, "report_id": str(uuid.uuid4()), "expect_runtime_attempt_id": str(uuid.uuid4())})


def _attempt(store):
    from types import SimpleNamespace

    request = _request()
    store.accept(request)
    attempt_id = str(uuid.uuid4())
    store.bind_runtime_attempt(
        task_id=request.task_id, invocation_id=request.invocation_id, generation=1, runtime_attempt_id=attempt_id, expected_version=1
    )
    return SimpleNamespace(
        task_id=request.task_id,
        invocation_id=request.invocation_id,
        generation=1,
        runtime_attempt_id=attempt_id,
        tenant=request.tenant,
        canonical_principal=request.canonical_principal,
    )


def test_result_upload_is_bound_idempotent_and_charges_aggregate_once(adapter, store):
    attempt = _attempt(store)
    content = b"x" * 300000
    digest = hashlib.sha256(content).hexdigest()
    record = adapter.put_run_artifact(attempt=attempt, content=content, content_type="text/plain", digest=digest)
    assert record.task_id == attempt.task_id
    assert adapter.read_artifact(record=record) == content
    assert adapter.put_run_artifact(attempt=attempt, content=content, content_type="text/plain", digest=digest) == record
    assert int(store.read_task(attempt.task_id)["result_artifact_bytes"]) == len(content)
    snapshot = store.read_task(attempt.task_id)
    assert snapshot["version"] == 3  # accept, attempt registration, one upload
    assert snapshot["result_artifact_ids"] == [record.artifact_id]
    # A finalizer which read the pre-upload version must retry and collect the
    # committed artifact references before establishing retention.
    from src.tasks.records import TaskState
    from src.tasks.store import TaskStateConflictError

    with pytest.raises(TaskStateConflictError):
        store.transition(task_id=attempt.task_id, expected_version=2, target_state=TaskState.FAILED)
    large = b"y" * 800000
    with pytest.raises(TaskStoreError, match="Aggregate"):
        adapter.put_run_artifact(attempt=attempt, content=large, content_type="text/plain", digest=hashlib.sha256(large).hexdigest())


def test_current_policy_revocation_denies_reads_and_result_commit(adapter, store, client):
    from src.tasks import errors
    from src.tasks.records import task_authority_partition, task_policy_sort_key

    attempt = _attempt(store)
    adapter.require_policy(tenant=attempt.tenant, principal=attempt.canonical_principal, persona="agent-task-investigator")
    client.update_item(
        TableName=store.authority_table_name,
        Key={"pk": {"S": task_authority_partition(attempt.tenant)}, "sk": {"S": task_policy_sort_key(attempt.canonical_principal)}},
        UpdateExpression="SET #status = :revoked",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":revoked": {"S": "revoked"}},
    )
    with pytest.raises(errors.TaskApiError):
        adapter.require_policy(tenant=attempt.tenant, principal=attempt.canonical_principal, persona="agent-task-investigator")
    with pytest.raises(TaskStoreError):
        adapter.put_run_artifact(attempt=attempt, content=b"result", content_type="text/plain", digest=hashlib.sha256(b"result").hexdigest())
    assert int(store.read_task(attempt.task_id).get("result_artifact_bytes", 0)) == 0


@pytest.mark.asyncio
async def test_internal_artifact_http_upload_and_input_read(adapter, store, monkeypatch):
    import base64
    from dataclasses import replace
    from types import SimpleNamespace

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.agentauth import task_runtime_routes
    from src.agentauth.routes import require_agent_transport
    from src.tasks import http, internal_artifacts

    uploaded, content = _artifact()
    stored = adapter.put_artifact(record=uploaded, content=content)
    request = _request(
        artifact_ids=[stored.artifact_id], request_payload={"instructions": "investigate", "inputs": {"a": 1}, "artifact_ids": [stored.artifact_id]}
    )
    reference = {
        **request.input_reference,
        "artifact_refs": [{"artifact_id": stored.artifact_id, "version": 1, "content_sha256": stored.content_sha256}],
    }
    request = replace(
        request,
        input_reference=reference,
        envelope={**request.envelope, "input_ref": reference},
        immutable_input={
            **request.immutable_input,
            "artifacts": [
                {"artifact_id": stored.artifact_id, "version": 1, "content_sha256": stored.content_sha256, "content_type": stored.content_type}
            ],
        },
    )
    store.accept(request)
    attempt_id = str(uuid.uuid4())
    store.bind_runtime_attempt(
        task_id=request.task_id, invocation_id=request.invocation_id, generation=1, runtime_attempt_id=attempt_id, expected_version=1
    )
    attempt = SimpleNamespace(
        task_id=request.task_id,
        invocation_id=request.invocation_id,
        generation=1,
        runtime_attempt_id=attempt_id,
        tenant=request.tenant,
        canonical_principal=request.canonical_principal,
    )

    async def authenticate(_):
        return attempt

    monkeypatch.setattr(task_runtime_routes, "authenticate_task_attempt", authenticate)
    monkeypatch.setattr(internal_artifacts, "get_store", lambda: adapter)
    monkeypatch.setenv(http.FLAG_WORKER, "true")
    app = FastAPI()
    app.dependency_overrides[require_agent_transport] = lambda: None
    app.include_router(internal_artifacts.router)
    binding = {"task_id": attempt.task_id, "invocation_id": attempt.invocation_id, "generation": 1}
    body = {"schema_version": "1.0", "operation": "read", "run": binding, "artifact_id": stored.artifact_id}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/internal/v1/agent/task/artifact", json=body)
        assert response.status_code == 200, response.text
        assert base64.b64decode(response.json()["content_base64"]) == content
        response = await client.post("/internal/v1/agent/task/artifact", json={**body, "run": {**binding, "generation": True}})
        assert response.status_code == 409
        out = b"result evidence"
        response = await client.post(
            "/internal/v1/agent/task/artifact",
            json={
                "schema_version": "1.0",
                "run": binding,
                "content_type": "text/plain",
                "content_sha256": hashlib.sha256(out).hexdigest(),
                "content_base64": base64.b64encode(out).decode(),
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["expires_at"] is None
        response = await client.post("/internal/v1/agent/task/artifact", json={**body, "artifact_id": response.json()["artifact_id"]})
        assert response.status_code == 404


def test_generated_html_result_preserves_binding_digest_and_replay(adapter, store):
    attempt = _attempt(store)
    content = b"<!doctype html><html><body>Evidence report</body></html>"
    digest = hashlib.sha256(content).hexdigest()
    record = adapter.put_run_artifact(attempt=attempt, content=content, content_type="text/html", digest=digest)
    assert record.content_type == "text/html"
    assert record.task_id == attempt.task_id
    assert adapter.read_artifact(record=record) == content
    assert adapter.put_run_artifact(attempt=attempt, content=content, content_type="text/html", digest=digest) == record
    assert int(store.read_task(attempt.task_id)["result_artifact_bytes"]) == len(content)
