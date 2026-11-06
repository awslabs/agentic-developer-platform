"""Effective access reports are read-only and fail closed on missing policy."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.internal import tools_access, tools_authorize
from src.internal.tool_entitlements import effective_entitlement
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Team, TeamMembership, User
from src.shared.models.tool_entitlement import ToolEntitlement


def grant(name, *, team="", enabled=True, ceiling=None, revision=1):
    return SimpleNamespace(tool_name=name, team_id=team, enabled=enabled, max_cost_usd=ceiling, revision=revision)


@pytest.fixture
def resolved_team(monkeypatch):
    monkeypatch.setattr(tools_access, "primary_team_for_workspace", AsyncMock(return_value=SimpleNamespace(id="team-x")))


@pytest.mark.asyncio
@pytest.mark.parametrize("authoritative_membership", [True, False])
async def test_diagnostic_team_agrees_with_human_authorization(monkeypatch, authoritative_membership):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            for model in (User, Team, TeamMembership, TenantMembership, ToolEntitlement):
                await connection.run_sync(model.__table__.create)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            owner = User(id="owner-one", org_id="tenant-one", team_id="old-team", email="owner@example.test")
            db.add_all(
                [
                    owner,
                    Team(id="old-team", org_id="tenant-one", department_id="dept", name="Old"),
                    Team(id="new-team", org_id="tenant-one", department_id="dept", name="New"),
                    ToolEntitlement(org_id="tenant-one", team_id="", tool_name="browser.start", enabled=True, revision=1),
                    ToolEntitlement(org_id="tenant-one", team_id="new-team", tool_name="browser.start", enabled=False, revision=2),
                ]
            )
            if authoritative_membership:
                db.add(TeamMembership(user_id=owner.id, org_id=owner.org_id, team_id="new-team", is_primary=True))
            await db.commit()
            context = SimpleNamespace(account_type="human", auth_source="jwt", user_id=owner.id, org_id=owner.org_id, cognito_username="")
            monkeypatch.setattr(tools_authorize, "validate_cognito_jwt", AsyncMock(return_value=context))
            request = Request({"type": "http", "headers": [(b"x-adp-user-authorization", b"Bearer test")]})
            _, authorized_owner, team_id = await tools_authorize.authenticate_human(request, db)
            entitled, _ = await effective_entitlement(db, tenant_id=owner.org_id, team_id=team_id, tool_name="browser.start")
            report = await tools_access.tool_access_report(
                db,
                tenant_id=owner.org_id,
                owner=owner,
                policy={
                    "status": "active",
                    "allowed_tools": ["browser.start"],
                    "consented_tools": ["browser.start"],
                    "tool_max_cost_usd": {"browser.start": "0.10"},
                    "catalog": [{"name": "browser___start"}],
                },
                catalogue={"tools": [{"name": "browser.start"}]},
            )
            assert report["user_id"] == authorized_owner.id == owner.id
            assert report["team_id"] == team_id == ("new-team" if authoritative_membership else "old-team")
            assert report["tools"][0]["ready"] is entitled is (not authoritative_membership)
            assert report["tools"][0]["reason"] == ("team_disabled" if authoritative_membership else None)
            assert report["tools"][0]["team_entitlement_revision"] == (2 if authoritative_membership else 0)
            assert owner.team_id == "old-team"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_diagnostic_priority_distinguishes_deployment_tenant_team_consent_and_cost(resolved_team):
    names = [
        "browser.start",
        "browser.step",
        "code_interpreter.start",
        "websearch.search",
        "websearch.operation_get",
        "browser.inspect",
        "browser.close",
    ]
    grants = [grant(name) for name in names] + [grant("removed.call")]
    grants += [grant("browser.start", enabled=False), grant("code_interpreter.start", team="team-x", enabled=False)]
    # The latest tenant row in a real database is unique; model a disabled tenant row directly.
    grants = [entry for entry in grants if not (entry.tool_name == "browser.start" and entry.enabled)]
    db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: grants)), scalar=AsyncMock())
    owner = SimpleNamespace(id="owner-one", team_id="team-x")
    policy = {
        "status": "active",
        "revision": 7,
        "allowed_tools": [*names, "removed.call"],
        "consented_tools": [name for name in names if name != "websearch.search"] + ["removed.call"],
        "tool_max_cost_usd": {name: "0.20" for name in [*names, "removed.call"] if name != "browser.close"},
    }
    policy["allowed_tools"].remove("websearch.operation_get")
    catalogue = {"tools": [{"name": name} for name in names]}
    report = await tools_access.tool_access_report(db, tenant_id="tenant-one", owner=owner, policy=policy, catalogue=catalogue)
    decisions = {tool["name"]: tool for tool in report["tools"]}
    assert {name: decisions[name]["reason"] for name in decisions} == {
        "browser.start": "tenant_disabled",
        "browser.step": None,
        "code_interpreter.start": "team_disabled",
        "websearch.search": "not_consented",
        "websearch.operation_get": "not_allowed",
        "browser.inspect": None,
        "browser.close": "cost_policy_missing",
        "removed.call": "undeployed",
    }
    assert report["policy_revision"] == 7
    assert decisions["browser.step"]["tenant_entitlement_revision"] == 1
    assert decisions["removed.call"]["tenant_entitlement_revision"] == 1
    assert decisions["removed.call"]["ready"] is False
    assert decisions["browser.step"]["ready"] is True
    assert decisions["browser.step"]["discovery_reason"] == "catalogue_stale"
    assert decisions["browser.step"]["transport"] == "tools_gateway"
    db.scalar.assert_not_awaited()


async def test_expired_dynamic_discovery_is_not_reported_as_undeployed(resolved_team):
    db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [grant("knowledge.search"), grant("removed.call")])))
    report = await tools_access.tool_access_report(
        db,
        tenant_id="tenant-one",
        owner=SimpleNamespace(id="owner-one", team_id="team-x"),
        policy=None,
        catalogue={
            "tools": [],
            "connectors": [{"name": "knowledge", "dynamic": True, "discovery_status": "unavailable"}],
        },
    )
    assert {tool["name"]: tool["reason"] for tool in report["tools"]} == {
        "knowledge.search": "discovery_unavailable",
        "removed.call": "undeployed",
    }
    assert all(tool["ready"] is False for tool in report["tools"])


@pytest.mark.asyncio
async def test_diagnostic_reports_disconnected_and_missing_oauth_scope(monkeypatch, resolved_team):
    monkeypatch.setattr(tools_access, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-oauth-user"))
    credential = SimpleNamespace(
        org_id="tenant-one",
        user_id="owner-one",
        team_id=None,
        domain_app_id=None,
        service="github",
        provider="agentcore-identity",
        credential_type="oauth_token",
        agent_consent=True,
        secret_arn="agentcore-identity:adp-oauth-user:github",
        scopes={"oauth_scopes": ["not-a-scope"]},
        expires_at=None,
    )
    db = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [grant("github.get_user")])),
        scalar=AsyncMock(return_value=None),
    )
    kwargs = dict(
        tenant_id="tenant-one",
        owner=SimpleNamespace(id="owner-one", team_id="team-x"),
        policy={
            "status": "active",
            "allowed_tools": ["github.get_user"],
            "consented_tools": ["github.get_user"],
            "tool_max_cost_usd": {"github.get_user": "0.10"},
            "catalog": [{"name": "github___get_user"}],
        },
        catalogue={"tools": [{"name": "github.get_user"}]},
    )
    disconnected = await tools_access.tool_access_report(db, **kwargs)
    assert disconnected["tools"][0]["reason"] == "disconnected"
    db.scalar.return_value = credential
    missing = await tools_access.tool_access_report(db, **kwargs)
    assert missing["tools"][0]["reason"] == "missing_scope"
    credential.scopes = {"oauth_scopes": ["read:user"]}
    connected = await tools_access.tool_access_report(db, **kwargs)
    assert connected["tools"][0] == {
        "name": "github.get_user",
        "transport": "tools_gateway",
        "ready": True,
        "reason": None,
        "discovery_reason": None,
        "max_cost_usd": "0.10",
        "tenant_entitlement_revision": 1,
        "team_entitlement_revision": 0,
    }


@pytest.mark.asyncio
async def test_invalid_policy_or_entitlement_failure_never_reports_ready(resolved_team):
    from sqlalchemy.exc import SQLAlchemyError

    owner = SimpleNamespace(id="owner-one", team_id="team-x")
    db = SimpleNamespace(scalars=AsyncMock(side_effect=SQLAlchemyError("failed")))
    with pytest.raises(HTTPException) as refused:
        await tools_access.tool_access_report(db, tenant_id="tenant-one", owner=owner, policy=None, catalogue={"tools": []})
    assert refused.value.status_code == 503
    with pytest.raises(HTTPException) as refused:
        await tools_access.tool_access_report(db, tenant_id="tenant-one", owner=owner, policy={"status": "inactive"}, catalogue={"tools": []})
    assert refused.value.status_code == 503
    with pytest.raises(HTTPException) as refused:
        await tools_access.tool_access_report(
            db, tenant_id="tenant-one", owner=owner, policy={"status": "active", "allowed_tools": [["browser.start"]]}, catalogue={"tools": []}
        )
    assert refused.value.status_code == 503


@pytest.mark.asyncio
async def test_unavailable_oauth_credential_read_is_not_reported_as_disconnected(monkeypatch, resolved_team):
    from sqlalchemy.exc import SQLAlchemyError

    monkeypatch.setattr(tools_access, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-oauth-user"))
    db = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [grant("github.get_user")])),
        scalar=AsyncMock(side_effect=SQLAlchemyError("database unavailable")),
    )
    with pytest.raises(HTTPException) as refused:
        await tools_access.tool_access_report(
            db,
            tenant_id="tenant-one",
            owner=SimpleNamespace(id="owner-one", team_id="team-x"),
            policy={
                "status": "active",
                "allowed_tools": ["github.get_user"],
                "consented_tools": ["github.get_user"],
                "tool_max_cost_usd": {"github.get_user": "0.10"},
            },
            catalogue={"tools": [{"name": "github.get_user"}]},
        )
    assert refused.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [SQLAlchemyError("database unavailable"), ValueError("ambiguous primary membership")])
async def test_unavailable_team_resolution_never_reports_ready(monkeypatch, failure):
    monkeypatch.setattr(tools_access, "primary_team_for_workspace", AsyncMock(side_effect=failure))
    db = SimpleNamespace(scalars=AsyncMock())
    with pytest.raises(HTTPException) as refused:
        await tools_access.tool_access_report(
            db, tenant_id="tenant-one", owner=SimpleNamespace(id="owner-one", team_id="team-x"), policy=None, catalogue={"tools": []}
        )
    assert refused.value.status_code == 503
    db.scalars.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_party_platform_tools_need_entitlement_and_consent_but_no_per_user_allow_or_cost(resolved_team):
    """#8113: the access report applies the implicit allow and the zero ceiling of first-party read-only tools."""
    db = SimpleNamespace(
        scalars=AsyncMock(
            return_value=SimpleNamespace(
                all=lambda: [
                    grant("platform.my_work_items"),
                    grant("platform.my_budget"),
                    grant("platform.my_flows"),
                    grant("platform.my_flows", team="team-x", enabled=False),
                ]
            )
        )
    )
    owner = SimpleNamespace(id="owner-one", team_id="team-x")
    catalogue = {
        "tools": [{"name": name} for name in ("platform.my_work_items", "platform.my_budget", "platform.my_flows", "platform.my_agent_runs")]
    }
    policy = {
        "status": "active",
        "allowed_tools": [],
        "consented_tools": ["platform.my_work_items", "platform.my_flows"],
        "tool_max_cost_usd": {},
        "catalog": [],
    }
    report = await tools_access.tool_access_report(db, tenant_id="tenant-one", owner=owner, policy=policy, catalogue=catalogue)
    by_name = {tool["name"]: tool for tool in report["tools"]}
    assert by_name["platform.my_work_items"]["ready"] is True and by_name["platform.my_work_items"]["max_cost_usd"] == "0"
    assert by_name["platform.my_work_items"]["discovery_reason"] is None
    assert all(tool["transport"] == "chat" for tool in report["tools"])
    assert by_name["platform.my_budget"] == {**by_name["platform.my_budget"], "ready": False, "reason": "not_consented"}
    assert by_name["platform.my_flows"]["reason"] == "team_disabled"
    assert by_name["platform.my_agent_runs"]["reason"] == "tenant_disabled"
    # No policy row at all: entitled first-party tools are still reported, waiting only on consent.
    report = await tools_access.tool_access_report(db, tenant_id="tenant-one", owner=owner, policy=None, catalogue=catalogue)
    assert {tool["name"]: tool["reason"] for tool in report["tools"]}["platform.my_work_items"] == "not_consented"


