"""Reconnect requires a fresh explicit consent attempt and preserves old authority on failure."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.auth import atlassian_mcp_routes
from src.internal import jira_provider
from src.internal.github_oauth import credential_digest
from src.shared.identity.oauth_setup import get_oauth_setup
from src.shared.models.vault import OAuthConnectionAttempt, OAuthConnectionBinding, UserCredential
from tests.auth import test_connections_routes as connection_fixtures

api = connection_fixtures.api
session = connection_fixtures.session


@pytest.mark.asyncio
async def test_atlassian_mcp_reconnect_fences_lost_completion_and_preserves_newer_grant(api, session, monkeypatch):
    client, app, identity = api
    setup = SimpleNamespace(
        settings=SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user", atlassian_identity_compatibility_verified=True),
        secret_store=object(),
        atlassian_mcp_status=lambda **kwargs: {"status": "ready"},
    )
    app.dependency_overrides[get_oauth_setup] = lambda: setup
    monkeypatch.setattr(atlassian_mcp_routes, "read_tenant_policy_observation", AsyncMock(return_value="allowed"))
    monkeypatch.setattr(
        atlassian_mcp_routes,
        "atlassian_mcp_config",
        lambda settings, secret_store: {
            "return_url": "https://example.test/api/auth/connections/atlassian-mcp/return",
            "metadata_sha256": "a" * 64,
        },
    )
    identity.get_resource_oauth2_token.return_value = {
        "sessionStatus": "IN_PROGRESS",
        "authorizationUrl": "https://example.test/authorize",
        "sessionUri": "opaque-session",
    }
    observation = AsyncMock(side_effect=HTTPException(503, {"error": "adapter_unavailable"}))
    monkeypatch.setattr(atlassian_mcp_routes, "observe_connection", observation)
    first = client.post("/auth/connections/atlassian-mcp/start", json={"capability_set": "reads"}).json()
    first_callback = {"state": first["state"], "session_uri": first["session_uri"]}
    unavailable = client.post("/auth/connections/atlassian-mcp/complete", json=first_callback)
    assert unavailable.status_code == 503
    old_attempt = await session.get(OAuthConnectionAttempt, first["attempt_id"])
    assert old_attempt.identity_completed_at is not None and old_attempt.consumed_at is None
    assert identity.complete_resource_token_auth.call_count == 1

    second = client.post("/auth/connections/atlassian-mcp/start", json={"capability_set": "reads"}).json()
    assert second["state"] != first["state"]
    assert old_attempt.superseded_at is not None and old_attempt.consumed_at is None
    refused = client.post("/auth/connections/atlassian-mcp/complete", json=first_callback)
    assert refused.status_code == 403
    assert identity.complete_resource_token_auth.call_count == 1
    observation.assert_awaited_once()

    async def observed(*, attempt, user_id, tenant_id):
        assert attempt.state_digest == second["attempt_id"]
        assert user_id == "user-alice" and tenant_id == "org-acme"
        return SimpleNamespace(
            attempt_id=attempt.state_digest,
            config_revision="a" * 64,
            purpose="connection_observe",
            provider_user_id="subject-alice",
            sites=(SimpleNamespace(site_id="site-1", scopes=("read:jira:agent-interface",)),),
            granted_scopes=atlassian_mcp_routes.SCOPES,
        )

    monkeypatch.setattr(atlassian_mcp_routes, "observe_connection", observed)
    current = client.post(
        "/auth/connections/atlassian-mcp/complete",
        json={
            "state": second["state"],
            "session_uri": second["session_uri"],
        },
    )
    assert current.status_code == 201, current.text
    assert identity.complete_resource_token_auth.call_count == 2
    binding = await session.get(OAuthConnectionBinding, current.json()["id"])
    assert binding.provider_account_id == "subject-alice" and binding.revision == current.json()["revision"]
    assert client.post("/auth/connections/atlassian-mcp/complete", json=first_callback).status_code == 403
    assert identity.complete_resource_token_auth.call_count == 2


@pytest.mark.asyncio
async def test_jira_write_escalation_requires_fresh_owner_consent_and_observed_site_grant(api, session, monkeypatch):
    from tests.auth.test_connections_routes import caller

    client, app, identity = api
    setup = SimpleNamespace(
        settings=SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user", atlassian_identity_compatibility_verified=True),
        secret_store=object(),
        atlassian_mcp_status=lambda **kwargs: {"status": "ready"},
    )
    app.dependency_overrides[get_oauth_setup] = lambda: setup
    monkeypatch.setattr(atlassian_mcp_routes, "read_tenant_policy_observation", AsyncMock(return_value="allowed"))
    monkeypatch.setattr(
        atlassian_mcp_routes,
        "atlassian_mcp_config",
        lambda *_: {
            "return_url": "https://example.test/api/auth/connections/atlassian-mcp/return",
            "metadata_sha256": "a" * 64,
        },
    )
    identity.get_resource_oauth2_token.return_value = {
        "sessionStatus": "IN_PROGRESS",
        "authorizationUrl": "https://example.test/authorize",
        "sessionUri": "opaque-session",
    }
    credential = UserCredential(
        id="00000000-0000-4000-8000-000000000030",
        org_id="org-acme",
        user_id="user-alice",
        service="atlassian-mcp",
        credential_type="oauth_token",
        provider="agentcore-identity",
        label="Atlassian MCP OAuth",
        secret_arn="agentcore-identity:adp-tools-dev-atlassian-mcp-oauth-user:atlassian-mcp",
        scopes={"oauth_scopes": list(atlassian_mcp_routes.SCOPES)},
        strict=True,
        agent_consent=True,
    )
    binding = OAuthConnectionBinding(
        credential_id=credential.id,
        org_id="org-acme",
        user_id="user-alice",
        provider="atlassian-mcp",
        revision="revision-one",
        credential_digest=credential_digest(credential),
        provider_account_id="subject-alice",
        observed_scopes=list(atlassian_mcp_routes.SCOPES),
        observed_resources=[{"site_id": "site-one", "scopes": ["read:jira:agent-interface"]}],
        selected_site_id="site-one",
        verified_at=datetime.now(UTC),
    )
    session.add_all([credential, binding])
    await session.commit()
    request = {"capability_set": "jira-writes", "reconnect_revision": "revision-one"}
    assert client.post("/auth/connections/atlassian-mcp/start", json=request).status_code == 503
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_WRITES_ENABLED", "true")
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_WRITES_SHA256", "b" * 64)
    assert client.post("/auth/connections/atlassian-mcp/start", json={**request, "reconnect_revision": "other"}).status_code == 409
    app.dependency_overrides[connection_fixtures.get_current_user_context] = lambda: caller(user_id="user-bob")
    assert client.post("/auth/connections/atlassian-mcp/start", json=request).status_code == 409
    app.dependency_overrides[connection_fixtures.get_current_user_context] = lambda: caller()
    started = client.post("/auth/connections/atlassian-mcp/start", json=request)
    assert started.status_code == 200, started.text
    assert identity.get_resource_oauth2_token.call_args.kwargs["scopes"] == list(atlassian_mcp_routes.WRITE_SCOPES)
    attempt = await session.get(OAuthConnectionAttempt, started.json()["attempt_id"])
    assert attempt.requested_scopes == list(atlassian_mcp_routes.WRITE_SCOPES)
    callback = {"state": started.json()["state"], "session_uri": started.json()["session_uri"]}

    async def observe(*, attempt, user_id, tenant_id):
        assert (user_id, tenant_id) == ("user-alice", "org-acme")
        return SimpleNamespace(
            attempt_id=attempt.state_digest,
            config_revision="a" * 64,
            purpose="connection_observe",
            provider_user_id="subject-alice",
            sites=(SimpleNamespace(site_id="site-one", scopes=("read:jira:agent-interface",)),),
            granted_scopes=atlassian_mcp_routes.SCOPES,
        )

    monkeypatch.setattr(atlassian_mcp_routes, "observe_connection", observe)
    refused = client.post("/auth/connections/atlassian-mcp/complete", json=callback)
    assert refused.status_code == 403, refused.text
    await session.refresh(credential)
    assert credential.scopes == {"oauth_scopes": list(atlassian_mcp_routes.SCOPES)}
    await session.refresh(binding)
    assert binding.revision == "revision-one"

    async def observed_write(*, attempt, user_id, tenant_id):
        result = await observe(attempt=attempt, user_id=user_id, tenant_id=tenant_id)
        result.sites = (
            SimpleNamespace(
                site_id="site-one",
                scopes=(
                    "read:jira:agent-interface",
                    "write:jira:agent-interface",
                ),
            ),
        )
        result.granted_scopes = atlassian_mcp_routes.WRITE_SCOPES
        return result

    monkeypatch.setattr(atlassian_mcp_routes, "observe_connection", observed_write)
    completed = client.post("/auth/connections/atlassian-mcp/complete", json=callback)
    assert completed.status_code == 201, completed.text
    await session.refresh(credential)
    await session.refresh(binding)
    assert credential.scopes == {"oauth_scopes": list(atlassian_mcp_routes.WRITE_SCOPES)}
    assert binding.revision != "revision-one"
    assert "write:jira:agent-interface" in binding.observed_resources[0]["scopes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lost_identity_reply", "lost_database_commit"])
async def test_atlassian_mcp_uncertain_identity_completion_requires_fresh_consent(api, session, monkeypatch, failure):
    client, app, identity = api
    setup = SimpleNamespace(
        settings=SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user", atlassian_identity_compatibility_verified=True),
        secret_store=object(),
        atlassian_mcp_status=lambda **kwargs: {"status": "ready"},
    )
    app.dependency_overrides[get_oauth_setup] = lambda: setup
    monkeypatch.setattr(atlassian_mcp_routes, "read_tenant_policy_observation", AsyncMock(return_value="allowed"))
    monkeypatch.setattr(
        atlassian_mcp_routes,
        "atlassian_mcp_config",
        lambda settings, secret_store: {
            "return_url": "https://example.test/api/auth/connections/atlassian-mcp/return",
            "metadata_sha256": "a" * 64,
        },
    )
    identity.get_resource_oauth2_token.return_value = {
        "sessionStatus": "IN_PROGRESS",
        "authorizationUrl": "https://example.test/authorize",
        "sessionUri": "opaque-session",
    }
    observer = AsyncMock(side_effect=AssertionError("uncertain Identity completion cannot be observed"))
    monkeypatch.setattr(atlassian_mcp_routes, "observe_connection", observer)
    started = client.post("/auth/connections/atlassian-mcp/start", json={"capability_set": "reads"}).json()
    callback = {"state": started["state"], "session_uri": started["session_uri"]}
    if failure == "lost_identity_reply":
        identity.complete_resource_token_auth.side_effect = RuntimeError("SDK reply lost")
    else:
        original_commit = session.commit
        failed = False

        async def lose_commit():
            nonlocal failed
            attempt = await session.get(OAuthConnectionAttempt, started["attempt_id"])
            if not failed and attempt.identity_completed_at is not None:
                failed = True
                raise RuntimeError("DB commit reply lost")
            await original_commit()

        monkeypatch.setattr(session, "commit", lose_commit)
    refused = client.post("/auth/connections/atlassian-mcp/complete", json=callback)
    assert refused.status_code == (502 if failure == "lost_identity_reply" else 500), refused.text
    if failure == "lost_database_commit":
        await session.rollback()
    attempt = await session.get(OAuthConnectionAttempt, started["attempt_id"])
    assert attempt.identity_completion_started_at is not None
    assert attempt.identity_completed_at is None and attempt.consumed_at is None
    assert identity.complete_resource_token_auth.call_count == 1
    again = client.post("/auth/connections/atlassian-mcp/complete", json=callback)
    assert again.status_code == 409 and again.json()["detail"]["error"] == "reconnect_required"
    assert identity.complete_resource_token_auth.call_count == 1
    observer.assert_not_awaited()
    assert (await session.scalars(select(UserCredential).where(UserCredential.service == "atlassian-mcp"))).all() == []
    restart = client.post("/auth/connections/atlassian-mcp/start", json={"capability_set": "reads"})
    assert restart.status_code == 200
    assert attempt.superseded_at is not None


@pytest.mark.asyncio
async def test_jira_reconnect_upgrades_legacy_grant_only_after_verified_consent(api, session):
    client, _, identity = api
    pointer = UserCredential(
        id="00000000-0000-4000-8000-000000000009",
        org_id="org-acme",
        user_id="user-alice",
        service="jira",
        provider="agentcore-identity",
        credential_type="oauth_token",
        label="Original connection",
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:atlassian",
        agent_consent=True,
        scopes={"oauth_scopes": ["read:jira-work"]},
    )
    session.add(pointer)
    await session.commit()
    assert client.post("/auth/connections/oauth/atlassian/start").status_code == 409
    started = client.post("/auth/connections/oauth/atlassian/start", json={"reconnect": True}).json()
    assert pointer.scopes == {"oauth_scopes": ["read:jira-work"]}
    assert identity.get_resource_oauth2_token.call_args.kwargs["scopes"] == ["read:jira-work", "read:me", "offline_access"]
    result = client.post("/auth/connections/oauth/return", json={"state": started["state"], "session_uri": started["session_uri"]})
    assert result.status_code == 201, result.text
    assert result.json()["id"] == pointer.id and pointer.label == "Original connection"
    assert pointer.scopes == {"oauth_scopes": ["read:jira-work", "read:me", "offline_access"]}
    binding = await session.get(OAuthConnectionBinding, pointer.id)
    assert binding.provider_account_id == "fixture-account"
    assert binding.observed_resources == [{"cloud_id": "00000000-0000-4000-8000-000000000001", "scopes": ["read:jira-work"]}]
    assert "offline_access" not in binding.observed_scopes


@pytest.mark.asyncio
async def test_revocation_during_reconnect_prevents_completion_and_token_exchange(api, session):
    client, _, identity = api
    first = client.post("/auth/connections/oauth/atlassian/start").json()
    assert client.post("/auth/connections/oauth/return", json={"state": first["state"], "session_uri": first["session_uri"]}).status_code == 201
    pointer = (await session.scalars(select(UserCredential))).one()
    second = client.post("/auth/connections/oauth/atlassian/start", json={"reconnect": True}).json()
    pointer.agent_consent = False
    await session.commit()
    identity.complete_resource_token_auth.reset_mock()
    response = client.post("/auth/connections/oauth/return", json={"state": second["state"], "session_uri": second["session_uri"]})
    assert response.status_code == 409 and response.json()["detail"]["error"] == "connection_changed"
    identity.complete_resource_token_auth.assert_not_called()
    assert pointer.agent_consent is False


@pytest.mark.asyncio
async def test_old_consent_without_identity_scope_cannot_be_completed(api, session):
    client, _, identity = api
    started = client.post("/auth/connections/oauth/atlassian/start").json()
    attempt = (await session.scalars(select(OAuthConnectionAttempt))).one()
    attempt.requested_scopes = ["read:jira-work"]
    await session.commit()
    result = client.post("/auth/connections/oauth/return", json={"state": started["state"], "session_uri": started["session_uri"]})
    assert result.status_code == 403 and result.json()["detail"]["error"] == "reconnect_required"
    identity.complete_resource_token_auth.assert_not_called()
    assert list(await session.scalars(select(UserCredential))) == []


@pytest.mark.asyncio
async def test_site_membership_cannot_substitute_for_user_identity(api, session, monkeypatch):
    client, _, _ = api
    monkeypatch.setattr(jira_provider, "observe_jira_user", AsyncMock(side_effect=HTTPException(403, "identity refused")))
    started = client.post("/auth/connections/oauth/atlassian/start").json()
    result = client.post("/auth/connections/oauth/return", json={"state": started["state"], "session_uri": started["session_uri"]})
    assert result.status_code == 403
    jira_provider.observe_jira_sites.assert_not_awaited()
    assert list(await session.scalars(select(UserCredential))) == []
    assert list(await session.scalars(select(OAuthConnectionBinding))) == []


@pytest.mark.asyncio
async def test_failed_reconnect_preserves_previous_binding(api, session, monkeypatch):
    client, _, _ = api
    started = client.post("/auth/connections/oauth/atlassian/start").json()
    result = client.post("/auth/connections/oauth/return", json={"state": started["state"], "session_uri": started["session_uri"]})
    binding = await session.get(OAuthConnectionBinding, result.json()["id"])
    revision = binding.revision
    second = client.post("/auth/connections/oauth/atlassian/start", json={"reconnect": True}).json()
    monkeypatch.setattr(jira_provider, "observe_jira_user", AsyncMock(return_value=jira_provider.JiraUserObservation("other-account")))
    refused = client.post("/auth/connections/oauth/return", json={"state": second["state"], "session_uri": second["session_uri"]})
    assert refused.status_code == 403 and refused.json()["detail"]["error"] == "atlassian_account_changed"
    assert binding.provider_account_id == "fixture-account" and binding.revision == revision


@pytest.mark.asyncio
async def test_slack_reconnect_preserves_write_scope_without_starting_unavailable_flow(api, session, monkeypatch):
    client, _, identity = api
    monkeypatch.delenv("ADP_TOOLS_SLACK_MEDIATOR_ENABLED", raising=False)
    pointer = UserCredential(
        id="00000000-0000-4000-8000-000000000009",
        org_id="org-acme",
        user_id="user-alice",
        service="slack",
        provider="agentcore-identity",
        credential_type="oauth_token",
        label="Retained Slack account",
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:slack",
        agent_consent=True,
        scopes={"oauth_scopes": ["users:read", "chat:write"]},
    )
    session.add(pointer)
    await session.commit()
    response = client.post("/auth/connections/oauth/slack/start", json={"reconnect": True})
    assert response.status_code == 503
    identity.get_resource_oauth2_token.assert_not_called()
    assert list(await session.scalars(select(OAuthConnectionAttempt))) == []
    assert pointer.scopes == {"oauth_scopes": ["users:read", "chat:write"]}
