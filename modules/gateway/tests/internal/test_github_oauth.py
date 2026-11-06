"""Synthetic provider observations never stand in for live user consent."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal import github_oauth
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import OAuthConnectionBinding, UserCredential


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as database:
        database.add(
            Organization(
                id="org-acme", name="Fixture", aws_accounts=[], role_mappings={}, settings={}, github_installation_ids=[], cognito_client_ids=[]
            )
        )
        database.add(Department(id="dept-eng", org_id="org-acme", name="Engineering"))
        database.add(Team(id="team-eng", org_id="org-acme", department_id="dept-eng", name="Team"))
        for owner_id in ("user-alice", "user-bob"):
            database.add(User(id=owner_id, org_id="org-acme", team_id="team-eng", email=f"{owner_id}@example.invalid"))
        await database.commit()
        yield database
    await engine.dispose()


@pytest.mark.asyncio
async def test_observation_uses_only_fixed_github_read_and_releases_bounded_identity(monkeypatch):
    from src.internal import credential_routes

    seen = []

    def provider(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={"id": 213, "login": "fixture-user", "type": "User", "private_data": "not-a-tool-result"},
            headers={"X-OAuth-Scopes": "user, read:user"},
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(github_oauth.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs))
    monkeypatch.setattr(credential_routes, "_validate_proxy_url", lambda url, settings: httpx.URL(url))
    monkeypatch.setattr(github_oauth, "get_settings", lambda: object())
    observed = await github_oauth.observe_github_user("synthetic-owner-token")
    assert observed.account_id == "213" and observed.scopes == ("read:user", "user")
    assert observed.profile == {"id": 213, "login": "fixture-user", "html_url": "https://github.com/fixture-user"}
    assert len(seen) == 1 and seen[0].method == "GET" and str(seen[0].url) == github_oauth.GITHUB_USER_URL
    assert seen[0].headers["authorization"] == "Bearer synthetic-owner-token"
    assert "synthetic-owner-token" not in str(observed)
    assert "private_data" not in str(observed)


@pytest.mark.asyncio
async def test_app_observation_accepts_no_scope_header_and_paginates_installations(monkeypatch):
    from src.internal import credential_routes

    requests = []

    def provider(request):
        requests.append(request)
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 213, "login": "fixture-user", "type": "User"})
        if request.url.path == "/applications/Iv1fixture/token":
            assert request.method == "POST"
            assert request.headers["authorization"].startswith("Basic ")
            assert request.content == b'{"access_token":"synthetic-owner-token"}'
            return httpx.Response(200, json={"app": {"client_id": "Iv1fixture"}, "user": {"id": 213}})
        if request.url.path == "/user/installations":
            page = int(request.url.params["page"])
            return httpx.Response(
                200,
                json={
                    "total_count": 101,
                    "installations": [{"id": index, "app_id": 123} for index in range(1, 101)] if page == 1 else [{"id": 101, "app_id": 456}],
                },
            )
        return httpx.Response(404)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(github_oauth.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs))
    monkeypatch.setattr(credential_routes, "_validate_proxy_url", lambda url, settings: httpx.URL(url))
    monkeypatch.setattr(github_oauth, "get_settings", lambda: object())
    observed = await github_oauth.observe_github_app_user(
        "synthetic-owner-token", client_id="Iv1fixture", client_secret="synthetic-secret", app_id="123"
    )
    assert observed.scopes == ()
    assert observed.profile["login"] == "fixture-user"
    assert observed.resources == (
        {
            "kind": "github_app_user",
            "schema_version": 1,
            "app_id": 123,
            "installation_ids": list(range(1, 101)),
            "account_id": "213",
        },
    )
    assert [request.method for request in requests] == ["GET", "POST", "GET", "GET"]
    assert "synthetic-owner-token" not in str(observed)
    assert "synthetic-secret" not in str(observed)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["client", "client_reply", "missing_client_claim", "owner", "installations", "truncated"])
async def test_app_observation_rejects_unqualified_tokens(monkeypatch, invalid):
    from src.internal import credential_routes

    def provider(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 213, "login": "fixture-user", "type": "User"})
        if request.url.path.startswith("/applications/"):
            return httpx.Response(
                404 if invalid == "client" else 200,
                json={
                    "app": {} if invalid == "missing_client_claim" else {"client_id": "other-client" if invalid == "client_reply" else "Iv1fixture"},
                    "user": {"id": 214 if invalid == "owner" else 213},
                },
            )
        return httpx.Response(
            200,
            json={
                "total_count": 101 if invalid == "truncated" else 1,
                "installations": [{"id": 321, "app_id": 456 if invalid == "installations" else 123}],
            },
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(github_oauth.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs))
    monkeypatch.setattr(credential_routes, "_validate_proxy_url", lambda url, settings: httpx.URL(url))
    monkeypatch.setattr(github_oauth, "get_settings", lambda: object())
    with pytest.raises(HTTPException) as error:
        await github_oauth.observe_github_app_user("synthetic-owner-token", client_id="Iv1fixture", client_secret="synthetic-secret", app_id="123")
    assert error.value.status_code == 403
    assert error.value.detail == {"error": "github_reconnect_required"}


@pytest.mark.asyncio
async def test_app_pointer_exchanges_without_oauth_scopes(session, monkeypatch):
    from src.internal import credential_routes

    monkeypatch.setattr(credential_routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    owner = await session.get(User, "user-alice")
    credential = UserCredential(
        id="00000000-0000-4000-8000-000000000010",
        org_id="org-acme",
        user_id=owner.id,
        service="github",
        provider="agentcore-identity",
        credential_type="oauth_token",
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:github",
        scopes={"github_app_grant": {"schema_version": 1, "repository_access": True}},
        agent_consent=True,
    )
    identity = MagicMock()
    identity.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "synthetic-workload"}
    identity.get_resource_oauth2_token.return_value = {"accessToken": "synthetic-owner-token"}
    token = await credential_routes._identity_access_token(credential, owner, identity)
    assert token == "synthetic-owner-token"
    assert identity.get_resource_oauth2_token.call_args.kwargs["scopes"] == []
    identity.get_resource_oauth2_token.return_value = {"sessionStatus": "FAILED"}
    with pytest.raises(HTTPException) as error:
        await credential_routes._identity_access_token(credential, owner, identity)
    assert error.value.status_code == 502 and error.value.detail == {"error": "identity_unavailable"}


@pytest.mark.parametrize("scopes", ["", "repo", "read:user, read:user", "bad scope, read:user", "read:user,"])
def test_missing_or_ambiguous_provider_scopes_fail_closed(scopes):
    with pytest.raises(ValueError):
        github_oauth.observed_scopes(scopes)


@pytest.mark.asyncio
async def test_selected_binding_refuses_foreign_owner_and_changed_revision(session, monkeypatch):
    monkeypatch.setattr(github_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    pointers = []
    for number, owner_id in enumerate(("user-alice", "user-bob"), start=1):
        pointer = UserCredential(
            id=f"00000000-0000-4000-8000-00000000000{number}",
            org_id="org-acme",
            user_id=owner_id,
            service="github",
            provider="agentcore-identity",
            credential_type="oauth_token",
            label="AgentCore OAuth",
            secret_arn="agentcore-identity:adp-tools-dev-oauth-user:github",
            scopes={"oauth_scopes": ["read:user"]},
            agent_consent=True,
        )
        session.add(pointer)
        session.add(github_oauth.new_github_binding(pointer, github_oauth.GithubObservation(str(number), ("read:user",), {"id": number})))
        pointers.append(pointer)
    await session.commit()
    selected, binding, claim = await github_oauth.selected_github_connection(session, user_id="user-alice", tenant_id="org-acme")
    assert selected.id == pointers[0].id and binding.provider_account_id == "1" and claim["connection_id"] == pointers[0].id
    with pytest.raises(HTTPException):
        await github_oauth.selected_github_connection(session, user_id="user-bob", tenant_id="org-acme", expected=claim)
    pointers[0].agent_consent = False
    await session.commit()
    with pytest.raises(HTTPException):
        await github_oauth.selected_github_connection(session, user_id="user-alice", tenant_id="org-acme", expected=claim)
    await session.delete(pointers[0])
    await session.delete(await session.get(OAuthConnectionBinding, pointers[0].id))
    await session.commit()
    with pytest.raises(HTTPException):
        await github_oauth.selected_github_connection(session, user_id="user-alice", tenant_id="org-acme", expected=claim)
