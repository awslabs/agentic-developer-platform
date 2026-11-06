"""A Jira read verifies current ADP authority, account and site before issue dispatch."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal import tools_jira
from src.internal.jira_provider import JiraSiteObservation, JiraUserObservation
from src.shared.models.base import Base

SITE = JiraSiteObservation("00000000-0000-4000-8000-000000000001", ("read:jira-work",))
CLAIMS = {
    "principal": {"user_id": "user-alice", "tenant_id": "org-acme", "team_id": "team-eng"},
    "jira_connection": {"connection_id": "owned-connection"},
    "connection_id": "owned-connection",
    "receipt_id": "receipt-one",
    "expires_at": 3000000000,
}
PACKET = {
    "method": "tools/call",
    "id": 17,
    "params": {"name": "get_issue", "arguments": {"cloudId": SITE.cloud_id, "issueIdOrKey": "EXAMPLE-1", "adp_assertion": "signed"}},
}


@pytest.fixture
async def dispatch(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tools_jira, "get_session_factory", lambda: factory)
    monkeypatch.setattr(tools_jira, "verify_facade", AsyncMock(return_value=CLAIMS))
    monkeypatch.setattr(tools_jira, "check_fresh", lambda value: None)
    monkeypatch.setattr(tools_jira, "current_binding", lambda **kwargs: {"target_status": "READY", "target_id": "target-one"})
    monkeypatch.setattr(tools_jira, "get_identity_client", Mock(return_value=object()))
    broker = AsyncMock(return_value="private-provider-token")
    account = AsyncMock(return_value=JiraUserObservation("account-one"))
    sites = AsyncMock(return_value=(SITE,))
    issue = AsyncMock(return_value={"id": "123", "key": "EXAMPLE-1", "fields": {"summary": "Fixture"}})
    monkeypatch.setattr(tools_jira, "_identity_access_token", broker)
    monkeypatch.setattr(tools_jira, "observe_jira_user", account)
    monkeypatch.setattr(tools_jira, "observe_jira_sites", sites)
    monkeypatch.setattr(tools_jira, "read_jira_issue", issue)
    checks = []

    @asynccontextmanager
    async def authority(claims, runtime):
        checks.append(claims)
        yield (
            SimpleNamespace(id="owned-connection"),
            SimpleNamespace(provider_account_id="account-one", observed_resources=[{"cloud_id": SITE.cloud_id, "scopes": list(SITE.scopes)}]),
            SimpleNamespace(id="user-alice"),
        )

    monkeypatch.setattr(tools_jira, "current_authority", authority)
    yield broker, account, sites, issue, checks
    await engine.dispose()


@pytest.mark.asyncio
async def test_one_read_uses_same_token_for_account_sites_issue_and_one_use_receipt(dispatch):
    broker, account, sites, issue, checks = dispatch
    response = await tools_jira.serve(PACKET, SimpleNamespace())
    assert json.loads(response.body)["result"]["content"] == [{"type": "text", "text": json.dumps(issue.return_value)}]
    assert b"private-provider-token" not in response.body
    account.assert_awaited_once_with("private-provider-token")
    sites.assert_awaited_once_with("private-provider-token")
    issue.assert_awaited_once_with("private-provider-token", protected_site=SITE, cloud_id=SITE.cloud_id, issue_id_or_key="EXAMPLE-1")
    assert len(checks) == 5
    tools_jira.verify_facade.assert_awaited_once_with(
        "signed", "get_issue", {"cloudId": SITE.cloud_id, "issueIdOrKey": "EXAMPLE-1"}, 17, namespace="jira"
    )
    with pytest.raises(HTTPException) as replay:
        await tools_jira.serve(PACKET, SimpleNamespace())
    assert replay.value.status_code == 409
    assert broker.await_count == issue.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["account", "site", "scopes"])
async def test_changed_provider_authority_never_reads_issue(dispatch, fault):
    _, account, sites, issue, _ = dispatch
    if fault == "account":
        account.return_value = JiraUserObservation("different-account")
    elif fault == "site":
        sites.return_value = ()
    else:
        sites.return_value = (JiraSiteObservation(SITE.cloud_id, ("read:jira-work", "write:jira-work")),)
    with pytest.raises(HTTPException) as refused:
        await tools_jira.serve(PACKET, SimpleNamespace())
    assert refused.value.status_code == 403
    issue.assert_not_awaited()
    if fault == "account":
        sites.assert_not_awaited()


@pytest.mark.asyncio
async def test_unbound_site_is_refused_before_any_provider_call(dispatch):
    broker, account, sites, issue, _ = dispatch
    packet = {
        **PACKET,
        "params": {**PACKET["params"], "arguments": {**PACKET["params"]["arguments"], "cloudId": "00000000-0000-4000-8000-000000000002"}},
    }
    with pytest.raises(HTTPException) as refused:
        await tools_jira.serve(packet, SimpleNamespace())
    assert refused.value.detail == {"error": "jira_site_denied"}
    for method in (broker, account, sites, issue):
        method.assert_not_awaited()


@pytest.mark.asyncio
async def test_revocation_between_identity_and_issue_stops_dispatch(dispatch, monkeypatch):
    _, _, _, issue, _ = dispatch
    original = tools_jira.current_authority
    checks = 0

    @asynccontextmanager
    async def revoked(claims, runtime):
        nonlocal checks
        checks += 1
        if checks == 4:
            raise HTTPException(403, "current human membership required")
        async with original(claims, runtime) as selected:
            yield selected

    monkeypatch.setattr(tools_jira, "current_authority", revoked)
    with pytest.raises(HTTPException) as refused:
        await tools_jira.serve(PACKET, SimpleNamespace())
    assert refused.value.status_code == 403
    issue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("jira_ready,github_ready", [(True, False), (False, True)])
async def test_catalogue_publication_uses_jira_target_readiness(dispatch, monkeypatch, jira_ready, github_ready):
    monkeypatch.setenv("ADP_TOOLS_JIRA_TARGET_ID", "jira-target" if jira_ready else "")
    monkeypatch.setenv("ADP_TOOLS_GITHUB_TARGET_ID", "github-target" if github_ready else "")
    publish = Mock()
    monkeypatch.setattr(tools_jira, "publish_catalogue", publish)
    response = await tools_jira.serve({"method": "tools/list", "id": 9}, SimpleNamespace(store=SimpleNamespace(client=object(), table="fixture")))
    assert response.status_code == 200
    assert publish.call_count == int(jira_ready)
    for call in dispatch[:4]:
        call.assert_not_awaited()
