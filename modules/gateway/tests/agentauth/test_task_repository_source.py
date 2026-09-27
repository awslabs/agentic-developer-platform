"""Task source uses tenant ownership and immutable provider identity, no EXEC grant."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.admin.installations import resolver
from src.agentauth import task_repository_source as source
from src.agentauth.github_operations import OperationRefusedError
from tests.agentauth.test_task_repository_policy import BINDING


@pytest.fixture
def scope(monkeypatch):
    owner = AsyncMock(return_value=(SimpleNamespace(tenant_id="tenant"), resolver.OwnerState.RESOLVED))
    token = AsyncMock(return_value="gateway-only-token")
    monkeypatch.setattr(resolver, "resolve_installation_owner", owner)
    monkeypatch.setattr(source, "installation_token", token)
    return SimpleNamespace(owner=owner, token=token, authorize=AsyncMock(), requests=[])


async def fetch(scope, *, repository_id=456, head="a" * 40):
    def handle(request):
        scope.requests.append(request.url.path)
        if request.url.path == "/repos/org/repo":
            return httpx.Response(200, json={"id": repository_id})
        if request.url.path == "/repos/org/repo/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": head}})
        if request.url.path == "/repos/org/repo/tarball/" + "a" * 40:
            if hasattr(scope, "redirect"):
                return httpx.Response(302, headers={"location": scope.redirect})
            return httpx.Response(200, content=getattr(scope, "archive", b"verified archive transport fixture"))
        raise AssertionError("unexpected provider call")

    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handle)) as client:
        return await source.fetch_task_source(
            db=None,
            tenant="tenant",
            frozen={"alias": "application", "binding": BINDING},
            reauthorize=scope.authorize,
            provider_client=client,
        )


@pytest.mark.asyncio
async def test_task_source_fetch_uses_scoped_read_token_and_no_legacy_assignment(scope):
    result = await fetch(scope)
    assert result.content == b"verified archive transport fixture"
    assert result.commit_sha == "a" * 40
    scope.token.assert_awaited_once_with(
        org_id="tenant", installation_id=123, repository="org/repo", permissions={"contents": "read", "metadata": "read"}
    )
    assert len(scope.requests) == 3
    assert scope.authorize.await_count >= 5


@pytest.mark.asyncio
async def test_foreign_installation_cannot_mint_a_token(scope):
    scope.owner.return_value = (SimpleNamespace(tenant_id="other"), resolver.OwnerState.RESOLVED)
    with pytest.raises(OperationRefusedError):
        await fetch(scope)
    scope.token.assert_not_awaited()
    assert scope.requests == []


@pytest.mark.asyncio
async def test_repository_name_reuse_does_not_grant_read_access(scope):
    with pytest.raises(OperationRefusedError):
        await fetch(scope, repository_id=999)
    assert scope.requests == ["/repos/org/repo"]


@pytest.mark.asyncio
async def test_revocation_after_transfer_does_not_return_archive(scope):
    async def authorize():
        if any("tarball" in path for path in scope.requests):
            raise OperationRefusedError("revoked")

    scope.authorize.side_effect = authorize
    with pytest.raises(OperationRefusedError, match="revoked"):
        await fetch(scope)


@pytest.mark.asyncio
async def test_malformed_provider_revision_cannot_select_an_archive(scope):
    with pytest.raises(OperationRefusedError):
        await fetch(scope, head="/" * 40)
    assert not any("tarball" in path for path in scope.requests)


@pytest.mark.asyncio
async def test_archive_larger_than_legacy_slice_is_downloaded_once(scope):
    scope.archive = b"a" * (7 * 1024 * 1024)
    result = await fetch(scope)
    assert result.content == scope.archive and result.complete
    assert sum("tarball" in path for path in scope.requests) == 1


@pytest.mark.asyncio
async def test_download_rejects_archive_above_transport_bound(scope, monkeypatch):
    from src.agentauth import github_provider

    monkeypatch.setattr(github_provider, "MAX_ARCHIVE_BYTES", 16)
    with pytest.raises(OperationRefusedError):
        await fetch(scope)


@pytest.mark.asyncio
async def test_read_only_source_adapter_cannot_issue_provider_mutation(scope):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("provider mutation reached HTTP"))) as client:
        provider = source.TaskGitHubSource(binding=BINDING, token="gateway-only", reauthorize=scope.authorize, client=client)
        with pytest.raises(OperationRefusedError, match="reads only"):
            await provider._call("POST", "/repos/org/repo/git/commits")


@pytest.mark.asyncio
async def test_gateway_archive_materializes_worker_workspace_with_provider_revision(scope, tmp_path, monkeypatch):
    import io
    import tarfile
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "modules/agent-factory/agent-worker-image"))
    from lib.codex_workspace import CodexWorkspace

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as stream:
        entry = tarfile.TarInfo("org-repo-revision/source.txt")
        content = b"provider source\n"
        entry.size = len(content)
        stream.addfile(entry, io.BytesIO(content))
    scope.archive = archive.getvalue()
    fetched = await fetch(scope)
    workspace = CodexWorkspace(tmp_path / "repository", provider="github", repository=BINDING["repository"], source_revision=fetched.commit_sha)
    state = workspace.materialize(fetched.content, archive_sha256=fetched.digest)
    assert state["sourceRevision"] == "a" * 40
    assert state["localHead"] != state["sourceRevision"]
    assert workspace.read_file("source.txt")["content"] == "provider source\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example/archive",
        "http://codeload.github.com/archive",
        "https://user@codeload.github.com/archive",
        "https://codeload.github.com:8443/archive",
    ],
)
async def test_archive_redirect_cannot_send_a_request_to_an_arbitrary_host(scope, url):
    scope.redirect = url
    with pytest.raises(OperationRefusedError, match="download host"):
        await fetch(scope)
