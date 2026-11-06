"""Disconnect retains metadata-only revocation work and never promotes legacy Jira."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.internal.github_oauth import credential_digest
from src.shared.models.oauth_setup import OAuthGrantRevocation
from src.shared.models.vault import OAuthConnectionAttempt, OAuthConnectionBinding, UserCredential
from tests.auth import test_connections_routes as connection_fixtures

api = connection_fixtures.api
session = connection_fixtures.session


@pytest.mark.asyncio
async def test_disconnect_queues_each_grant_generation_without_identity_tokens(api, session, record_property):
    client, _, identity = api
    credential = UserCredential(
        id="v2-credential",
        org_id="org-acme",
        user_id="user-alice",
        service="atlassian-mcp",
        provider="agentcore-identity",
        credential_type="oauth_token",
        label="Atlassian MCP OAuth",
        secret_arn="agentcore-identity:workload:atlassian-mcp",
        scopes={"oauth_scopes": ["read:me"]},
        strict=True,
        agent_consent=True,
    )
    session.add(credential)
    await session.flush()
    binding = OAuthConnectionBinding(
        credential_id=credential.id,
        org_id="org-acme",
        user_id="user-alice",
        provider="atlassian-mcp",
        revision="revision-current",
        credential_digest=credential_digest(credential),
        provider_account_id="subject-alice",
        observed_scopes=["read:me"],
        observed_resources=[{"site_id": "site-one", "scopes": ["read:me"]}],
        selected_site_id="site-one",
        verified_at=datetime.now(UTC),
    )
    attempt = OAuthConnectionAttempt(
        state_digest="a" * 64,
        org_id="org-acme",
        user_id="user-alice",
        provider="atlassian-mcp",
        session_uri="opaque-session",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        identity_completion_started_at=datetime.now(UTC),
    )
    session.add_all([binding, attempt])
    await session.commit()
    wrong = client.request("DELETE", "/auth/connections/atlassian-mcp", json={"connection_revision": "older"})
    assert wrong.status_code == 409
    assert (await session.scalars(select(OAuthGrantRevocation))).all() == []
    response = client.request("DELETE", "/auth/connections/atlassian-mcp", json={"connection_revision": "revision-current"})
    assert response.status_code == 200, response.text
    assert response.json()["revocation_pending"] is True
    assert response.json()["revocation_complete"] is False
    jobs = (await session.scalars(select(OAuthGrantRevocation))).all()
    assert {(job.grant_revision, job.source_kind) for job in jobs} == {
        ("revision-current", "binding"),
        ("a" * 64, "attempt"),
    }
    assert all(job.tenant_id == "org-acme" and job.owner_ref == "user-alice" and job.state == "pending" for job in jobs)
    assert all("opaque-session" not in str(job.__dict__) and "private-workload-token" not in str(job.__dict__) for job in jobs)
    assert (await session.scalars(select(UserCredential).where(UserCredential.service == "atlassian-mcp"))).all() == []
    session.expire_all()
    assert (await session.get(OAuthConnectionAttempt, "a" * 64)).superseded_at is not None
    again = client.request("DELETE", "/auth/connections/atlassian-mcp", json={"connection_revision": "revision-current"})
    assert again.status_code == 200 and again.json()["revocation_pending"] is True
    assert len((await session.scalars(select(OAuthGrantRevocation))).all()) == 2
    identity.complete_resource_token_auth.assert_not_called()

    record_property(
        "identity_observation",
        json.dumps(
            {
                "owner": binding.user_id,
                "tenant": binding.org_id,
                "generations": sorted(job.grant_revision for job in jobs),
                "pending": len(jobs),
                "wrong_revision_status": wrong.status_code,
                "disconnect_status": response.status_code,
                "provider_calls": identity.complete_resource_token_auth.call_count,
                "token_persisted": any("opaque-session" in str(job.__dict__) for job in jobs),
            }
        ),
    )


@pytest.mark.asyncio
async def test_cancel_v2_migration_keeps_legacy_credential_and_binding(api, session, record_property):
    client, _, identity = api
    legacy = UserCredential(
        id="legacy-jira",
        org_id="org-acme",
        user_id="user-alice",
        service="jira",
        provider="agentcore-identity",
        credential_type="oauth_token",
        label="Jira OAuth",
        secret_arn="agentcore-identity:legacy:jira",
        scopes={"oauth_scopes": ["read:jira-work"]},
        strict=True,
        agent_consent=True,
    )
    session.add(legacy)
    await session.flush()
    legacy_binding = OAuthConnectionBinding(
        credential_id=legacy.id,
        org_id="org-acme",
        user_id="user-alice",
        provider="atlassian",
        revision="legacy-revision",
        credential_digest=credential_digest(legacy),
        provider_account_id="subject-alice",
        observed_scopes=["read:jira-work"],
        observed_resources=[],
        selected_site_id=None,
        verified_at=datetime.now(UTC),
    )
    pending = OAuthConnectionAttempt(
        state_digest="b" * 64,
        org_id="org-acme",
        user_id="user-alice",
        provider="atlassian-mcp",
        session_uri="opaque-v2-session",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session.add_all([legacy_binding, pending])
    await session.commit()
    response = client.delete("/auth/connections/atlassian-mcp")
    assert response.status_code == 200, response.text
    assert response.json()["revocation_pending"] is True
    assert (await session.get(UserCredential, legacy.id)).agent_consent is True
    assert (await session.get(OAuthConnectionBinding, legacy.id)).revision == "legacy-revision"
    assert (await session.get(OAuthConnectionAttempt, "b" * 64)).superseded_at is not None
    jobs = (await session.scalars(select(OAuthGrantRevocation))).all()
    assert len(jobs) == 1 and jobs[0].source_kind == "attempt" and jobs[0].grant_revision == "b" * 64
    assert len((await session.scalars(select(UserCredential).where(UserCredential.service == "jira"))).all()) == 1
    assert (await session.scalars(select(UserCredential).where(UserCredential.service == "atlassian-mcp"))).all() == []
    identity.complete_resource_token_auth.assert_not_called()
    record_property(
        "identity_observation",
        json.dumps(
            {
                "legacy_revision": (await session.get(OAuthConnectionBinding, legacy.id)).revision,
                "legacy_consent": (await session.get(UserCredential, legacy.id)).agent_consent,
                "v2_jobs": len(jobs),
                "v2_generation": jobs[0].grant_revision,
                "disconnect_status": response.status_code,
                "provider_calls": identity.complete_resource_token_auth.call_count,
            }
        ),
    )
