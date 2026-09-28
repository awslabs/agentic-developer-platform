"""Real-ledger integration tests for the consolidated budget UI and its policy keys."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.auth.dependencies import get_current_user
from src.budget.overview_routes import router
from src.budget.person_cap_routes import router as cap_router
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.base import Base
from src.shared.models.budget import BudgetUsage, PersonBudgetDefault
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, Team, TeamMembership, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add_all(
            [
                Organization(id="eng", name="Engineering"),
                Organization(id="ops", name="Operations"),
                Organization(id="foreign", name="Foreign"),
                Team(id="platform", org_id="eng", department_id="", name="Platform"),
                Team(id="delivery", org_id="eng", department_id="", name="Delivery"),
                Team(id="support", org_id="ops", department_id="", name="Support"),
                User(id="alex", cognito_sub="sub-alex", name="Alex", email="alex@example.com", org_id="eng", team_id="platform"),
                User(id="admin", cognito_sub="sub-admin", name="Admin", email="admin@example.com", org_id="eng", team_id=""),
                TenantMembership(user_id="alex", tenant_id="ops", is_active=False),
                TeamMembership(user_id="alex", org_id="eng", team_id="delivery"),
                PersonBudgetDefault(
                    scope_type="platform",
                    period_type="monthly",
                    budget_amount_usd=Decimal("300"),
                    enforcement_mode="hard",
                    authored_by_user_id="admin",
                ),
                PersonBudgetDefault(
                    scope_type="org",
                    scope_id_org="eng",
                    period_type="monthly",
                    budget_amount_usd=Decimal("750"),
                    enforcement_mode="hard",
                    authored_by_user_id="admin",
                ),
                PersonBudgetDefault(
                    scope_type="team",
                    scope_id_org="eng",
                    scope_id_team="platform",
                    period_type="monthly",
                    budget_amount_usd=Decimal("500"),
                    enforcement_mode="hard",
                    authored_by_user_id="admin",
                ),
                PersonBudgetDefault(
                    scope_type="team",
                    scope_id_org="eng",
                    scope_id_team="delivery",
                    period_type="monthly",
                    budget_amount_usd=Decimal("400"),
                    enforcement_mode="hard",
                    authored_by_user_id="admin",
                ),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


def client(db, *, admin=False, org="eng"):
    app = FastAPI()
    app.include_router(router)
    app.include_router(cap_router)

    async def database():
        yield db

    @app.exception_handler(BedrockGatewayError)
    async def errors(request: Request, exc: BedrockGatewayError):
        return JSONResponse(status_code=exc.status_code, content={"message": exc.message})

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: TokenContext(
        user_id="sub-admin" if admin else "sub-alex",
        org_id=org,
        team_id="",
        department_id="",
        account_type="human",
        is_admin=admin,
        expires_at=date(2099, 1, 1),
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def usage(db, org, kind, user, amount, period, day):
    db.add(BudgetUsage(org_id=org, entity_type=kind, entity_id=user, total_cost_usd=Decimal(amount), period_type=period, period_start=day))
    await db.commit()


def descendants(node):
    return [node, *(descendant for child in node["children"] for descendant in descendants(child))]


async def test_daily_spend_reconciles_and_is_stable_across_workspaces(db):
    today = datetime.now(UTC).date()
    for period, day in [("daily", today), ("monthly", today.replace(day=1))]:
        await usage(db, "eng", "user", "sub-alex", "0.123456", period, day)
        await usage(db, "ops", "root_user", "alex", "10.654321", period, day)
        await usage(db, "foreign", "root_user", "alex", "999", period, day)
        await usage(db, "ops", "user", "worker", "999", period, day)
        await usage(db, "ops", "org", "ops", "999", period, day)
    async with client(db) as http:
        first = (await http.get("/me/budget/monthly-spend")).json()
    async with client(db, org="ops") as http:
        second = (await http.get("/me/budget/monthly-spend")).json()
    assert first["totals"] == second["totals"] == {"direct_usd": "0.123456", "cloud_usd": "10.654321", "total_usd": "10.777777"}
    assert first["daily_complete"] is True
    assert len(first["days"]) == today.day
    assert first["days"][0]["in_progress"] is True
    assert first["days"][0]["date"] == today.isoformat()
    assert sum(Decimal(d["total_usd"]) for d in first["days"]) == Decimal(first["totals"]["total_usd"])
    assert all(d["total_usd"] == "0.000000" for d in first["days"][1:])


async def test_missing_daily_data_is_unknown_not_invented(db):
    await usage(db, "eng", "user", "sub-alex", "12", "monthly", date.today().replace(day=1))
    async with client(db) as http:
        body = (await http.get("/me/budget/monthly-spend")).json()
    assert body["daily_complete"] is False
    assert body["days"] == []
    assert body["totals"]["total_usd"] == "12.000000"


@pytest.mark.parametrize("path", ["/budget/hierarchy", "/budget/people-spend"])
async def test_global_reads_deny_org_admin_and_member(db, path):
    async with client(db) as http:
        response = await http.get(path)
    assert response.status_code == 403
    assert "Alex" not in response.text


async def test_hierarchy_retains_parentage_and_empty_teams(db):
    async with client(db, admin=True) as http:
        response = await http.get("/budget/hierarchy")
    assert response.status_code == 200, response.text
    root = response.json()
    orgs = {n["name"]: n for n in root["children"]}
    assert {n["name"] for n in orgs["Engineering"]["children"] if n["kind"] == "team"} == {"Platform", "Delivery"}
    support = orgs["Operations"]["children"][0]
    assert support["name"] == "Support" and support["children"] == []
    assert support["effective"]["amount_usd"] == "300.00"
    alex = [n for n in descendants(root) if n["name"] == "Alex"]
    assert len(alex) == 3  # both Engineering teams and unassigned in Operations
    assert all(n["effective"]["amount_usd"] == "400.00" for n in alex)
    assert len({n["person_anchor"] for n in alex}) == 1


async def test_native_override_upward_and_removal_match_enforcement(db):
    from unittest.mock import AsyncMock

    from src.budget.enforcement_service import BudgetEnforcementService

    # Both branches of the enforcement source gate must honor a native individual cap.
    async with client(db, admin=True) as http:
        response = await http.put("/budget/person-cap/users:alex?period_type=monthly", json={"budget_amount_usd": "1000.00"})
        assert response.status_code == 200, response.text
    async with client(db) as http:
        assert (await http.get("/me/budget/person-cap")).json()["cap_usd"] == "1000.00"
    service = BudgetEnforcementService.__new__(BudgetEnforcementService)
    context = TokenContext(user_id="sub-alex", org_id="eng", team_id="platform", department_id="", account_type="human", expires_at=date(2099, 1, 1))
    for defaults_exist in [True, False]:
        service._person_limit_sources_exist = AsyncMock(return_value=(True, defaults_exist))
        resolved = await service._resolve_person_limits(db, context)
        assert resolved[0]["monthly"].amount == Decimal("1000")
        assert resolved[1] == "users:alex"
        context.attributed_user_id = "alex"
        resolved = await service._resolve_person_limits(db, context)
        assert resolved[0]["monthly"].amount == Decimal("1000")
        context.attributed_user_id = None
    async with client(db, admin=True) as http:
        assert (await http.delete("/budget/person-cap/users:alex")).status_code == 204
    async with client(db) as http:
        assert (await http.get("/me/budget/person-cap")).json()["cap_usd"] == "400.00"


async def test_people_pagination_deduplicates_linked_identities(db):
    db.add(User(id="alex-ops", cognito_sub="sub-alex-ops", name="Alex ops", email="alex-ops@example.com", org_id="ops", team_id="support"))
    for uid, org in [("alex", "eng"), ("alex-ops", "ops")]:
        db.add(UserIdentity(user_id=uid, org_id=org, team_id="", provider="github", provider_user_id="123", verification_method="oauth"))
    await db.commit()
    await usage(db, "ops", "user", "sub-alex-ops", "15", "monthly", date.today().replace(day=1))
    async with client(db, admin=True) as http:
        response = await http.get("/budget/people-spend?search=Alex&page_size=1")
        body = response.json()
        assert body["total"] == 1 and len(body["items"]) == 1
        assert body["items"][0]["spend"]["total_usd"] == "15.000000"
        assert (await http.get("/budget/people-spend?search=Alex&page_size=1&page=2")).json()["items"] == []


async def test_removal_walks_team_org_platform_then_no_budget(db):
    async with client(db, admin=True) as http:
        assert (await http.delete("/budget/person-default/team:eng:delivery")).status_code == 204
        assert (await http.delete("/budget/person-default/team:eng:platform")).status_code == 204
    async with client(db) as http:
        cap = (await http.get("/me/budget/person-cap")).json()
        assert cap["cap_usd"] == "750.00" and cap["source"] == "org_default"
    async with client(db, admin=True) as http:
        assert (await http.delete("/budget/person-default/org:eng")).status_code == 204
    async with client(db) as http:
        cap = (await http.get("/me/budget/person-cap")).json()
        assert cap["cap_usd"] == "300.00" and cap["source"] == "platform_default"
    async with client(db, admin=True) as http:
        assert (await http.delete("/budget/person-default/platform")).status_code == 204
    async with client(db) as http:
        cap = (await http.get("/me/budget/person-cap")).json()
        assert cap["cap_usd"] is None and cap["cap_status"] == "uncapped"
