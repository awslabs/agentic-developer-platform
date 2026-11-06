"""First-party platform tools on the chat data plane are governed by the tools layer and scoped to the launch (#8113)."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.activity.routes import get_activity_service
from src.activity.schemas import InvocationItem, InvocationListResponse
from src.agentauth import chat_data_routes, chat_platform_tools
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.shared.database import get_db
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, Team, TeamMembership, User
from src.shared.models.tool_entitlement import ToolEntitlement
from src.shared.models.vault import UserIdentity
from tests.agentauth.chat_activity_fixtures import HEADERS, workload_runtime

LIST = "/v1/chat/data/tools/list"
CALL = "/v1/chat/data/tools/call"
TENANT, OWNER, TEAM = "tenant-a", "owner-a", "team-a"
PLATFORM = ["platform.my_agent_runs", "platform.my_budget", "platform.my_flows", "platform.my_work_items"]


def launch(**changes):
    return SimpleNamespace(**{"run_id": "run-a", "tenant_id": TENANT, "user_id": OWNER, "team_id": TEAM, "attempt": 1, **changes})


NOW = 1_792_000_000


@pytest.fixture
async def database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for model in (Organization, User, Team, TeamMembership, TenantMembership, ToolEntitlement, UserIdentity):
            await connection.run_sync(model.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=TENANT, name="Tenant A", github_installation_ids=["4242"]),
                Organization(id="tenant-b", name="Tenant B", github_installation_ids=["9999"]),
                Team(id=TEAM, org_id=TENANT, department_id="dept", name="A"),
                User(id=OWNER, org_id=TENANT, team_id=TEAM, email="owner@example.test", cognito_sub="sub-owner"),
                User(id="victim", org_id=TENANT, team_id=TEAM, email="victim@example.test", cognito_sub="sub-victim"),
                User(id="outsider", org_id="tenant-b", team_id="team-b", email="outsider@example.test"),
                UserIdentity(
                    user_id=OWNER,
                    org_id=TENANT,
                    team_id=TEAM,
                    provider="github",
                    provider_user_id="1001",
                    provider_username="octocat",
                    verification_method="oauth",
                ),
                UserIdentity(
                    user_id="victim",
                    org_id=TENANT,
                    team_id=TEAM,
                    provider="github",
                    provider_user_id="1002",
                    provider_username="victim-gh",
                    verification_method="oauth",
                ),
                *[ToolEntitlement(org_id=TENANT, team_id="", tool_name=name, enabled=True) for name in PLATFORM if name != "platform.my_flows"],
            ]
        )
        await db.commit()
        yield sessions
    await engine.dispose()


@pytest.fixture
def api(monkeypatch, database):
    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "true")
    monkeypatch.setattr(chat_data_routes, "clock", lambda: NOW)
    capabilities = MagicMock()
    capabilities.verify_run.return_value = launch()
    services = workload_runtime(capabilities)
    services = (SimpleNamespace(verify_workload=services[0].verify_workload, store=SimpleNamespace(table="authority", client=object())), services[1])
    policy = {
        "status": "active",
        "allowed_tools": ["websearch.search"],
        "consented_tools": ["platform.my_agent_runs", "platform.my_work_items", "platform.my_flows", "websearch.search"],
        "tool_max_cost_usd": {"websearch.search": "0.05"},
        "catalog": [],
        "revision": 4,
    }
    policy_read = MagicMock(return_value=policy)
    monkeypatch.setattr(chat_platform_tools, "ToolsPolicyStore", lambda **kwargs: SimpleNamespace(get=policy_read))
    activity = MagicMock()
    activity.query_by_user.return_value = InvocationListResponse(
        items=[
            InvocationItem(
                invocation_id="inv-1",
                invoked_at="2026-10-02T08:00:00Z",
                status="completed",
                persona="developer",
                repo="acme/widgets",
                issue_number=14,
            )
        ],
        count=1,
        last_key="more",
    )
    app = FastAPI()
    app.include_router(chat_data_routes.router)
    app.dependency_overrides[chat_data_routes.runtime] = lambda: services

    async def db_dependency():
        async with database() as db:
            yield db

    app.dependency_overrides[get_db] = db_dependency
    app.dependency_overrides[get_activity_service] = lambda: activity
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, capabilities=capabilities, policy=policy, policy_read=policy_read, activity=activity)


def test_listing_is_catalogue_entitlement_consent_for_the_launch_principal_only(api):
    response = api.client.post(LIST, headers=HEADERS, json={"run_id": "run-a"})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    # my_flows is consented but not entitled by the tenant: not offered and not revealed.
    assert [tool["name"] for tool in body["tools"]] == ["platform___my_agent_runs", "platform___my_work_items"]
    for tool in body["tools"]:
        assert tool["kind"] == "first_party" and tool["read_only"] is True and tool["inputSchema"]["additionalProperties"] is False
    # my_budget is entitled but not consented: named so the assistant can point at Settings → Tools.
    assert body["withheld"] == [{"name": "platform___my_budget", "reason": "not_consented"}]
    # The consented MCP tool cannot run from the default turn (façade needs a Crew binding) and says so.
    assert body["coverage"] == [
        {"source": "tools.platform", "status": "available", "reason": None},
        {"source": "tools.mcp", "status": "unavailable", "reason": "crew_binding_required"},
    ]
    assert body["policy_revision"] == 4
    api.capabilities.verify_run.assert_called_with("delegated", run_id="run-a", operation="tools.invoke", now=NOW)
    api.policy_read.assert_called_with(tenant_id=TENANT, owner_id=OWNER)
    # Identity comes from the verified launch, never the body.
    assert api.client.post(LIST, headers=HEADERS, json={"run_id": "run-a", "user_id": "victim"}).status_code == 422
    api.capabilities.verify_run.side_effect = ChatAuthorizationRefusedError("scope refused")
    assert api.client.post(LIST, headers=HEADERS, json={"run_id": "forged"}).status_code == 404
    api.capabilities.verify_run.side_effect = None
    assert api.client.post(LIST, headers={"Authorization": "Bearer delegated"}, json={"run_id": "run-a"}).status_code == 404


def test_listing_without_any_policy_offers_every_entitled_tool_under_the_default_posture(api):
    """#8387: tenant entitlement is the owner's consent until they opt out or the tenant requires opt-in."""
    api.policy_read.return_value = None
    body = api.client.post(LIST, headers=HEADERS, json={"run_id": "run-a"}).json()
    assert {tool["name"] for tool in body["tools"]} == {"platform___my_agent_runs", "platform___my_budget", "platform___my_work_items"}
    assert body["withheld"] == [] and body["policy_revision"] == 0
    assert body["coverage"] == [{"source": "tools.platform", "status": "available", "reason": None}]


