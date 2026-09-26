"""Publication fences and retry behavior against actual DynamoDB and S3."""

# ruff: noqa: F811
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
import pytest
import rfc8785
from fastapi import HTTPException

from src.agentauth.task_publication_service import PERMISSIONS, TaskPublicationService
from src.agentauth.task_repository_policy import TaskValidationCheck
from src.agentauth.task_tool_routes import authorize_tool
from src.tasks.dynamo_read_store import DynamoTaskReadStore
from src.tasks.store import TaskStoreError
from tests.agentauth.test_task_repository_policy import BINDING
from tests.agentauth.test_task_repository_publication import LOCAL, MANIFEST, REMOTE, SOURCE, TREE
from tests.tasks.test_store import NOW, _bind_attempt, _request, client, store  # noqa: F401


@pytest.fixture
def publication(store, monkeypatch):
    check = TaskValidationCheck(name="unit", image="sha256:" + "a" * 64, argv=["pytest"]).model_dump()
    binding = {**BINDING, "validation_checks": [check]}
    request = _request(tool_grants=PERMISSIONS, repository_binding={"alias": "application", "binding": binding})
    store.accept(request)
    attempt = _bind_attempt(store, request.task_id, request.invocation_id)
    identity = SimpleNamespace(
        task_id=request.task_id,
        invocation_id=request.invocation_id,
        generation=1,
        runtime_attempt_id=attempt,
        tenant=request.tenant,
        canonical_principal=request.canonical_principal,
    )
    policy = {"status": "active", "allowed_personas": [request.persona], "allowed_tools": list(PERMISSIONS), "repositories": {"application": binding}}
    policies = SimpleNamespace(get=lambda **kw: policy)
    env = {"ADP_TASK_PERSONA_TOOLS": json.dumps({request.persona: list(PERMISSIONS)})}
    monkeypatch.setattr("src.agentauth.task_tool_routes.time.time", lambda: NOW.timestamp())

    def authorize(identity, tool):
        return authorize_tool(store, policies, identity, tool, env=env)

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="publication-tests")
    artifacts = DynamoTaskReadStore(store, s3_client=s3, artifact_bucket="publication-tests")
    raw = rfc8785.dumps(MANIFEST)
    digest = hashlib.sha256(raw).hexdigest()
    artifact = artifacts.put_run_artifact(attempt=identity, content=raw, content_type="application/json", digest=digest)
    evidence = [{"check": "unit", "status": "passed", "specificationDigest": hashlib.sha256(rfc8785.dumps(check)).hexdigest()}]

    def validations(**kw):
        assert kw == {"identity": identity, "commit": LOCAL, "tree": TREE}
        return evidence

    result = {
        "schema_version": "1.0",
        "task_id": identity.task_id,
        "provider": "github",
        "repository_id": "456",
        "source_revision": SOURCE,
        "local_head": LOCAL,
        "tree": TREE,
        "provider_head": REMOTE,
        "branch": "adp-task-" + identity.task_id.removeprefix("tsk_"),
        "number": 7,
        "url": "https://github.com/org/repo/pull/7",
        "state": "open",
        "draft": False,
    }
    publisher = AsyncMock(return_value=result)
    service = TaskPublicationService(
        store,
        artifacts=artifacts,
        staging=SimpleNamespace(read=lambda _: {"commit": SOURCE}),
        validations=SimpleNamespace(read=validations),
        authorize=authorize,
        publisher=publisher,
    )
    arguments = dict(artifact_id=artifact.artifact_id, digest=digest, commit=LOCAL, title="Repair", body="Validated")
    return SimpleNamespace(
        service=service,
        identity=identity,
        arguments=arguments,
        publisher=publisher,
        evidence=evidence,
        policy=policy,
        artifacts=artifacts,
        artifact=artifact,
        s3=s3,
        result=result,
    )


@pytest.mark.asyncio
async def test_claim_precedes_effect_and_confirmed_retry_does_not_publish(publication):
    s = publication

    async def publish(**kw):
        assert s.service.read(s.identity)["operation_status"] == "pending"
        await kw["reauthorize"]()
        return s.result

    s.publisher.side_effect = publish
    first = await s.service.execute(s.identity, **s.arguments)
    assert first == await s.service.execute(s.identity, **s.arguments)
    s.publisher.assert_awaited_once()
    row = s.service.read(s.identity)
    assert row["operation_status"] == "confirmed"
    assert row["result"]["local_head"] == LOCAL and row["result"]["provider_head"] == REMOTE
    assert row["result"]["tree"] == TREE


@pytest.mark.parametrize("fault", ["missing", "failed", "specification", "tree"])
@pytest.mark.asyncio
async def test_invalid_validation_never_claims_or_publishes(publication, fault):
    s = publication
    if fault == "missing":
        s.evidence.clear()
    elif fault == "failed":
        s.evidence[0]["status"] = "failed"
    elif fault == "specification":
        s.evidence[0]["specificationDigest"] = "0" * 64
    else:

        def mismatch(**kw):
            raise TaskStoreError("Validation tree differs from publication")

        s.service.validations.read = mismatch
    with pytest.raises(TaskStoreError):
        await s.service.execute(s.identity, **s.arguments)
    s.publisher.assert_not_awaited()
    assert s.service.read(s.identity) is None


