"""Task publication maps verified local trees onto scoped provider commits."""

import base64
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.admin.installations import resolver
from src.agentauth import task_repository_publication as publication
from src.agentauth.github_operations import OperationRefusedError
from tests.agentauth.test_task_repository_policy import BINDING

TASK = "tsk_11111111-1111-4111-8111-111111111111"
SOURCE, BASE, TREE, LOCAL, REMOTE = (c * 40 for c in "abcde")
MANIFEST = {
    "schema_version": "1.0",
    "provider": "github",
    "repository_id": "456",
    "repository": "org/repo",
    "source_revision": SOURCE,
    "base_tree": BASE,
    "tree": TREE,
    "local_head": LOCAL,
    "changes": [{"path": "main.txt", "mode": "100644", "deleted": False, "content_base64": base64.b64encode(b"new").decode()}],
}


@pytest.fixture
def scope(monkeypatch):
    state = SimpleNamespace(branch=None, pull=None, commit=None, requests=[], tree=TREE, base_tree=BASE, default_head=SOURCE)
    owner = AsyncMock(return_value=(SimpleNamespace(tenant_id="tenant"), resolver.OwnerState.RESOLVED))
    token = AsyncMock(return_value="gateway-only")
    monkeypatch.setattr(resolver, "resolve_installation_owner", owner)
    monkeypatch.setattr(publication, "installation_token", token)
    state.owner, state.token, state.authorize = owner, token, AsyncMock()
    state.branch_name = "adp-task-" + TASK.removeprefix("tsk_")

    def handle(request):
        import json

        method, path = request.method, request.url.path
        data = json.loads(request.content) if request.content else None
        state.requests.append((method, path))
        if path == "/repos/org/repo":
            return httpx.Response(200, json={"id": 456})
        if path == "/repos/org/repo/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": state.default_head}})
        if path == "/repos/org/repo/git/ref/heads/" + state.branch_name:
            return httpx.Response(404 if state.branch is None else 200, json={} if state.branch is None else {"object": {"sha": state.branch}})
        if path == "/repos/org/repo/git/commits/" + SOURCE:
            return httpx.Response(200, json={"sha": SOURCE, "tree": {"sha": state.base_tree}})
        if path == "/repos/org/repo/compare/" + SOURCE + "..." + SOURCE:
            return httpx.Response(200, json={"status": "identical"})
        if path == "/repos/org/repo/git/blobs" and method == "POST":
            assert base64.b64decode(data["content"]) == b"new"
            return httpx.Response(201, json={"sha": "f" * 40})
        if path == "/repos/org/repo/git/trees" and method == "POST":
            assert data["base_tree"] == BASE
            assert data["tree"] == [{"path": "main.txt", "mode": "100644", "type": "blob", "sha": "f" * 40}]
            return httpx.Response(201, json={"sha": state.tree})
        if path == "/repos/org/repo/git/commits" and method == "POST":
            assert data["parents"] == [SOURCE] and data["tree"] == TREE
            state.commit = {"sha": REMOTE, "tree": {"sha": TREE}, "parents": [{"sha": SOURCE}], "message": data["message"]}
            return httpx.Response(201, json=state.commit)
        if path == "/repos/org/repo/git/refs" and method == "POST":
            assert data == {"ref": "refs/heads/" + state.branch_name, "sha": REMOTE}
            assert state.branch is None
            state.branch = REMOTE
            return httpx.Response(201, json={"ref": data["ref"], "object": {"sha": REMOTE}})
        if path == "/repos/org/repo/git/commits/" + REMOTE:
            return httpx.Response(200, json=state.commit)
        if path == "/repos/org/repo/pulls" and method == "GET":
            return httpx.Response(200, json=[state.pull] if state.pull else [])
        if path == "/repos/org/repo/pulls" and method == "POST":
            assert data["head"] == state.branch_name and data["base"] == "main"
            state.pull = {
                "number": 7,
                "html_url": "https://github.com/org/repo/pull/7",
                "state": "open",
                "draft": False,
                "head": {"ref": state.branch_name, "sha": REMOTE, "repo": {"id": 456}},
                "base": {"ref": "main"},
                "body": data["body"],
                "title": data["title"],
            }
            return httpx.Response(201, json=state.pull)
        if path == "/repos/org/repo/pulls/7" and method in {"GET", "PATCH"}:
            if data:
                state.pull.update(data)
            return httpx.Response(200, json=state.pull)
        raise AssertionError(f"unexpected provider operation {method} {path}")

    state.handle = handle
    return state