def test_listing_without_any_policy_offers_nothing_but_names_entitled_tools_as_withheld(api, monkeypatch):
    """A tenant that requires explicit consent keeps the pre-#8387 behaviour."""
    from src.internal import tools_effective_policy

    monkeypatch.setattr(tools_effective_policy, "resolve_tenant_consent_default", AsyncMock(return_value="explicit"))
    api.policy_read.return_value = None
    body = api.client.post(LIST, headers=HEADERS, json={"run_id": "run-a"}).json()
    assert body["tools"] == [] and body["policy_revision"] == 0
    assert {entry["name"] for entry in body["withheld"]} == {"platform___my_agent_runs", "platform___my_budget", "platform___my_work_items"}
    assert body["coverage"] == [{"source": "tools.platform", "status": "empty", "reason": "no_consented_platform_tools"}]


def test_launch_for_a_non_member_is_refused(api):
    api.capabilities.verify_run.return_value = launch(user_id="outsider")
    assert api.client.post(LIST, headers=HEADERS, json={"run_id": "run-a"}).status_code == 404


def test_call_runs_only_offered_tools_under_the_launch_identity_and_records_the_call(api):
    call = {"run_id": "run-a", "tool": "platform___my_agent_runs", "arguments": {"limit": 5}, "request_id": "req-1"}
    response = api.client.post(CALL, headers=HEADERS, json=call)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tool"] == "platform___my_agent_runs" and body["status"] == "confirmed" and body["request_id"] == "req-1"
    result = body["result"]
    assert result["source"] == "activity.agent_runs" and result["scope"] == "owner" and result["has_more"] is True
    assert result["coverage"] == [{"source": "activity.agent_runs", "status": "available"}]
    assert result["runs"][0] == {
        "invocation_id": "inv-1",
        "invoked_at": "2026-10-02T08:00:00Z",
        "status": "completed",
        "persona": "developer",
        "channel": None,
        "topic": None,
        "summary": None,
        "repo": "acme/widgets",
        "issue_number": 14,
        "trigger_kind": "human",
        "source_url": None,
        "url": "/me/agent-invocations/inv-1",
    }
    window = [datetime.fromisoformat(result["window"][edge].replace("Z", "+00:00")) for edge in ("from", "to")]
    assert window[0] < window[1]
    # Scoped to the launch principal, never to a body-named user.
    assert api.activity.query_by_user.call_args.args == (OWNER,)
    assert api.activity.query_by_user.call_args.kwargs["tenant_id"] == TENANT
    assert api.activity.query_by_user.call_args.kwargs["page_size"] == 5
    recorded = api.activity.record_tool_call.call_args.kwargs
    assert recorded["user_id"] == OWNER and recorded["tenant_id"] == TENANT and recorded["tool"] == "platform___my_agent_runs"
    assert recorded["estimated_cost_usd"] == Decimal(0) and recorded["run_id"] == "run-a" and recorded["team_id"] == TEAM
    assert api.client.post(CALL, headers=HEADERS, json={**call, "user_id": "victim"}).status_code == 422
    # Not consented → refused with the reason; not entitled → refused without revealing why it is absent.
    refused = api.client.post(CALL, headers=HEADERS, json={**call, "tool": "platform___my_budget"})
    assert refused.status_code == 403 and refused.json()["detail"] == {"error": "tool_not_consented"}
    refused = api.client.post(CALL, headers=HEADERS, json={**call, "tool": "platform___my_flows"})
    assert refused.status_code == 403 and refused.json()["detail"] == {"error": "tool_not_entitled"}
    refused = api.client.post(CALL, headers=HEADERS, json={**call, "tool": "websearch___search"})
    assert refused.status_code == 403 and refused.json()["detail"] == {"error": "tool_not_entitled"}
    assert api.client.post(CALL, headers=HEADERS, json={**call, "arguments": {"limit": 500}}).status_code == 422
    assert api.client.post(CALL, headers=HEADERS, json={**call, "arguments": {"tenant_id": "tenant-b"}}).status_code == 422
    assert api.activity.record_tool_call.call_count == 1