@pytest.mark.asyncio
async def test_lost_provider_ack_never_replays(publication):
    s = publication
    s.publisher.side_effect = TimeoutError("response lost")
    with pytest.raises(TimeoutError):
        await s.service.execute(s.identity, **s.arguments)
    with pytest.raises(TaskStoreError, match="do not replay"):
        await s.service.execute(s.identity, **s.arguments)
    s.publisher.assert_awaited_once()
    assert s.service.read(s.identity)["operation_status"] == "pending"


@pytest.mark.parametrize("fault", ["provider_head", "url", "number", "branch", "tree"])
@pytest.mark.asyncio
async def test_malformed_provider_result_cannot_confirm(publication, fault):
    s = publication
    s.publisher.return_value = {**s.result, fault: True}
    with pytest.raises(TaskStoreError, match="outcome unknown"):
        await s.service.execute(s.identity, **s.arguments)
    assert s.service.read(s.identity)["operation_status"] == "pending"


@pytest.mark.parametrize("fault", ["revoked", "attempt", "digest", "bytes", "source"])
@pytest.mark.asyncio
async def test_invalid_authority_or_artifact_never_publishes(publication, fault):
    s = publication
    if fault == "revoked":
        s.policy["allowed_tools"] = []
    elif fault == "attempt":
        s.identity.runtime_attempt_id = "foreign"
    elif fault == "digest":
        s.arguments["digest"] = "0" * 64
    elif fault == "source":
        s.service.staging.read = lambda _: {"commit": "0" * 40}
    else:
        s.s3.put_object(Bucket=s.artifacts.bucket, Key=s.artifact.storage_key, Body=b"tampered")
    from src.tasks.read_store import TaskStoreError as ArtifactError

    with pytest.raises((TaskStoreError, ArtifactError, HTTPException)):
        await s.service.execute(s.identity, **s.arguments)
    s.publisher.assert_not_awaited()
    assert s.service.read(s.identity) is None


@pytest.mark.asyncio
async def test_revocation_after_provider_effect_prevents_confirmation(publication):
    s = publication

    async def publish(**kw):
        s.policy["allowed_tools"] = []
        return s.result

    s.publisher.side_effect = publish
    with pytest.raises(HTTPException):
        await s.service.execute(s.identity, **s.arguments)
    assert s.service.read(s.identity)["operation_status"] == "pending"


@pytest.mark.asyncio
async def test_different_intent_cannot_replace_confirmed_publication(publication):
    s = publication
    await s.service.execute(s.identity, **s.arguments)
    changed = deepcopy(s.arguments)
    changed["title"] = "Different proposal"
    with pytest.raises(TaskStoreError, match="different publication intent"):
        await s.service.execute(s.identity, **changed)
    s.publisher.assert_awaited_once()


def test_http_route_authenticates_attempt_and_publishes_with_durable_receipt(publication, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.agentauth import task_repository_publication, task_source_staging, task_tool_routes, task_validation_evidence
    from src.agentauth.routes import require_agent_transport
    from src.shared.database import get_db
    from src.tasks import routes

    s = publication
    monkeypatch.setattr(task_tool_routes, "authenticate_task_attempt", AsyncMock(return_value=s.identity))
    monkeypatch.setattr(task_tool_routes, "TaskServicePolicyStore", lambda **kw: SimpleNamespace(get=lambda **kw: s.policy))
    monkeypatch.setenv("ADP_TASK_PERSONA_TOOLS", json.dumps({s.policy["allowed_personas"][0]: list(PERMISSIONS)}))
    monkeypatch.setattr(routes, "get_store", lambda: s.artifacts)
    monkeypatch.setattr(task_source_staging, "TaskSourceStaging", lambda *a, **kw: s.service.staging)
    monkeypatch.setattr(task_validation_evidence, "TaskValidationEvidence", lambda *a, **kw: s.service.validations)
    monkeypatch.setattr(task_repository_publication, "publish_task_change", s.publisher)
    app = FastAPI()
    app.dependency_overrides[require_agent_transport] = lambda: None
    app.dependency_overrides[get_db] = lambda: None
    app.include_router(task_tool_routes.router)
    body = {
        "schema_version": "1.0",
        "attempt": {
            "run": {k: getattr(s.identity, k) for k in ("task_id", "invocation_id", "generation")},
            "runtime_attempt_id": s.identity.runtime_attempt_id,
        },
        **s.arguments,
    }
    with TestClient(app) as http:
        response = http.post("/internal/v1/agent/task/repository-publication", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["provider_head"] == REMOTE
        duplicate = http.post("/internal/v1/agent/task/repository-publication", json=body)
        assert duplicate.json() == response.json()
        s.publisher.assert_awaited_once()
        body["attempt"]["runtime_attempt_id"] = "00000000-0000-4000-8000-000000000000"
        assert http.post("/internal/v1/agent/task/repository-publication", json=body).status_code == 404
