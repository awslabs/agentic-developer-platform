"""Repository selection must not depend on connection ordering or stored lists."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.admin.installations.resolver import OwnerState
from src.orchestration import repository_binding
from src.orchestration.repository_binding import _provider_read as real_provider_read


class Connections:
    def __init__(self, *installation_ids):
        self.rows = [SimpleNamespace(installation_id=str(installation_id)) for installation_id in installation_ids]

    async def scalars(self, _query):
        return SimpleNamespace(all=lambda: self.rows)


@pytest.fixture
def provider(monkeypatch):
    async def owner(installation_id, *, db):
        tenant_id = "tenant-a" if isinstance(db, Connections) else "org-alpha"
        return SimpleNamespace(tenant_id=tenant_id, github_account_id=str(installation_id + 100)), OwnerState.RESOLVED

    async def read(_tenant, installation_id, name, repository_id, issue):
        if installation_id not in (11, 22) or name not in ("acme/a", "acme/b"):
            return None
        expected_installation = 11 if name == "acme/a" else 22
        if installation_id != expected_installation:
            return None
        repo_id = 1001 if name == "acme/a" else 1002
        return {"id": repo_id, "full_name": name}, {"account": {"id": installation_id + 100}}, {"id": 42, "node_id": f"I_{repo_id}_{issue}"}

    monkeypatch.setattr(repository_binding, "resolve_installation_owner", owner)
    monkeypatch.setattr(repository_binding, "_provider_read", read)


@pytest.mark.asyncio
async def test_two_stories_keep_separate_installations_with_unrelated_connection(provider):
    async def check(session):
        return [
            await repository_binding.resolve_story_repository(
                session,
                tenant_id="tenant-a",
                name=name,
                issue=42,
                operation="accept",
            )
            for name in ("acme/a", "acme/b")
        ]

    baseline = await check(Connections(11, 22))
    with_third = await check(Connections(33, 22, 11))
    assert [(item.repository_id, item.installation_id, item.issue_number) for item in baseline] == [(1001, 11, 42), (1002, 22, 42)]
    assert with_third == baseline


@pytest.mark.asyncio
async def test_link_parent_id_comes_from_verified_provider_issue_readback(provider, monkeypatch):
    async def issue_response(request):
        if request.url.path == "/repositories/1001":
            return httpx.Response(200, json={"id": 1001, "full_name": "acme/a"})
        if request.url.path == "/repos/acme/a/issues/42":
            return httpx.Response(
                200,
                json={
                    "id": 32042,
                    "node_id": "I_ACME_PARENT",
                    "number": 42,
                    "repository_url": "https://api.github.com/repos/acme/a",
                    "html_url": "https://github.com/acme/a/issues/42",
                },
            )
        raise AssertionError(f"unexpected read: {request.url.path}")

    async def mint(*_args, **_kwargs):
        return "fixture-token", None

    async def installation(_installation_id):
        return {"account": {"id": 111}}

    calls = []
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(issue_response)

    def client(**kwargs):
        calls.append(kwargs)
        return original_client(transport=transport, **kwargs)

    monkeypatch.setattr(repository_binding, "_provider_read", real_provider_read)
    monkeypatch.setattr(repository_binding, "resolve_tenant_app_credentials", AsyncMock(return_value=(123, "fixture-key")))
    monkeypatch.setattr(repository_binding, "mint_installation_token_with_expiry", mint)
    monkeypatch.setattr(repository_binding, "GitHubAppClient", lambda *_args, **_kwargs: SimpleNamespace(get_installation=installation))
    monkeypatch.setattr(httpx, "AsyncClient", client)
    parent = await repository_binding.resolve_story_repository(
        Connections(11),
        tenant_id="tenant-a",
        name="acme/a",
        repository_id=1001,
        installation_id=11,
        issue=42,
        operation="link",
    )
    assert (parent.repository_id, parent.installation_id, parent.issue_number) == (1001, 11, 42)
    assert (parent.issue_id, parent.issue_node_id) == (32042, "I_ACME_PARENT")
    assert len(calls) == 1
    assert calls[0]["trust_env"] is False


@pytest.mark.asyncio
async def test_evaluation_harness_connection_verifies_repository_without_an_issue(provider, monkeypatch):
    async def response(request):
        if request.url.path == "/repositories/1001":
            return httpx.Response(200, json={"id": 1001, "full_name": "acme/a"})
        raise AssertionError(f"unexpected issue read: {request.url.path}")

    monkeypatch.setattr(repository_binding, "_provider_read", real_provider_read)
    monkeypatch.setattr(repository_binding, "resolve_tenant_app_credentials", AsyncMock(return_value=(123, "fixture-key")))
    monkeypatch.setattr(repository_binding, "mint_installation_token_with_expiry", AsyncMock(return_value=("fixture-token", None)))
    monkeypatch.setattr(
        repository_binding,
        "GitHubAppClient",
        lambda *_args, **_kwargs: SimpleNamespace(get_installation=AsyncMock(return_value={"account": {"id": 111}})),
    )
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(response), **kwargs))
    harness = await repository_binding.resolve_story_repository(
        Connections(11),
        tenant_id="tenant-a",
        name="acme/a",
        repository_id=1001,
        installation_id=11,
        issue=None,
        operation="evaluate",
    )
    assert (harness.repository_id, harness.installation_id, harness.issue_number) == (1001, 11, None)
    with pytest.raises(repository_binding.RepositoryBindingError, match="repository_invalid"):
        await repository_binding.resolve_story_repository(
            Connections(11),
            tenant_id="tenant-a",
            name="acme/a",
            repository_id=1001,
            installation_id=11,
            issue=None,
            operation="dispatch",
        )


@pytest.mark.asyncio
async def test_selector_cannot_choose_an_unrelated_connection(provider):
    with pytest.raises(repository_binding.RepositoryBindingError) as caught:
        await repository_binding.resolve_story_repository(
            Connections(11, 22),
            tenant_id="tenant-a",
            name="acme/a",
            issue=42,
            installation_id=22,
            operation="accept",
        )
    assert caught.value.code == "repository_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "tenant", "expected"),
    [
        (OwnerState.REVOKED, "tenant-a", "connection_revoked"),
        (OwnerState.NOT_FOUND, "tenant-a", "connection_not_owned"),
        (OwnerState.AMBIGUOUS, "tenant-a", "connection_not_owned"),
        (OwnerState.UNATTESTABLE, "tenant-a", "connection_not_owned"),
        (OwnerState.RESOLVED, "tenant-b", "connection_not_owned"),
    ],
)
async def test_selected_connection_refuses_unverified_ownership(monkeypatch, state, tenant, expected):
    async def owner(installation_id, *, db):
        return SimpleNamespace(tenant_id=tenant, github_account_id="111"), state

    async def forbidden_read(*args):
        pytest.fail("provider must not be queried for an unowned or revoked installation")

    monkeypatch.setattr(repository_binding, "resolve_installation_owner", owner)
    monkeypatch.setattr(repository_binding, "_provider_read", forbidden_read)
    with pytest.raises(repository_binding.RepositoryBindingError) as caught:
        await repository_binding.resolve_story_repository(
            Connections(11),
            tenant_id="tenant-a",
            name="acme/a",
            issue=42,
            installation_id=11,
            operation="dispatch",
        )
    assert caught.value.code == expected


@pytest.mark.asyncio
async def test_no_connection_and_foreign_selector_expose_no_inventory():
    for connections, selector, expected in ((Connections(), None, "repository_unavailable"), (Connections(11), 99, "connection_not_owned")):
        with pytest.raises(repository_binding.RepositoryBindingError) as caught:
            await repository_binding.resolve_story_repository(
                connections,
                tenant_id="tenant-a",
                name="acme/a",
                issue=42,
                installation_id=selector,
                operation="accept",
            )
        assert caught.value.code == expected
        assert "acme/a" not in str(caught.value)


@pytest.mark.asyncio
async def test_two_usable_connections_require_reviewed_selection(monkeypatch, provider):
    async def read(_tenant, installation_id, name, repository_id, issue):
        return {"id": 1001, "full_name": name}, {"account": {"id": installation_id + 100}}, {"id": 42, "node_id": f"I_1001_{issue}"}

    monkeypatch.setattr(repository_binding, "_provider_read", read)
    with pytest.raises(repository_binding.RepositoryBindingError) as caught:
        await repository_binding.resolve_story_repository(
            Connections(11, 22),
            tenant_id="tenant-a",
            name="acme/a",
            issue=42,
            operation="accept",
        )
    assert caught.value.code == "repository_connection_ambiguous"
    selected = await repository_binding.resolve_story_repository(
        Connections(11, 22),
        tenant_id="tenant-a",
        name="acme/a",
        issue=42,
        operation="accept",
        installation_id=22,
    )
    assert selected.installation_id == 22


@pytest.mark.asyncio
async def test_policy_disallows_repository_before_any_binding_is_returned(provider):
    with pytest.raises(repository_binding.RepositoryBindingError) as caught:
        await repository_binding.resolve_story_repository(
            Connections(11),
            tenant_id="tenant-a",
            name="acme/a",
            issue=42,
            operation="accept",
            permitted_names=("acme/other",),
        )
    assert caught.value.code == "repository_not_permitted"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("credentials unavailable"), TimeoutError("provider timeout")])
async def test_provider_failure_is_a_bounded_typed_refusal(monkeypatch, provider, error):
    calls = 0

    async def failing_read(*args):
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(repository_binding, "_provider_read", failing_read)
    with pytest.raises(repository_binding.RepositoryBindingError) as caught:
        await repository_binding.resolve_story_repository(
            Connections(11),
            tenant_id="tenant-a",
            name="acme/a",
            issue=42,
            operation="dispatch",
        )
    assert caught.value.code == "repository_verification_unavailable"
    assert calls == 1


@pytest.mark.asyncio
async def test_connection_limit_fails_closed_without_provider_polling(monkeypatch, provider):
    async def forbidden_read(*args):
        pytest.fail("bounded candidate verification must not query provider")

    monkeypatch.setattr(repository_binding, "_provider_read", forbidden_read)
    with pytest.raises(repository_binding.RepositoryBindingError) as caught:
        await repository_binding.resolve_story_repository(
            Connections(*range(1, 34)),
            tenant_id="tenant-a",
            name="acme/a",
            issue=42,
            operation="accept",
        )
    assert caught.value.code == "repository_verification_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [(404, "repository_unavailable"), (403, "repository_verification_unavailable"), (429, "repository_verification_unavailable")],
)
async def test_authenticated_provider_response_refuses_without_unscoped_retry(monkeypatch, status, expected):
    import httpx

    async def owner(installation_id, *, db):
        return SimpleNamespace(tenant_id="tenant-a", github_account_id="111"), OwnerState.RESOLVED

    monkeypatch.setattr(repository_binding, "resolve_installation_owner", owner)

    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(status, json={"message": "unavailable"})

    transport = httpx.MockTransport(respond)
    client_type = httpx.AsyncClient

    def client(*args, **kwargs):
        return client_type(*args, transport=transport, **kwargs)

    async def credentials(_tenant):
        return "fictional-app", "fictional-private-key"

    async def token(*args, **kwargs):
        assert kwargs["repositories"] == ["a"]
        assert kwargs["repository_ids"] is None
        assert kwargs["permissions"] == {"metadata": "read", "issues": "read"}
        return "synthetic-token", "never-used"

    monkeypatch.setattr(repository_binding.httpx, "AsyncClient", client)
    monkeypatch.setattr(repository_binding, "resolve_tenant_app_credentials", credentials)
    monkeypatch.setattr(repository_binding, "mint_installation_token_with_expiry", token)
    with pytest.raises(repository_binding.RepositoryBindingError) as caught:
        await repository_binding.resolve_story_repository(
            Connections(11),
            tenant_id="tenant-a",
            name="acme/a",
            issue=42,
            installation_id=11,
            operation="accept",
        )
    assert caught.value.code == expected
    assert calls == ["https://api.github.com/repos/acme/a"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("id_mismatch", "repository_identity_mismatch"),
        ("reused_name", "repository_identity_mismatch"),
        ("wrong_issue_repository", "repository_identity_mismatch"),
        ("wrong_issue_number", "repository_identity_mismatch"),
        ("wrong_issue_html", "repository_identity_mismatch"),
        ("issue_is_pr", "repository_identity_mismatch"),
        ("issue_missing", "repository_identity_mismatch"),
        ("rename", None),
    ],
)
async def test_authenticated_repository_and_issue_identity(monkeypatch, case, expected):
    import httpx

    from src.admin.connections.github_client import GitHubAppClient

    calls = []
    repo_name = "acme/renamed" if case in {"rename", "reused_name"} else "acme/a"

    def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/repositories/1001":
            return httpx.Response(200, json={"id": 2002 if case == "id_mismatch" else 1001, "full_name": repo_name})
        if request.url.path == "/repos/acme/a":
            return httpx.Response(200 if case == "reused_name" else 404, json={"id": 2002, "full_name": "acme/a"})
        if request.url.path == f"/repos/{repo_name}/issues/42":
            if case == "issue_missing":
                return httpx.Response(404)
            issue_repo = "acme/other" if case == "wrong_issue_repository" else repo_name
            issue_metadata = {
                "id": 999,
                "node_id": "I_999",
                "number": 43 if case == "wrong_issue_number" else 42,
                "repository_url": f"https://api.github.com/repos/{issue_repo}",
                "html_url": f"https://github.com/{'acme/other' if case == 'wrong_issue_html' else issue_repo}/issues/42",
            }
            if case == "issue_is_pr":
                issue_metadata["pull_request"] = {"url": "https://api.github.com/repos/acme/a/pulls/42"}
            return httpx.Response(200, json=issue_metadata)
        pytest.fail(f"unexpected provider request {request.url.path}")

    transport = httpx.MockTransport(respond)
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        return original_client(*args, transport=transport, **kwargs)

    async def owner(_installation_id, *, db):
        return SimpleNamespace(tenant_id="tenant-a", github_account_id="111"), OwnerState.RESOLVED

    async def credentials(_tenant):
        return "fictional-app", "fictional-private-key"

    async def token(*args, **kwargs):
        assert kwargs.get("repository_ids") == [1001] or kwargs.get("repositories") == ["a"]
        assert kwargs.get("permissions") in ({"metadata": "read", "issues": "read"}, {"metadata": "read"})
        return "synthetic-token", "never-used"

    async def installation(_self, _installation_id):
        return {"account": {"id": 111}}

    monkeypatch.setattr(repository_binding, "resolve_installation_owner", owner)
    monkeypatch.setattr(repository_binding, "resolve_tenant_app_credentials", credentials)
    monkeypatch.setattr(repository_binding, "mint_installation_token_with_expiry", token)
    monkeypatch.setattr(repository_binding.httpx, "AsyncClient", client)
    monkeypatch.setattr(GitHubAppClient, "get_installation", installation)
    if expected:
        with pytest.raises(repository_binding.RepositoryBindingError) as caught:
            await repository_binding.resolve_story_repository(
                Connections(11),
                tenant_id="tenant-a",
                name="acme/a",
                issue=42,
                repository_id=1001,
                installation_id=11,
                operation="dispatch",
            )
        assert caught.value.code == expected
    else:
        verified = await repository_binding.resolve_story_repository(
            Connections(11),
            tenant_id="tenant-a",
            name="acme/a",
            issue=42,
            repository_id=1001,
            installation_id=11,
            operation="dispatch",
        )
        assert (verified.accepted_name, verified.full_name, verified.repository_id) == ("acme/a", "acme/renamed", 1001)
    assert calls[0] == "/repositories/1001"
