"""Isolated private GitHub target tests with synthetic provider observations."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal import tools_github
from src.internal.github_oauth import GithubObservation
from src.shared.models.base import Base


@pytest.fixture
async def receipts(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tools_github, "get_session_factory", lambda: factory)
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_private_mediator_uses_exact_connection_and_refuses_replay_or_provider_drift(receipts, monkeypatch):
    claim = {
        "principal": {"user_id": "user-alice", "tenant_id": "org-acme", "team_id": "team-eng"},
        "github_connection": {"connection_id": "owned-id"},
        "connection_id": "owned-id",
        "receipt_id": "receipt-1",
        "expires_at": 3000000000,
    }
    verified = AsyncMock(return_value=claim)
    monkeypatch.setattr(tools_github, "verify_facade", verified)
    monkeypatch.setattr(tools_github, "check_fresh", lambda claims: None)
    monkeypatch.setattr(tools_github, "current_binding", lambda **kwargs: {"target_status": "READY", "target_id": "target-1"})
    credential = SimpleNamespace(id="owned-id", user_id="user-alice", org_id="org-acme")
    binding = SimpleNamespace(provider_account_id="213", observed_scopes=["read:user"])
    owner = SimpleNamespace(id="user-alice", org_id="org-acme")
    authorities = []

    @asynccontextmanager
    async def current_authority(claims, runtime):
        authorities.append((claims["principal"]["user_id"], claims["connection_id"]))
        yield credential, binding, owner

    monkeypatch.setattr(tools_github, "current_authority", current_authority)
    monkeypatch.setattr(tools_github, "get_identity_client", Mock(return_value=object()))
    exchange = AsyncMock(return_value="synthetic-private-token")
    monkeypatch.setattr(tools_github, "_identity_access_token", exchange)
    observe = AsyncMock(return_value=GithubObservation("213", ("read:user",), {"id": 213, "login": "fixture-user"}))
    monkeypatch.setattr(tools_github, "observe_github_user", observe)
    packet = {"method": "tools/call", "id": 17, "params": {"name": "get_user", "arguments": {"adp_assertion": "signed-assertion"}}}
    result = await tools_github.serve(packet, SimpleNamespace())
    assert result.status_code == 200 and "synthetic-private-token" not in result.body.decode()
    assert "fixture-user" in result.body.decode()
    assert authorities == [("user-alice", "owned-id")] * 3
    assert exchange.await_count == observe.await_count == 1
    verified.assert_awaited_once_with("signed-assertion", "get_user", {}, 17, namespace="github")
    with pytest.raises(HTTPException) as replay:
        await tools_github.serve(packet, SimpleNamespace())
    assert replay.value.status_code == 409
    assert exchange.await_count == observe.await_count == 1
    claim["receipt_id"] = "receipt-2"
    observe.return_value = GithubObservation("214", ("read:user",), {"id": 214})
    with pytest.raises(HTTPException) as changed:
        await tools_github.serve(packet, SimpleNamespace())
    assert changed.value.status_code == 403
    assert "identity changed" in changed.value.detail
    claim["receipt_id"] = "receipt-3"
    observe.return_value = GithubObservation("213", ("read:user",), {"id": 999, "login": "platform-app"})
    with pytest.raises(HTTPException) as mismatched_profile:
        await tools_github.serve(packet, SimpleNamespace())
    assert mismatched_profile.value.status_code == 403
    assert "identity changed" in mismatched_profile.value.detail
    assert exchange.await_count == observe.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["legacy", "app"])
async def test_get_user_accepts_legacy_and_app_bindings_without_returning_provider_token(receipts, monkeypatch, grant):
    from src.internal import github_oauth
    from src.shared.models.vault import UserCredential

    resources = ({"kind": "github_app_user", "schema_version": 1, "app_id": 123, "installation_ids": [42], "account_id": "213"},)
    observation = (
        GithubObservation("213", (), {"id": 213, "login": "fixture-owner"}, resources)
        if grant == "app"
        else GithubObservation("213", ("read:user", "repo", "workflow"), {"id": 213, "login": "fixture-owner"})
    )
    credential = UserCredential(
        id="00000000-0000-4000-8000-000000000001",
        org_id="org-acme",
        user_id="user-alice",
        service="github",
        provider="agentcore-identity",
        credential_type="oauth_token",
        label="AgentCore OAuth",
        agent_consent=True,
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:github",
        scopes={"github_app_grant": {"schema_version": 1, "repository_access": True}}
        if grant == "app"
        else {"oauth_scopes": ["read:user", "repo", "workflow"]},
    )
    binding = github_oauth.new_github_binding(credential, observation)
    factory = tools_github.get_session_factory()
    async with factory() as database:
        database.add(credential)
        database.add(binding)
        await database.commit()
    claim = {
        "principal": {"user_id": "user-alice", "tenant_id": "org-acme"},
        "connection_id": credential.id,
        "github_connection": github_oauth.binding_claim(binding),
        "receipt_id": "get-user-" + grant,
        "expires_at": 3000000000,
    }
    monkeypatch.setattr(github_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user", github_app_id="123"))
    monkeypatch.setattr(tools_github, "verify_facade", AsyncMock(return_value=claim))
    monkeypatch.setattr(tools_github, "check_policy", AsyncMock())
    monkeypatch.setattr(tools_github, "check_fresh", lambda claims: None)
    monkeypatch.setattr(tools_github, "current_tools_human", AsyncMock(return_value=SimpleNamespace(id="user-alice", org_id="org-acme")))
    monkeypatch.setattr(tools_github, "current_binding", lambda **kwargs: {"target_status": "READY"})
    monkeypatch.setattr(tools_github, "get_identity_client", Mock(return_value=object()))
    exchange = AsyncMock(return_value="synthetic-private-token")
    legacy_observe = AsyncMock(return_value=observation)
    app_observe = AsyncMock(return_value=observation)
    monkeypatch.setattr(tools_github, "_identity_access_token", exchange)
    monkeypatch.setattr(tools_github, "observe_github_user", legacy_observe)
    monkeypatch.setattr(github_oauth, "observe_github_app_user", app_observe)
    monkeypatch.setattr(
        github_oauth,
        "github_app_client",
        AsyncMock(
            return_value={
                "client_id": "Iv1fixture",
                "client_secret": "synthetic-secret",
                "app_id": "123",
            }
        ),
    )
    packet = {"method": "tools/call", "id": 17, "params": {"name": "get_user", "arguments": {"adp_assertion": "signed"}}}
    response = await tools_github.serve(packet, SimpleNamespace())
    assert response.status_code == 200
    assert json.loads(json.loads(response.body)["result"]["content"][0]["text"])["login"] == "fixture-owner"
    assert "synthetic-private-token" not in response.body.decode()
    exchange.assert_awaited_once()
    if grant == "app":
        app_observe.assert_awaited_once_with("synthetic-private-token", client_id="Iv1fixture", client_secret="synthetic-secret", app_id="123")
        legacy_observe.assert_not_awaited()
    else:
        legacy_observe.assert_awaited_once_with("synthetic-private-token")
        app_observe.assert_not_awaited()


def test_private_route_rejects_unverified_transport_without_calling_identity(monkeypatch):
    exchange = AsyncMock()
    monkeypatch.setattr(tools_github, "_identity_access_token", exchange)
    app = FastAPI()
    app.include_router(tools_github.router)
    response = TestClient(app).post(tools_github.PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response.status_code == 403
    exchange.assert_not_awaited()


@pytest.mark.asyncio
async def test_mediator_requires_signed_connection_before_claiming_receipt(monkeypatch):
    monkeypatch.setattr(tools_github, "current_binding", lambda **kwargs: {"target_status": "READY"})
    monkeypatch.setattr(tools_github, "verify_facade", AsyncMock(return_value={"connection_id": "owned-id", "github_connection": {}}))
    receipt = AsyncMock()
    monkeypatch.setattr(tools_github, "claim_receipt", receipt)
    packet = {"method": "tools/call", "id": 5, "params": {"name": "get_user", "arguments": {"adp_assertion": "signed-assertion"}}}
    with pytest.raises(HTTPException) as refused:
        await tools_github.serve(packet, SimpleNamespace())
    assert refused.value.status_code == 403
    receipt.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnection_after_token_retrieval_prevents_provider_call(monkeypatch):
    claims = {
        "principal": {"user_id": "user-alice", "tenant_id": "org-acme", "team_id": "team-eng"},
        "github_connection": {"connection_id": "owned-id"},
        "connection_id": "owned-id",
        "receipt_id": "receipt-1",
        "expires_at": 3000000000,
    }
    monkeypatch.setattr(tools_github, "verify_facade", AsyncMock(return_value=claims))
    monkeypatch.setattr(tools_github, "check_fresh", lambda value: None)
    monkeypatch.setattr(tools_github, "current_binding", lambda **kwargs: {"target_status": "READY"})
    monkeypatch.setattr(tools_github, "claim_receipt", AsyncMock())
    monkeypatch.setattr(tools_github, "_identity_access_token", AsyncMock(return_value="private-owner-token"))
    monkeypatch.setattr(tools_github, "get_identity_client", Mock(return_value=object()))
    provider = AsyncMock()
    monkeypatch.setattr(tools_github, "observe_github_user", provider)
    checks = []

    @asynccontextmanager
    async def authority(value, runtime):
        checks.append(value["connection_id"])
        if len(checks) != 1:
            raise HTTPException(403, "GitHub connection changed")
        yield SimpleNamespace(), SimpleNamespace(), SimpleNamespace()

    monkeypatch.setattr(tools_github, "current_authority", authority)
    packet = {"method": "tools/call", "id": 17, "params": {"name": "get_user", "arguments": {"adp_assertion": "signed"}}}
    with pytest.raises(HTTPException) as refused:
        await tools_github.serve(packet, SimpleNamespace())
    assert refused.value.status_code == 403
    assert checks == ["owned-id", "owned-id"]
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_owners_receive_distinct_broker_tokens_and_provider_profiles(receipts, monkeypatch):
    owners = {
        "alice": ("connection-alice", "101", "token-alice"),
        "bob": ("connection-bob", "202", "token-bob"),
    }
    calls = {owner: 0 for owner in owners}
    claims = {}
    monkeypatch.setattr(tools_github, "verify_facade", AsyncMock(side_effect=lambda *args, **kwargs: claims.copy()))
    monkeypatch.setattr(tools_github, "check_fresh", lambda value: None)
    monkeypatch.setattr(tools_github, "current_binding", lambda **kwargs: {"target_status": "READY", "target_id": "target-1"})
    monkeypatch.setattr(tools_github, "get_identity_client", Mock(return_value=object()))

    @asynccontextmanager
    async def authority(value, runtime):
        principal = value["principal"]["user_id"]
        connection_id, account_id, _ = owners[principal]
        if value["connection_id"] != connection_id or value["github_connection"]["connection_id"] != connection_id:
            raise HTTPException(403, "wrong connection")
        yield (
            SimpleNamespace(id=connection_id),
            SimpleNamespace(provider_account_id=account_id, observed_scopes=["read:user"]),
            SimpleNamespace(id=principal),
        )

    async def exchange(credential, user, identity_client):
        if credential.id != owners[user.id][0]:
            raise HTTPException(403, "wrong owner")
        return owners[user.id][2]

    async def provider(token):
        for owner, (_, account_id, selected_token) in owners.items():
            if token == selected_token:
                calls[owner] += 1
                return GithubObservation(account_id, ("read:user",), {"id": int(account_id), "login": owner})
        raise AssertionError("service or fallback credential used")

    monkeypatch.setattr(tools_github, "current_authority", authority)
    broker = AsyncMock(side_effect=exchange)
    monkeypatch.setattr(tools_github, "_identity_access_token", broker)
    monkeypatch.setattr(tools_github, "observe_github_user", AsyncMock(side_effect=provider))
    packet = {"method": "tools/call", "id": 1, "params": {"name": "get_user", "arguments": {"adp_assertion": "signed"}}}
    for owner, (connection_id, account_id, _) in owners.items():
        claims.clear()
        claims.update(
            {
                "principal": {"user_id": owner},
                "connection_id": connection_id,
                "github_connection": {"connection_id": connection_id},
                "receipt_id": "receipt-" + owner,
                "expires_at": 3000000000,
            }
        )
        result = await tools_github.serve(packet, SimpleNamespace())
        assert result.status_code == 200
        assert json.loads(json.loads(result.body)["result"]["content"][0]["text"])["id"] == int(account_id)
        assert "token-" not in result.body.decode()
    assert calls == {"alice": 1, "bob": 1}
    assert broker.await_count == 2
    claims["connection_id"] = "connection-bob"
    claims["principal"] = {"user_id": "alice"}
    claims["receipt_id"] = "receipt-cross-owner"
    with pytest.raises(HTTPException) as wrong_owner:
        await tools_github.serve(packet, SimpleNamespace())
    assert wrong_owner.value.status_code == 403
    assert calls == {"alice": 1, "bob": 1}
    assert broker.await_count == 2


@pytest.mark.asyncio
async def test_expired_token_refresh_preserves_owner_and_observed_scopes(receipts, monkeypatch):
    from src.internal import credential_routes

    clock = {"now": 1001, "expired_at": 1000}
    owner = SimpleNamespace(id="user-alice", org_id="org-acme")
    credential = SimpleNamespace(
        id="owned-id",
        user_id=owner.id,
        org_id=owner.org_id,
        service="github",
        credential_type="oauth_token",
        agent_consent=True,
        expires_at=None,
        scopes={"oauth_scopes": ["read:user"]},
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:github",
    )
    binding = SimpleNamespace(provider_account_id="213", observed_scopes=["read:user"])
    claims = {
        "principal": {"user_id": owner.id},
        "connection_id": credential.id,
        "github_connection": {"connection_id": credential.id},
        "receipt_id": "refresh-1",
        "expires_at": 3000000000,
    }

    @asynccontextmanager
    async def authority(value, runtime):
        if value["principal"]["user_id"] != owner.id or value["connection_id"] != credential.id:
            raise HTTPException(403, "foreign user")
        yield credential, binding, owner

    identity = Mock()
    identity.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "workload-alice"}

    def identity_refresh(**params):
        assert clock["now"] > clock["expired_at"]
        assert params["workloadIdentityToken"] == "workload-alice"
        assert params["scopes"] == ["read:user"] and params["oauth2Flow"] == "USER_FEDERATION"
        return {"accessToken": "refreshed-alice"}

    identity.get_resource_oauth2_token.side_effect = identity_refresh
    calls = []

    async def provider(token):
        calls.append(token)
        return GithubObservation("213", ("read:user",), {"id": 213, "login": "alice"})

    monkeypatch.setattr(tools_github, "verify_facade", AsyncMock(side_effect=lambda *args, **kwargs: claims.copy()))
    monkeypatch.setattr(tools_github, "check_fresh", lambda value: None)
    monkeypatch.setattr(tools_github, "current_binding", lambda **kwargs: {"target_status": "READY"})
    monkeypatch.setattr(tools_github, "current_authority", authority)
    monkeypatch.setattr(tools_github, "get_identity_client", lambda: identity)
    monkeypatch.setattr(credential_routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    monkeypatch.setattr(tools_github, "observe_github_user", AsyncMock(side_effect=provider))
    packet = {"method": "tools/call", "id": 17, "params": {"name": "get_user", "arguments": {"adp_assertion": "signed"}}}
    response = await tools_github.serve(packet, SimpleNamespace())
    assert json.loads(json.loads(response.body)["result"]["content"][0]["text"])["id"] == 213
    assert calls == ["refreshed-alice"] and "refreshed-alice" not in response.body.decode()
    claims["receipt_id"] = "refresh-2"
    repeated = await tools_github.serve(packet, SimpleNamespace())
    assert json.loads(json.loads(repeated.body)["result"]["content"][0]["text"])["id"] == 213
    assert "refreshed-alice" not in repeated.body.decode()
    claims["receipt_id"] = "refresh-3"
    monkeypatch.setitem(credential_routes.PROVIDER_SCOPES, "github", ("github", ["read:user", "repo"]))
    unchanged = await tools_github.serve(packet, SimpleNamespace())
    assert json.loads(json.loads(unchanged.body)["result"]["content"][0]["text"])["id"] == 213
    assert "refreshed-alice" not in unchanged.body.decode()
    assert calls == ["refreshed-alice"] * 3
    assert identity.get_resource_oauth2_token.call_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("failed_refresh", "withdrawn_consent", "changed_scopes", "changed_binding"))
async def test_refresh_failure_or_committed_authority_change_never_dispatches(receipts, monkeypatch, fault):
    from src.internal import credential_routes, github_oauth
    from src.shared.models.base import new_uuid
    from src.shared.models.vault import OAuthConnectionBinding, UserCredential

    factory = tools_github.get_session_factory()
    pointer = UserCredential(
        id="00000000-0000-4000-8000-000000000001",
        org_id="org-acme",
        user_id="user-alice",
        service="github",
        provider="agentcore-identity",
        credential_type="oauth_token",
        label="AgentCore OAuth",
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:github",
        scopes={"oauth_scopes": ["read:user"]},
        agent_consent=True,
    )
    binding = github_oauth.new_github_binding(pointer, GithubObservation("213", ("read:user",), {"id": 213}))
    async with factory() as database:
        database.add(pointer)
        database.add(binding)
        await database.commit()
    claim = {
        "principal": {"user_id": "user-alice", "tenant_id": "org-acme"},
        "connection_id": pointer.id,
        "github_connection": github_oauth.binding_claim(binding),
        "receipt_id": "refresh-failure-1",
        "expires_at": 3000000000,
    }
    monkeypatch.setattr(tools_github, "verify_facade", AsyncMock(return_value=claim))
    monkeypatch.setattr(tools_github, "check_fresh", lambda value: None)
    monkeypatch.setattr(tools_github, "check_policy", AsyncMock())
    monkeypatch.setattr(tools_github, "current_tools_human", AsyncMock(return_value=SimpleNamespace(id="user-alice", org_id="org-acme")))
    monkeypatch.setattr(tools_github, "current_binding", lambda **kwargs: {"target_status": "READY"})
    monkeypatch.setattr(github_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    monkeypatch.setattr(credential_routes, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    identity = Mock()
    identity.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "private-workload-token"}
    identity.get_resource_oauth2_token.side_effect = RuntimeError("private-provider-token")
    monkeypatch.setattr(tools_github, "get_identity_client", lambda: identity)
    provider = AsyncMock(return_value=GithubObservation("213", ("read:user",), {"id": 213}))
    monkeypatch.setattr(tools_github, "observe_github_user", provider)
    broker = tools_github._identity_access_token
    if fault != "failed_refresh":

        async def change_authority(credential, owner, client):
            assert credential.id == pointer.id and owner.id == "user-alice"
            async with factory() as database:
                stored = await database.get(UserCredential, pointer.id)
                stored_binding = await database.get(OAuthConnectionBinding, pointer.id)
                if fault == "withdrawn_consent":
                    stored.agent_consent = False
                elif fault == "changed_scopes":
                    stored.scopes = {"oauth_scopes": ["read:user", "repo"]}
                else:
                    stored_binding.revision = new_uuid()
                await database.commit()
            return "private-provider-token"

        broker = AsyncMock(side_effect=change_authority)
        monkeypatch.setattr(tools_github, "_identity_access_token", broker)
    packet = {"method": "tools/call", "id": 17, "params": {"name": "get_user", "arguments": {"adp_assertion": "signed"}}}
    with pytest.raises(HTTPException) as refused:
        await tools_github.serve(packet, SimpleNamespace())
    assert refused.value.status_code == (502 if fault == "failed_refresh" else 403)
    assert "private-provider-token" not in str(refused.value.detail)
    provider.assert_not_awaited()
    if fault == "failed_refresh":
        identity.get_resource_oauth2_token.assert_called_once()
    else:
        broker.assert_awaited_once()
    with pytest.raises(HTTPException) as replay:
        await tools_github.serve(packet, SimpleNamespace())
    assert replay.value.status_code in {403, 409}
    provider.assert_not_awaited()
    if fault == "failed_refresh":
        identity.get_resource_oauth2_token.assert_called_once()
    else:
        broker.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnected_and_reconnected_pointer_refuses_prior_signed_calls(receipts, monkeypatch):
    import time

    from sqlalchemy import select

    from src.auth import connections_routes
    from src.internal import github_oauth
    from src.shared.models.audit import AuditLog
    from src.shared.models.base import new_uuid
    from src.shared.models.vault import UserCredential

    factory = tools_github.get_session_factory()
    owner = SimpleNamespace(id="user-alice", org_id="org-acme")
    observation = GithubObservation("213", ("read:user",), {"id": 213, "login": "alice"})

    async def connect():
        pointer = UserCredential(
            id=new_uuid(),
            org_id=owner.org_id,
            user_id=owner.id,
            service="github",
            provider="agentcore-identity",
            credential_type="oauth_token",
            label="AgentCore OAuth",
            secret_arn="agentcore-identity:adp-tools-dev-oauth-user:github",
            scopes={"oauth_scopes": ["read:user"]},
            agent_consent=True,
        )
        binding = github_oauth.new_github_binding(pointer, observation)
        async with factory() as database:
            database.add(pointer)
            database.add(binding)
            await database.commit()
        return pointer.id, github_oauth.binding_claim(binding)

    monkeypatch.setattr(github_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    monkeypatch.setattr(tools_github, "check_policy", AsyncMock())
    monkeypatch.setattr(tools_github, "current_tools_human", AsyncMock(return_value=owner))
    monkeypatch.setattr(tools_github, "current_binding", lambda **kwargs: {"target_status": "READY"})
    monkeypatch.setattr(tools_github, "get_identity_client", lambda: object())
    monkeypatch.setattr(connections_routes, "_human_caller", AsyncMock(return_value=SimpleNamespace(user_id=owner.id, org_id=owner.org_id)))
    broker = AsyncMock(return_value="private-provider-token")
    provider = AsyncMock(return_value=observation)
    monkeypatch.setattr(tools_github, "_identity_access_token", broker)
    monkeypatch.setattr(tools_github, "observe_github_user", provider)
    first_id, first_binding = await connect()
    issued = int(time.time())
    tools_github.check_fresh({"issued_at": issued, "expires_at": issued + 30})
    with pytest.raises(ValueError):
        tools_github.check_fresh({"issued_at": issued, "expires_at": issued + 31})
    claim = {
        "principal": {"user_id": owner.id, "tenant_id": owner.org_id},
        "connection_id": first_id,
        "github_connection": first_binding,
        "receipt_id": "signed-before-disconnect",
        "issued_at": issued,
        "expires_at": issued + 30,
    }
    monkeypatch.setattr(tools_github, "verify_facade", AsyncMock(side_effect=lambda *args, **kwargs: claim.copy()))
    packet = {"method": "tools/call", "id": 17, "params": {"name": "get_user", "arguments": {"adp_assertion": "signed"}}}

    async def disconnect():
        async with factory() as database:
            response = await connections_routes.disconnect_connection(
                "github",
                caller=object(),
                db=database,
            )
            audit = (await database.execute(select(AuditLog).where(AuditLog.event_type == "oauth_connection_disconnected"))).scalars().all()
        assert response == {
            "status": "disconnected",
            "identity_token_revoked": False,
            "provider_access_token_expires_at": None,
            "private_assertion_max_seconds": 30,
            "provider_token_revocation": "not_attempted",
            "revocation_complete": False,
        }
        assert audit[-1].details["provider_access_token_expires_at"] is None
        assert audit[-1].details["provider_token_revocation"] == "not_attempted"
        assert audit[-1].details["revocation_complete"] is False
        assert audit[-1].details["private_assertion_max_seconds"] == 30

    await disconnect()
    with pytest.raises(HTTPException) as disconnected:
        await tools_github.serve(packet, SimpleNamespace())
    assert disconnected.value.status_code == 403
    broker.assert_not_awaited()
    provider.assert_not_awaited()
    second_id, second_binding = await connect()
    assert second_id != first_id and second_binding != first_binding
    with pytest.raises(HTTPException) as stale:
        await tools_github.serve(packet, SimpleNamespace())
    assert stale.value.status_code == 403
    broker.assert_not_awaited()
    provider.assert_not_awaited()
    claim.update(connection_id=second_id, github_connection=second_binding, receipt_id="signed-after-reconnect")
    response = await tools_github.serve(packet, SimpleNamespace())
    assert response.status_code == 200 and "private-provider-token" not in response.body.decode()
    broker.assert_awaited_once()
    provider.assert_awaited_once_with("private-provider-token")
    with pytest.raises(HTTPException) as replay:
        await tools_github.serve(packet, SimpleNamespace())
    assert replay.value.status_code == 409
    await disconnect()
    claim["receipt_id"] = "new-receipt-after-disconnect"
    with pytest.raises(HTTPException) as revoked:
        await tools_github.serve(packet, SimpleNamespace())
    assert revoked.value.status_code == 403
    broker.assert_awaited_once()
    provider.assert_awaited_once()
