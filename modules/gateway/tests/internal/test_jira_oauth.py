"""The same Jira account and exact site grants are required on every dispatch."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.internal import jira_oauth
from src.internal.jira_provider import JiraSiteObservation, JiraUserObservation
from src.shared.models.vault import UserCredential
from tests.internal import test_slack_oauth as connection_fixtures

session = connection_fixtures.session

SITE = JiraSiteObservation("00000000-0000-4000-8000-000000000001", ("read:jira-work",))


@pytest.mark.asyncio
async def test_selection_requires_current_owner_consent_and_exact_resource_binding(session, monkeypatch):
    monkeypatch.setattr(jira_oauth, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-tools-dev-oauth-user"))
    credential = UserCredential(
        id="00000000-0000-4000-8000-000000000002",
        org_id="org-acme",
        user_id="user-alice",
        service="jira",
        provider="agentcore-identity",
        credential_type="oauth_token",
        label="AgentCore OAuth",
        secret_arn="agentcore-identity:adp-tools-dev-oauth-user:atlassian",
        agent_consent=True,
        scopes={"oauth_scopes": ["read:jira-work", "read:me", "offline_access"]},
    )
    binding = jira_oauth.new_jira_binding(credential, JiraUserObservation("account-one"), (SITE,))
    session.add_all([credential, binding])
    await session.commit()
    _, _, claim = await jira_oauth.selected_jira_connection(session, user_id="user-alice", tenant_id="org-acme")
    assert claim["account_id"] == "account-one" and len(claim["resources_sha256"]) == 64
    with pytest.raises(HTTPException):
        await jira_oauth.selected_jira_connection(session, user_id="user-bob", tenant_id="org-acme", expected=claim)
    credential.agent_consent = False
    await session.commit()
    with pytest.raises(HTTPException):
        await jira_oauth.selected_jira_connection(session, user_id="user-alice", tenant_id="org-acme", expected=claim)
    credential.agent_consent = True
    binding.observed_resources = [{"cloud_id": "00000000-0000-4000-8000-000000000003", "scopes": ["read:jira-work"]}]
    await session.commit()
    with pytest.raises(HTTPException) as changed:
        await jira_oauth.selected_jira_connection(session, user_id="user-alice", tenant_id="org-acme", expected=claim)
    assert changed.value.detail == "Jira connection changed"
    credential.scopes = {"oauth_scopes": ["read:jira-work"]}
    await session.commit()
    with pytest.raises(HTTPException):
        await jira_oauth.selected_jira_connection(session, user_id="user-alice", tenant_id="org-acme")


@pytest.mark.parametrize(
    "resources",
    [
        None,
        [],
        [{"cloud_id": "invalid", "scopes": ["read:jira-work"]}],
        [{"cloud_id": SITE.cloud_id, "scopes": ["read:me"]}],
        [{"cloud_id": SITE.cloud_id, "scopes": ["read:jira-work"]}] * 2,
    ],
)
def test_absent_malformed_or_ambiguous_protected_sites_are_refused(resources):
    with pytest.raises(HTTPException):
        jira_oauth.protected_sites(SimpleNamespace(observed_resources=resources))
