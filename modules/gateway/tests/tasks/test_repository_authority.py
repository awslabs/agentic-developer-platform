"""Repository coding cannot turn caller snapshots into unchecked authority."""

import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.tasks import errors
from src.tasks import repository_authority as repo


def snapshot():
    content = "print('old')\n"
    return {
        "schema_version": "1.0",
        "repository_id": 42,
        "repository": "owner/repo",
        "commit_sha": "a" * 40,
        "issue": 5,
        "files": [
            {
                "path": "cli/main.py",
                "blob_sha": hashlib.sha1(b"blob " + str(len(content.encode())).encode() + b"\0" + content.encode()).hexdigest(),
                "content": content,
            }
        ],
    }


SCOPES = [{"repository_id": 42, "repository": "owner/repo", "path_prefixes": ["cli"]}]


def test_snapshot_exact_repo_scope_path_and_blob():
    assert repo.validate_snapshot(snapshot(), SCOPES)["repository_id"] == 42
    for mutation in (
        lambda v: v.update(repository_id=99),
        lambda v: v["files"][0].update(path="../token"),
        lambda v: v["files"][0].update(path="infra/main.py"),
        lambda v: v["files"][0].update(content="forged"),
    ):
        value = snapshot()
        mutation(value)
        with pytest.raises(errors.TaskApiError):
            repo.validate_snapshot(value, SCOPES)


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, True])
@pytest.mark.parametrize("content_case", ["valid", "different", "missing", "invalid_base64", "wrong_encoding"])
async def test_verify_repository_ownership_and_immutable_files(monkeypatch, foreign, content_case):
    value = snapshot()
    monkeypatch.setattr(repo.github, "resolve_installation_for_repo", AsyncMock(return_value=123))
    monkeypatch.setattr(repo.github, "verify_installation_ownership", AsyncMock(return_value=True))
    monkeypatch.setattr(repo.github, "_get_global_app_credentials", lambda: ("app", "private-key-fixture"))
    mint = AsyncMock(return_value=("token-fixture", "expiry"))
    monkeypatch.setattr(repo.github, "mint_installation_token_with_expiry", mint)
    requests = []

    def respond(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/issues/5"):
            body = {"number": 5}
        elif "/git/commits/" in path:
            body = {"sha": value["commit_sha"]}
        elif "/contents/" in path:
            assert request.url.params["ref"] == value["commit_sha"]
            body = {"type": "file", "path": "cli/main.py", "sha": "0" * 40 if foreign else value["files"][0]["blob_sha"]}
            if content_case != "missing":
                content = "different bytes" if content_case == "different" else value["files"][0]["content"]
                body.update(encoding="base64", content=base64.b64encode(content.encode()).decode() + "\n")
            if content_case == "invalid_base64":
                body["content"] = "!invalid!"
            if content_case == "wrong_encoding":
                body["encoding"] = "none"
        else:
            body = {"id": 42, "full_name": "owner/repo"}
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(respond)) as client:
        if foreign or content_case != "valid":
            with pytest.raises(errors.TaskApiError):
                await repo.verify_snapshot(value, caller=SimpleNamespace(tenant_id="tenant"), db=object(), client=client)
        else:
            await repo.verify_snapshot(value, caller=SimpleNamespace(tenant_id="tenant"), db=object(), client=client)
    assert len(requests) == 4 and all(request.method == "GET" for request in requests)
    assert mint.call_args.kwargs["permissions"] == {"contents": "read", "issues": "read", "metadata": "read"}
    assert mint.call_args.kwargs["repositories"] == ["repo"]


@pytest.mark.asyncio
async def test_foreign_installation_never_mints_credentials(monkeypatch):
    monkeypatch.setattr(repo.github, "resolve_installation_for_repo", AsyncMock(return_value=123))
    monkeypatch.setattr(repo.github, "verify_installation_ownership", AsyncMock(return_value=False))
    mint = AsyncMock()
    monkeypatch.setattr(repo.github, "mint_installation_token_with_expiry", mint)
    with pytest.raises(errors.TaskApiError):
        await repo.verify_snapshot(snapshot(), caller=SimpleNamespace(tenant_id="foreign"), db=object())
    mint.assert_not_called()


@pytest.mark.asyncio
async def test_coding_exact_replay_does_not_reverify_artifact_or_repository(monkeypatch):
    from unittest.mock import Mock

    from src.agentauth.task_admission import TaskAdmission
    from src.tasks.authz import Caller
    from src.tasks.records import payload_digest

    submit = {"persona": "agent-task-codex-developer", "instructions": "bounded", "inputs": {"repository_snapshot_artifact": "art-retained"}}
    caller = Caller(tenant_id="tenant", principal_id="human:owner", scopes=frozenset({"adp-tasks/submit"}))
    policy = {"status": "active", "task_scopes": ["submit"], "allowed_personas": [submit["persona"]]}
    repository = Mock()
    repository._read_idempotency.return_value = {"request_digest": payload_digest(submit), "task_id": "tsk-existing"}
    repository.read_task.return_value = {"task_id": "tsk-existing"}
    verify = AsyncMock(side_effect=AssertionError("No repository access for identity reconciliation"))
    monkeypatch.setattr(repo, "require_coding_snapshot", verify)
    service = TaskAdmission(repository, policies=Mock(get=Mock(return_value=policy)), budget=Mock(), model_resolver=AsyncMock())
    monkeypatch.setattr(service, "receipt", lambda task, replayed: {**task, "idempotent_replay": replayed})
    result = await service.admit(caller=caller, submit=submit, idempotency_key="saved", db=None)
    assert result == {"task_id": "tsk-existing", "idempotent_replay": True}
    verify.assert_not_called()
    service.model_resolver.assert_not_called()