def test_consent_withdrawn_between_listing_and_call_is_refused(api):
    api.policy["consented_tools"].remove("platform.my_agent_runs")
    call = {"run_id": "run-a", "tool": "platform___my_agent_runs", "arguments": {}, "request_id": "req-2"}
    refused = api.client.post(CALL, headers=HEADERS, json=call)
    assert refused.status_code == 403 and refused.json()["detail"] == {"error": "tool_not_consented"}
    api.activity.query_by_user.assert_not_called()


def test_work_items_use_the_installation_app_with_read_permissions_and_the_owners_proven_login(api, monkeypatch):
    from src.knowledge import github_app_service

    monkeypatch.setattr(github_app_service, "resolve_tenant_app_credentials", AsyncMock(return_value=("app-1", "pem")))
    minted = AsyncMock(return_value=("ghs_token", "2026-10-02T09:00:00Z"))
    monkeypatch.setattr(github_app_service, "mint_installation_token_with_expiry", minted)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer ghs_token"
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "total_count": 2,
                    "repositories": [
                        {"full_name": "acme/widgets", "owner": {"login": "acme"}},
                        {"full_name": "acme/gadgets", "owner": {"login": "acme"}},
                    ],
                },
            )
        assert request.url.path == "/search/issues"
        return httpx.Response(
            200,
            json={
                "total_count": 3,
                "items": [
                    {
                        "number": 14,
                        "title": "Story: ship the widget",
                        "state": "open",
                        "html_url": "https://github.com/acme/widgets/issues/14",
                        "repository_url": "https://api.github.com/repos/acme/widgets",
                        "user": {"login": "someone"},
                        "assignees": [{"login": "octocat"}],
                        "labels": [{"name": "story"}],
                        "updated_at": "2026-10-01T10:00:00Z",
                    },
                    {
                        "number": 7,
                        "title": "Fix gadget",
                        "state": "open",
                        "draft": True,
                        "html_url": "https://github.com/acme/gadgets/pull/7",
                        "repository_url": "https://api.github.com/repos/acme/gadgets",
                        "user": {"login": "octocat"},
                        "assignees": [],
                        "pull_request": {},
                        "labels": [],
                        "updated_at": "2026-10-01T09:00:00Z",
                    },
                    {
                        "number": 1,
                        "title": "Outside the installation",
                        "state": "open",
                        "html_url": "https://github.com/other/repo/issues/1",
                        "repository_url": "https://api.github.com/repos/other/repo",
                        "user": {"login": "octocat"},
                        "assignees": [],
                        "updated_at": "2026-10-01T08:00:00Z",
                    },
                ],
            },
        )

    monkeypatch.setattr(
        chat_platform_tools, "github_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    )
    call = {"run_id": "run-a", "tool": "platform___my_work_items", "arguments": {"state": "open", "kind": "all", "limit": 10}, "request_id": "req-3"}
    response = api.client.post(CALL, headers=HEADERS, json=call)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["source"] == "github.work_items" and result["login"] == "octocat"
    assert result["coverage"] == [{"source": "github.work_items", "status": "available"}]
    assert [(item["kind"], item["repo"], item["number"], item["relation"], item["url"]) for item in result["items"]] == [
        ("issue", "acme/widgets", 14, "assigned", "https://github.com/acme/widgets/issues/14"),
        ("pull_request", "acme/gadgets", 7, "authored", "https://github.com/acme/gadgets/pull/7"),
    ]
    assert result["items"][1]["draft"] is True and result["has_more"] is True and result["total"] == 3
    # One installation token, read-only permissions, installation-wide (no caller-chosen repository), for the tenant's single installation.
    minted.assert_awaited_once()
    assert minted.call_args.args == ("app-1", "pem", 4242)
    assert minted.call_args.kwargs == {"permissions": {"issues": "read", "pull_requests": "read", "metadata": "read"}}
    search = dict(requests[1].url.params)
    assert search["q"] == "involves:octocat updated:>=" + search["q"].split("updated:>=")[1].split(" ")[0] + " user:acme is:open"
    assert "victim-gh" not in search["q"] and search["per_page"] == "10" and search["sort"] == "updated"


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("no_identity", ("unavailable", "github_identity_unlinked")),
        ("two_installations", ("unavailable", "installation_unresolved")),
        ("credentials", ("unavailable", "app_credentials_unavailable")),
        ("token_403", ("denied", "installation_token_refused")),
        ("search_403", ("denied", "search_refused")),
        ("search_500", ("unavailable", "provider_failure")),
    ],
)
async def test_work_items_report_every_gap_as_coverage_rather_than_an_empty_answer(api, database, monkeypatch, fault, expected):
    from sqlalchemy import delete, update

    from src.knowledge import github_app_service

    async with database() as db:
        if fault == "no_identity":
            await db.execute(delete(UserIdentity).where(UserIdentity.user_id == OWNER))
        if fault == "two_installations":
            await db.execute(update(Organization).where(Organization.id == TENANT).values(github_installation_ids=["1", "2"]))
        await db.commit()
    credentials = AsyncMock(side_effect=ValueError("missing") if fault == "credentials" else None, return_value=("app-1", "pem"))
    monkeypatch.setattr(github_app_service, "resolve_tenant_app_credentials", credentials)
    token_error = httpx.HTTPStatusError("forbidden", request=httpx.Request("POST", "https://api.github.test"), response=httpx.Response(403))
    monkeypatch.setattr(
        github_app_service,
        "mint_installation_token_with_expiry",
        AsyncMock(side_effect=token_error if fault == "token_403" else None, return_value=("ghs_token", "later")),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/installation/repositories":
            return httpx.Response(200, json={"total_count": 1, "repositories": [{"full_name": "acme/widgets", "owner": {"login": "acme"}}]})
        return httpx.Response(403 if fault == "search_403" else 500, json={"message": "nope"})

    monkeypatch.setattr(
        chat_platform_tools, "github_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    )
    call = {"run_id": "run-a", "tool": "platform___my_work_items", "arguments": {}, "request_id": "req-4"}
    response = api.client.post(CALL, headers=HEADERS, json=call)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["items"] == [] and result["has_more"] is False
    assert result["coverage"] == [{"source": "github.work_items", "status": expected[0], "reason": expected[1]}]


def test_budget_is_read_for_a_principal_built_from_the_launch_and_outages_are_coverage(api, monkeypatch):
    from src.budget import me_routes

    api.policy["consented_tools"].append("platform.my_budget")
    seen = {}

    async def fake_budget(*, current_user, db, period_type):
        seen.update(user_id=current_user.user_id, org_id=current_user.org_id, team_id=current_user.team_id, period_type=period_type)
        return SimpleNamespace(
            model_dump=lambda mode: {
                "period": {"period_type": period_type},
                "entity_type": "user",
                "cap_usd": "10.00",
                "spend_usd": "2.50",
                "remaining_usd": "7.50",
                "utilization_pct": 25.0,
                "band": "ok",
                "cap_status": "capped",
                "enforcement_mode": "hard",
                "identity_status": "resolved",
                "freshness": {"cost_backfill_lag": False},
                "lines": [
                    {"entity_type": "user", "spend_usd": "2.50", "cap_usd": "10.00", "remaining_usd": "7.50", "cap_status": "capped", "secret": 1}
                ],
                "per_org": [{"org": "x"}],
            }
        )

    monkeypatch.setattr(me_routes, "get_my_budget", fake_budget)
    call = {"run_id": "run-a", "tool": "platform___my_budget", "arguments": {"period_type": "weekly"}, "request_id": "req-5"}
    result = api.client.post(CALL, headers=HEADERS, json=call).json()["result"]
    assert seen == {"user_id": "sub-owner", "org_id": TENANT, "team_id": TEAM, "period_type": "weekly"}
    assert result["coverage"] == [{"source": "budget.me", "status": "available"}]
    assert result["budget"]["cap_usd"] == "10.00" and result["budget"]["lines"] == [
        {"entity_type": "user", "spend_usd": "2.50", "cap_usd": "10.00", "remaining_usd": "7.50", "cap_status": "capped"}
    ]
    assert "per_org" not in result["budget"]

    async def outage(**kwargs):
        raise HTTPException(503, "ledger unavailable")

    monkeypatch.setattr(me_routes, "get_my_budget", outage)
    result = api.client.post(CALL, headers=HEADERS, json=call).json()["result"]
    assert result["budget"] is None and result["coverage"] == [{"source": "budget.me", "status": "unavailable", "reason": "http_503"}]


async def test_flows_are_the_tenants_flows_most_recently_updated_with_derived_status(api, database, monkeypatch):
    from src.orchestration import repository
    from src.orchestration.display_state import FlowStatus

    async with database() as db:
        db.add(ToolEntitlement(org_id=TENANT, team_id="", tool_name="platform.my_flows", enabled=True))
        await db.commit()
    calls = []

    async def page(self, **kwargs):
        calls.append(kwargs)
        flow = SimpleNamespace(
            id="flow-1",
            slug="ship-widgets",
            title="Ship widgets",
            execution_paused=False,
            archived_at=None,
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
            updated_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
        aggregate = SimpleNamespace(
            flow=flow,
            status=FlowStatus.AWAITING_YOU,
            display_counts=repository.FlowDisplayCounts(queued=1, in_progress=2, gate=1, stalled=0, complete=3),
            stalled_count=0,
            story_count=5,
            gate_count=1,
            completed_story_count=3,
        )
        return SimpleNamespace(flows=(aggregate,), total=7)

    monkeypatch.setattr(repository.OrchestrationRepository, "list_flows_page_with_aggregates", page)
    call = {"run_id": "run-a", "tool": "platform___my_flows", "arguments": {"limit": 1, "status": "awaiting_you"}, "request_id": "req-6"}
    response = api.client.post(CALL, headers=HEADERS, json=call)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert calls == [{"org_id": TENANT, "limit": 1, "status": FlowStatus.AWAITING_YOU, "include_archived": False, "sort": "updated"}]
    assert result["source"] == "orchestration.flows" and result["scope"] == "tenant" and result["has_more"] is True and result["total"] == 7
    assert result["flows"] == [
        {
            "id": "flow-1",
            "slug": "ship-widgets",
            "title": "Ship widgets",
            "status": "awaiting_you",
            "execution_paused": False,
            "archived": False,
            "stories": 5,
            "stories_complete": 3,
            "gates": 1,
            "attention_needed": 0,
            "nodes": {"queued": 1, "in_progress": 2, "gate": 1, "stalled": 0, "complete": 3},
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-10-01T00:00:00Z",
            "url": "/orchestration/flows/flow-1",
        }
    ]
    assert json.dumps(result).count("flow-1") == 2