async def publish(scope, manifest=None):
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(scope.handle)) as client:
        return await publication.publish_task_change(
            db=None,
            tenant="tenant",
            task_id=TASK,
            frozen={"alias": "application", "binding": BINDING},
            manifest=manifest or MANIFEST,
            title="Repair application",
            body="Validation passed.",
            reauthorize=scope.authorize,
            provider_client=client,
        )


@pytest.mark.asyncio
async def test_publication_preserves_validated_tree_and_reuses_task_branch_and_pr(scope):
    first = await publish(scope)
    assert first["local_head"] == LOCAL and first["provider_head"] == REMOTE and first["tree"] == TREE
    assert first["number"] == 7 and first["source_revision"] == SOURCE
    assert first == await publish(scope)
    assert scope.requests.count(("POST", "/repos/org/repo/git/commits")) == 1
    assert scope.requests.count(("POST", "/repos/org/repo/git/refs")) == 1
    assert scope.requests.count(("POST", "/repos/org/repo/pulls")) == 1
    assert scope.authorize.await_count >= len(scope.requests)
    assert scope.token.await_args.kwargs["permissions"] == {"contents": "write", "pull_requests": "write", "metadata": "read"}


@pytest.mark.parametrize("fault", ["base_tree", "default_head", "tree"])
@pytest.mark.asyncio
async def test_tree_or_source_mismatch_cannot_publish_a_ref(scope, fault):
    setattr(scope, fault, "0" * 40)
    with pytest.raises(OperationRefusedError):
        await publish(scope)
    assert scope.branch is None and scope.pull is None
    assert not any(path.endswith("/git/commits") and method == "POST" for method, path in scope.requests)


@pytest.mark.asyncio
async def test_revocation_mid_upload_prevents_ref_and_pr(scope):
    async def authorize():
        if any(path.endswith("/git/blobs") for _, path in scope.requests):
            raise OperationRefusedError("revoked")

    scope.authorize.side_effect = authorize
    with pytest.raises(OperationRefusedError, match="revoked"):
        await publish(scope)
    assert scope.branch is None and scope.pull is None


@pytest.mark.asyncio
async def test_foreign_installation_cannot_mint_publication_token(scope):
    scope.owner.return_value = (SimpleNamespace(tenant_id="other"), resolver.OwnerState.RESOLVED)
    with pytest.raises(OperationRefusedError):
        await publish(scope)
    scope.token.assert_not_awaited()
    assert scope.requests == []


@pytest.mark.asyncio
async def test_existing_branch_with_changed_tree_is_never_overwritten(scope):
    await publish(scope)
    scope.commit["tree"]["sha"] = "0" * 40
    with pytest.raises(OperationRefusedError):
        await publish(scope)
    assert scope.requests.count(("POST", "/repos/org/repo/git/refs")) == 1
    assert not any(method == "PATCH" and "/git/refs" in path for method, path in scope.requests)


@pytest.mark.parametrize("fault", ["repository_id", "path", "workflow", "encoding", "duplicate"])
@pytest.mark.asyncio
async def test_invalid_proposal_never_reaches_token_or_provider(scope, fault):
    proposal = deepcopy(MANIFEST)
    if fault == "repository_id":
        proposal["repository_id"] = "789"
    elif fault == "path":
        proposal["changes"][0]["path"] = "../outside"
    elif fault == "workflow":
        proposal["changes"][0]["path"] = ".github/workflows/unadmitted.yml"
    elif fault == "encoding":
        proposal["changes"][0]["content_base64"] = "!"
    else:
        proposal["changes"].append(proposal["changes"][0])
    with pytest.raises((ValueError, OperationRefusedError)):
        await publish(scope, proposal)
    scope.token.assert_not_awaited()
    assert scope.requests == []


def test_task_publication_branch_coexists_with_adp_memory_ref(tmp_path):
    import subprocess

    def git(*args, input=None):
        return subprocess.check_output(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", *args],
            cwd=tmp_path,
            input=input,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

    git("init", "--bare")
    tree = git("mktree", input="")
    commit = git("commit-tree", tree, input="Fixture source")
    git("update-ref", "refs/heads/adp", commit)
    branch = publication.task_publication_branch(TASK)
    git("update-ref", "refs/heads/" + branch, commit)
    assert git("rev-parse", "refs/heads/adp") == commit
    assert git("rev-parse", "refs/heads/" + branch) == commit
