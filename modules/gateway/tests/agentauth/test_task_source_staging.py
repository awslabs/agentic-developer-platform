"""Actual Moto S3 ranges and DynamoDB attempt-fenced source staging."""

# ruff: noqa: F811
import base64
import hashlib
import json
from types import SimpleNamespace

import boto3
import pytest
from fastapi import HTTPException

from src.agentauth.github_provider import ArchiveSlice
from src.agentauth.task_source_staging import CHUNK_BYTES, TaskSourceStaging
from src.agentauth.task_tool_routes import authorize_tool
from src.tasks.store import TaskStoreError
from tests.agentauth.test_task_repository_policy import BINDING
from tests.tasks.test_store import NOW, _bind_attempt, _request, client, store  # noqa: F401


@pytest.fixture
def staging(store, monkeypatch):
    tool = "repository.read"
    request = _request(tool_grants=(tool,), repository_binding={"alias": "application", "binding": BINDING})
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
    policy = {"status": "active", "allowed_personas": [request.persona], "allowed_tools": [tool], "repositories": {"application": dict(BINDING)}}
    policies = SimpleNamespace(get=lambda **kw: policy)
    env = {"ADP_TASK_PERSONA_TOOLS": json.dumps({request.persona: [tool]})}
    monkeypatch.setattr("src.agentauth.task_tool_routes.time.time", lambda: NOW.timestamp())
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="task-source-fixture")
    service = TaskSourceStaging(
        store, s3=s3, bucket="task-source-fixture", authorize=lambda identity, tool: authorize_tool(store, policies, identity, tool, env=env)
    )
    content = b"a" * CHUNK_BYTES + b"tail"
    archive = ArchiveSlice(commit_sha="b" * 40, total_bytes=len(content), digest=hashlib.sha256(content).hexdigest(), content=content)
    return SimpleNamespace(service=service, identity=identity, archive=archive, policy=policy, s3=s3)


def test_complete_archive_is_staged_once_and_reassembled_from_verified_ranges(staging):
    s = staging
    assert s.service.read(s.identity) is None
    row = s.service.stage(s.identity, s.archive)
    assert s.service.stage(s.identity, s.archive) == row
    chunks = [s.service.chunk(s.identity, index=index) for index in range(2)]
    content = b"".join(base64.b64decode(chunk["content_base64"]) for chunk in chunks)
    assert content == s.archive.content
    assert hashlib.sha256(content).hexdigest() == chunks[0]["archive_sha256"]
    assert all(chunk["commit"] == s.archive.commit_sha for chunk in chunks)
    assert s.s3.list_objects_v2(Bucket=s.service.bucket)["KeyCount"] == 1


def test_revoked_repository_cannot_read_staged_source(staging):
    s = staging
    s.service.stage(s.identity, s.archive)
    s.policy["repositories"] = {}
    with pytest.raises(HTTPException):
        s.service.chunk(s.identity, index=0)


def test_foreign_attempt_cannot_use_staged_source(staging):
    s = staging
    s.service.stage(s.identity, s.archive)
    s.identity.runtime_attempt_id = "foreign"
    with pytest.raises(HTTPException):
        s.service.chunk(s.identity, index=0)


def test_corrupted_staged_object_is_not_returned(staging):
    s = staging
    row = s.service.stage(s.identity, s.archive)
    s.s3.put_object(Bucket=s.service.bucket, Key=row["object_key"], Body=b"x" * len(s.archive.content))
    with pytest.raises(TaskStoreError, match="bytes differ"):
        s.service.chunk(s.identity, index=0)


def test_incomplete_provider_archive_never_creates_a_manifest(staging):
    s = staging
    incomplete = ArchiveSlice(commit_sha="b" * 40, total_bytes=100, digest=s.archive.digest, content=b"short")
    with pytest.raises(TaskStoreError):
        s.service.stage(s.identity, incomplete)
    assert s.service.read(s.identity) is None
    assert s.s3.list_objects_v2(Bucket=s.service.bucket)["KeyCount"] == 0


@pytest.mark.parametrize("corruption", [None, "chunk", "revision", "scope"])
def test_task_route_transfers_staged_archive_to_actual_worker_workspace(staging, tmp_path, monkeypatch, corruption):
    import io
    import os
    import tarfile
    from pathlib import Path
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.agentauth import task_repository_source, task_tool_routes
    from src.agentauth.routes import require_agent_transport
    from src.shared.database import get_db
    from src.tasks import routes as artifact_routes

    s = staging
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "modules/agent-factory/agent-worker-image"))
    from lib.codex_source import provision_workspace
    from lib.task_run_client import TaskRunClientError

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        for name, content in {"root/source.txt": b"authorized provider source", "root/data.bin": os.urandom(600000)}.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            tar.addfile(member, io.BytesIO(content))
    content = archive.getvalue()
    fetched = ArchiveSlice(commit_sha="b" * 40, total_bytes=len(content), digest=hashlib.sha256(content).hexdigest(), content=content)
    fetch = AsyncMock(return_value=fetched)
    monkeypatch.setattr(task_repository_source, "fetch_task_source", fetch)
    monkeypatch.setattr(task_repository_source, "authorize_source_connection", AsyncMock(return_value=123))
    monkeypatch.setattr(task_tool_routes, "authenticate_task_attempt", AsyncMock(return_value=s.identity))
    monkeypatch.setattr(task_tool_routes, "TaskServicePolicyStore", lambda **kw: SimpleNamespace(get=lambda **kw: s.policy))
    monkeypatch.setenv("ADP_TASK_PERSONA_TOOLS", json.dumps({s.policy["allowed_personas"][0]: ["repository.read"]}))
    monkeypatch.setattr(artifact_routes, "get_store", lambda: SimpleNamespace(repository=s.service.repository, s3=s.s3, bucket=s.service.bucket))
    app = FastAPI()
    app.dependency_overrides[require_agent_transport] = lambda: None
    app.dependency_overrides[get_db] = lambda: None
    app.include_router(task_tool_routes.router)
    attempt = {
        "run": {k: getattr(s.identity, k) for k in ("task_id", "invocation_id", "generation")},
        "runtime_attempt_id": s.identity.runtime_attempt_id,
    }
    with TestClient(app) as http:

        class WorkerTransport:
            def tool_authorize(self, body):
                return s.service.authorize(s.identity, "repository.read")

            def repository_source(self, body):
                response = http.post("/internal/v1/agent/task/repository-source", json=body)
                assert response.status_code == 200, response.text
                result = response.json()
                if body["index"] == 1:
                    if corruption == "chunk":
                        result["content_base64"] = "AAAA"
                    elif corruption == "revision":
                        result["commit"] = "c" * 40
                    elif corruption == "scope":
                        result["repository_binding"]["binding"]["repository"] = "other/repo"
                return result

        if corruption:
            with pytest.raises(TaskRunClientError):
                provision_workspace(WorkerTransport(), attempt=attempt, root=tmp_path / "workspace")
            assert not (tmp_path / "workspace").exists()
            fetch.assert_awaited_once()
            return
        workspace = provision_workspace(WorkerTransport(), attempt=attempt, root=tmp_path / "workspace")
    fetch.assert_awaited_once()
    assert workspace.read_file("source.txt")["content"] == "authorized provider source"
    assert workspace.state()["sourceRevision"] == "b" * 40
    assert workspace.state()["clean"]