@pytest.mark.asyncio
async def test_jira_diagnostic_never_advertises_unqualified_direct_target(resolved_team):
    db = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [grant("jira.get_issue")])),
        scalar=AsyncMock(return_value=SimpleNamespace(scopes={"oauth_scopes": ["read:jira-work"]})),
    )
    report = await tools_access.tool_access_report(
        db,
        tenant_id="tenant-one",
        owner=SimpleNamespace(id="owner-one", team_id="team-x"),
        policy={
            "status": "active",
            "allowed_tools": ["jira.get_issue"],
            "consented_tools": ["jira.get_issue"],
            "tool_max_cost_usd": {"jira.get_issue": "0.10"},
            "catalog": [{"name": "jira___get_issue"}],
        },
        catalogue={"tools": [{"name": "jira.get_issue"}]},
    )
    assert report["tools"][0]["ready"] is False
    assert report["tools"][0]["reason"] == "jira_connection_unqualified"
    db.scalar.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scopes,reasons",
    [
        (["users:read"], {"slack.get_user": None, "slack.post_message": "missing_scope"}),
        (["users:read", "chat:write"], {"slack.get_user": None, "slack.post_message": None}),
        (["chat:write"], {"slack.get_user": "missing_scope", "slack.post_message": None}),
        (["users:read", "admin"], {"slack.get_user": "missing_scope", "slack.post_message": "missing_scope"}),
    ],
)
async def test_access_report_uses_required_subset_of_actual_slack_grant(monkeypatch, resolved_team, scopes, reasons):
    monkeypatch.setattr(tools_access, "get_settings", lambda: SimpleNamespace(oauth_workload_name="adp-oauth-user"))
    credential = SimpleNamespace(
        org_id="tenant-one",
        user_id="owner-one",
        team_id=None,
        domain_app_id=None,
        service="slack",
        provider="agentcore-identity",
        credential_type="oauth_token",
        agent_consent=True,
        secret_arn="agentcore-identity:adp-oauth-user:slack",
        scopes={"oauth_scopes": scopes},
        expires_at=None,
    )
    db = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [grant(name) for name in reasons])), scalar=AsyncMock(return_value=credential)
    )
    policy = {
        "status": "active",
        "allowed_tools": list(reasons),
        "consented_tools": list(reasons),
        "tool_max_cost_usd": {name: "0.05" for name in reasons},
        "catalog": [{"name": name.replace(".", "___")} for name in reasons],
    }
    args = dict(
        tenant_id="tenant-one",
        owner=SimpleNamespace(id="owner-one", team_id="team-x"),
        policy=policy,
        catalogue={"tools": [{"name": name} for name in reasons]},
    )
    report = await tools_access.tool_access_report(db, **args)
    assert {row["name"]: row["reason"] for row in report["tools"]} == reasons
    assert all(row["ready"] == (reasons[row["name"]] is None) for row in report["tools"])
    credential.agent_consent = False
    revoked = await tools_access.tool_access_report(db, **args)
    assert all(row["reason"] == "disconnected" and row["ready"] is False for row in revoked["tools"])
