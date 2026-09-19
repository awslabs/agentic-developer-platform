"""Real upload route and Moto S3; only the upstream authority boundary is mocked."""

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import boto3
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from moto import mock_aws

from src.agentauth.artifact_keys import artifact_prefix
from src.agentauth.artifact_service import MAX_ARTIFACT_BYTES, artifact_storage, router
from src.agentauth.execution import ExecutionStateError
from src.agentauth.routes import get_agent_runtime, require_agent_transport
from tests.agentauth.test_run_services import GRANT, HEADERS, RECORD

URL = "/internal/v1/agent/self/artifacts/"


@pytest.fixture
async def artifacts():
    with mock_aws():
        storage = boto3.client("s3", region_name="us-east-1")
        for bucket in ("run-logs", "run-fallback"):
            storage.create_bucket(Bucket=bucket)
        runtime = SimpleNamespace(
            env={"AGENT_RUN_LOGS_BUCKET": "run-logs", "AGENT_FALLBACK_BUCKET": "run-fallback"},
            authenticate=Mock(return_value=(SimpleNamespace(uid="pod-one"), "caller", RECORD, GRANT)),
            validate_flow=AsyncMock(),
        )
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_agent_runtime] = lambda: runtime
        app.dependency_overrides[require_agent_transport] = lambda: None
        app.dependency_overrides[artifact_storage] = lambda: storage
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.test") as client:
            yield client, runtime, storage


@pytest.mark.parametrize(
    "kind,bucket,content_type",
    [
        ("transcript", "run-logs", "text/markdown"),
        ("spill", "run-logs", "text/plain"),
        ("comment", "run-fallback", "text/markdown"),
        ("git-changes", "run-fallback", "application/gzip"),
        ("git-manifest", "run-fallback", "text/markdown"),
    ],
)
async def test_content_is_archived_under_server_derived_run_prefix(artifacts, kind, bucket, content_type):
    client, runtime, storage = artifacts
    body = b"own run bytes\x00"
    headers = {**HEADERS, "x-amz-acl": "public-read", "x-amz-meta-tenant": "victim", "content-type": "text/html"}
    first = await client.post(URL + kind, content=body, headers=headers)
    second = await client.post(URL + kind, content=body, headers=headers)
    assert first.status_code == 200 and first.json() == second.json()
    receipt = first.json()
    assert receipt["key"].startswith(artifact_prefix(RECORD) + kind + "/")
    assert receipt["sha256"] == hashlib.sha256(body).hexdigest()
    assert receipt["uri"] == f"s3://{bucket}/{receipt['key']}"
    obj = storage.get_object(Bucket=bucket, Key=receipt["key"])
    assert obj["Body"].read() == body and obj["ContentType"] == content_type and obj["Metadata"] == {}
    assert len(storage.list_objects_v2(Bucket=bucket)["Contents"]) == 1
    assert first.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["transcript?key=victim", "transcript?bucket=victim", "../victim", "credentials", "transcript/other"])
async def test_no_destination_selection(artifacts, path):
    client, _, storage = artifacts
    assert (await client.post(URL + path, content=b"x", headers=HEADERS)).status_code == 404
    assert storage.list_objects_v2(Bucket="run-logs")["KeyCount"] == 0


@pytest.mark.parametrize("missing", list(HEADERS))
async def test_both_proofs_required(artifacts, missing):
    client, runtime, _ = artifacts
    response = await client.post(URL + "transcript", content=b"x", headers={k: v for k, v in HEADERS.items() if k != missing})
    assert response.status_code == 404
    runtime.authenticate.assert_not_called()


@pytest.mark.parametrize("size,expected", [(0, 422), (MAX_ARTIFACT_BYTES, 200), (MAX_ARTIFACT_BYTES + 1, 413)])
async def test_upload_size_is_bounded(artifacts, size, expected):
    client, _, storage = artifacts
    response = await client.post(URL + "spill", content=b"x" * size, headers=HEADERS)
    assert response.status_code == expected
    assert storage.list_objects_v2(Bucket="run-logs")["KeyCount"] == (1 if expected == 200 else 0)


async def test_revocation_while_uploading_refuses_before_s3(artifacts):
    client, runtime, storage = artifacts

    async def chunks():
        yield b"first"
        runtime.authenticate.side_effect = ExecutionStateError("revoked")
        yield b"last"

    assert (await client.post(URL + "transcript", content=chunks(), headers=HEADERS)).status_code == 404
    assert storage.list_objects_v2(Bucket="run-logs")["KeyCount"] == 0


async def test_changed_identity_during_upload_cannot_select_new_destination(artifacts):
    client, runtime, storage = artifacts
    old = runtime.authenticate.return_value
    newer = (*old[:2], replace(RECORD, current_attempt=2), GRANT)
    runtime.authenticate.side_effect = [old, old, newer, newer]
    assert (await client.post(URL + "transcript", content=b"x", headers=HEADERS)).status_code == 404
    assert storage.list_objects_v2(Bucket="run-logs")["KeyCount"] == 0


async def test_expiry_during_s3_does_not_release_receipt(artifacts):
    client, runtime, storage = artifacts
    original = storage.put_object

    def write(**kwargs):
        result = original(**kwargs)
        runtime.authenticate.side_effect = ExecutionStateError("expired")
        return result

    storage.put_object = write
    assert (await client.post(URL + "transcript", content=b"x", headers=HEADERS)).status_code == 404
    # The completed effect is confined to the original run, with no receipt released.
    assert storage.list_objects_v2(Bucket="run-logs")["Contents"][0]["Key"].startswith(artifact_prefix(RECORD))


def test_tenant_run_and_attempt_each_have_disjoint_namespaces():
    records = [RECORD, replace(RECORD, tenant_id="other"), replace(RECORD, invocation_id="other"), replace(RECORD, current_attempt=2)]
    assert len({artifact_prefix(record) for record in records}) == 4


async def test_missing_config_and_storage_errors_are_redacted(artifacts):
    from botocore.exceptions import ClientError

    client, runtime, storage = artifacts
    storage.put_object = Mock(side_effect=ClientError({"Error": {"Code": "Denied", "Message": "secret credential"}}, "PutObject"))
    response = await client.post(URL + "comment", content=b"x", headers=HEADERS)
    assert response.status_code == 503 and "secret" not in response.text
    runtime.env.clear()
    assert (await client.post(URL + "transcript", content=b"x", headers=HEADERS)).status_code == 503
