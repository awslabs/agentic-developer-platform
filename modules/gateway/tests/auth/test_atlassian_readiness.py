"""A static flag, a foreign grant or a stale observation cannot establish readiness."""

from datetime import UTC, datetime, timedelta

import pytest

from src.internal.github_oauth import credential_digest
from src.shared.identity.atlassian_readiness import observation_digest, verified_connection
from src.shared.models.vault import OAuthConnectionAttempt, OAuthConnectionBinding, UserCredential
from tests.auth.test_connections_routes import session  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "incomplete", "superseded", "configuration", "subject", "sites", "scopes", "owner", "consent", "unlinked"])
async def test_readiness_requires_the_current_owner_observation(session, change):  # noqa: F811
    now = datetime.now(UTC)
    credential = UserCredential(
        id="credential",
        org_id="org-acme",
        user_id="user-alice",
        service="atlassian-mcp",
        provider="agentcore-identity",
        credential_type="oauth_token",
        label="Controlled Atlassian grant",
        secret_arn="agentcore-identity:fixture:atlassian-mcp",
        strict=True,
        agent_consent=True,
        scopes={"oauth_scopes": ["read:jira:agent-interface"]},
    )
    session.add(credential)
    await session.flush()
    binding = OAuthConnectionBinding(
        credential_id=credential.id,
        org_id=credential.org_id,
        user_id=credential.user_id,
        provider="atlassian-mcp",
        revision="revision-one",
        credential_digest=credential_digest(credential),
        provider_account_id="provider-owner",
        observed_scopes=["read:jira:agent-interface"],
        observed_resources=[{"site_id": "site-one", "scopes": ["read:jira:agent-interface"]}],
        selected_site_id="site-one",
        verified_at=now,
    )
    session.add(binding)
    await session.flush()
    arguments = {"credential": credential, "binding": binding, "config_revision": "a" * 64}
    assert not await verified_connection(session, **arguments)
    attempt = OAuthConnectionAttempt(
        state_digest="b" * 64,
        org_id=credential.org_id,
        user_id=credential.user_id,
        provider="atlassian-mcp",
        session_uri="opaque-session",
        expires_at=now + timedelta(minutes=10),
        identity_completed_at=now,
        consumed_at=now,
        connection_snapshot={
            "config_revision": "a" * 64,
            "completed_connection": {"id": credential.id, "revision": binding.revision},
            "provider_observation_sha256": observation_digest(binding),
        },
    )
    session.add(attempt)
    await session.flush()
    assert await verified_connection(session, **arguments)
    if change == "incomplete":
        attempt.identity_completed_at = None
    elif change == "superseded":
        attempt.superseded_at = now
    elif change == "configuration":
        arguments["config_revision"] = "c" * 64
    elif change == "subject":
        binding.provider_account_id = "someone-else"
    elif change == "sites":
        binding.observed_resources = [{"site_id": "foreign", "scopes": ["read:jira:agent-interface"]}]
    elif change == "scopes":
        binding.observed_scopes = ["write:jira:agent-interface"]
    elif change == "owner":
        binding.user_id = "user-bob"
    elif change == "consent":
        credential.agent_consent = False
    elif change == "unlinked":
        attempt.connection_snapshot = {**attempt.connection_snapshot, "provider_observation_sha256": "0" * 64}
    else:
        binding.revision = "selected-revision"
    await session.flush()
    assert await verified_connection(session, **arguments) is (change is None)
