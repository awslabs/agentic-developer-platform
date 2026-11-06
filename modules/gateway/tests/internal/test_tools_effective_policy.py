"""#8387: one resolver turns the stored row plus the tenant posture into the effective lists."""

from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.internal import tools_effective_policy as resolver
from src.shared.models.organization import Organization
from src.shared.models.tool_entitlement import ToolEntitlement

TENANT, OWNER, TEAM = "tenant-one", "owner-one", "team-one"


@pytest.fixture
async def db(monkeypatch):
    monkeypatch.delenv("ADP_TOOLS_CONSENT_DEFAULT", raising=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for model in (Organization, ToolEntitlement):
            await connection.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add_all(
            [
                Organization(id=TENANT, name="One"),
                ToolEntitlement(org_id=TENANT, team_id="", tool_name="websearch.search", enabled=True, max_cost_usd=Decimal("0.50")),
                ToolEntitlement(org_id=TENANT, team_id="", tool_name="browser.start", enabled=True),
                ToolEntitlement(org_id=TENANT, team_id="", tool_name="code_interpreter.start", enabled=True),
                ToolEntitlement(org_id=TENANT, team_id=TEAM, tool_name="code_interpreter.start", enabled=False),
                ToolEntitlement(org_id=TENANT, team_id="", tool_name="platform.my_flows", enabled=True),
                ToolEntitlement(org_id=TENANT, team_id="", tool_name="slack.post_message", enabled=False),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


async def resolve(db, policy, **kwargs):
    return await resolver.effective_tools_policy(db, policy=policy, tenant_id=TENANT, owner_id=OWNER, team_id=TEAM, **kwargs)


@pytest.mark.asyncio
async def test_no_row_under_the_granted_posture_consents_every_entitled_tool(db):
    """AC-01: entitlement is the default consent; the team's disabled tool and the tenant's disabled tool stay out."""
    effective = await resolve(db, None)
    assert effective.consent_mode == "default" and effective.tenant_consent_default == "granted"
    assert effective.allowed_tools == ["browser.start", "platform.my_flows", "websearch.search"]
    assert effective.consented_tools == ["browser.start", "platform.my_flows", "websearch.search"]
    assert {name: Decimal(cost) for name, cost in effective.tool_max_cost_usd.items()} == {"websearch.search": Decimal("0.50")}  # entitlement ceiling
    assert effective.consent_source("websearch.search") == "default"
    assert effective.revision == 0 and effective.opted_out_tools == []


@pytest.mark.asyncio
async def test_admin_allowed_but_unentitled_tool_is_never_consented_by_default(db):
    """AC-01: the façade refuses it as tool_not_entitled first; the report must not call it consented."""
    row = {
        "status": "active",
        "allowed_tools": ["jira.get_issue", "websearch.search"],
        "consented_tools": [],
        "tool_max_cost_usd": {"websearch.search": "0.05"},
        "revision": 2,
    }
    effective = await resolve(db, row)
    assert effective.consent_mode == "default"
    assert effective.allowed_tools == ["jira.get_issue", "websearch.search", "browser.start", "platform.my_flows"]
    assert effective.consented_tools == ["websearch.search", "browser.start", "platform.my_flows"]
    assert effective.tool_max_cost_usd["websearch.search"] == "0.05"  # the per-user ceiling wins
    assert effective.revision == 2


@pytest.mark.asyncio
async def test_opt_outs_remove_only_the_named_tools(db):
    """AC-02."""
    row = {
        "status": "active",
        "allowed_tools": [],
        "consented_tools": [],
        "consent_mode": "default",
        "opted_out_tools": ["browser.start", "not.entitled"],
        "revision": 1,
    }
    effective = await resolve(db, row)
    assert effective.consented_tools == ["platform.my_flows", "websearch.search"]
    assert effective.opted_out_tools == ["browser.start"]
    assert "browser.start" in effective.allowed_tools


@pytest.mark.asyncio
async def test_legacy_row_with_a_chosen_consent_list_stays_explicit(db):
    """AC-03: a non-empty consent list without consent_mode is the owner's decision and is kept verbatim."""
    row = {"status": "active", "allowed_tools": ["websearch.search", "browser.start"], "consented_tools": ["websearch.search"], "revision": 5}
    effective = await resolve(db, row)
    assert effective.consent_mode == "explicit"
    assert effective.consented_tools == ["websearch.search"]
    assert effective.allowed_tools == ["websearch.search", "browser.start", "platform.my_flows"]  # #8113 implicit first-party tools
    assert effective.consent_source("websearch.search") == "explicit"
    facade = await resolve(db, row, include_platform=False)
    assert facade.allowed_tools == ["websearch.search", "browser.start"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["tenant", "platform"])
async def test_explicit_posture_forces_opt_in(db, monkeypatch, source):
    """AC-04: the tenant override or the platform flag restores the pre-#8387 behaviour for a default-mode row."""
    if source == "tenant":
        org = await db.get(Organization, TENANT)
        org.settings = {"tools": {"consent_default": "explicit"}}
        await db.commit()
    else:
        monkeypatch.setenv("ADP_TOOLS_CONSENT_DEFAULT", "explicit")
    effective = await resolve(db, None)
    assert effective.consent_mode == "explicit" and effective.tenant_consent_default == "explicit"
    assert effective.consented_tools == []
    assert effective.allowed_tools == ["platform.my_flows"]


@pytest.mark.asyncio
async def test_invalid_posture_values_fail_closed(db, monkeypatch):
    monkeypatch.setenv("ADP_TOOLS_CONSENT_DEFAULT", "yes-please")
    assert resolver.platform_consent_default() == "explicit"
    assert resolver.tenant_consent_default({"tools": {"consent_default": "whatever"}}) == "explicit"
    monkeypatch.setenv("ADP_TOOLS_CONSENT_DEFAULT", "granted")
    assert resolver.tenant_consent_default({"tools": {"consent_default": "explicit"}}) == "explicit"
    assert resolver.tenant_consent_default({"tools": "broken"}) == "granted"
    assert resolver.tenant_consent_default(None) == "granted"


@pytest.mark.asyncio
async def test_malformed_rows_are_refused_not_widened(db):
    for row in (
        {"status": "disabled", "allowed_tools": []},
        {"status": "active", "allowed_tools": "websearch.search"},
        {"status": "active", "allowed_tools": [], "consent_mode": "maybe"},
        {"status": "active", "allowed_tools": [], "opted_out_tools": [1]},
    ):
        with pytest.raises(HTTPException) as refused:
            await resolve(db, row)
        assert refused.value.status_code == 503


@pytest.mark.asyncio
async def test_as_policy_keeps_the_row_shape_consumers_read(db):
    effective = await resolve(db, {"status": "active", "allowed_tools": [], "consented_tools": [], "catalog": [{"name": "x"}], "revision": 1})
    policy = effective.as_policy()
    assert policy["status"] == "active" and policy["catalog"] == [{"name": "x"}] and policy["revision"] == 1
    assert policy["consent_mode"] == "default" and policy["stored_allowed_tools"] == []
    assert set(policy["consented_tools"]) == {"browser.start", "platform.my_flows", "websearch.search"}
