"""Non-calendar `budget_configs` rows must not break the calendar readers — Issue #4328.

#4301 (closing #4187) added per-run and per-chain spend caps with **no new tables**:
a run cap is an ordinary `budget_configs` row with `entity_type="run"|"chain"` and
`period_type="run"`. To keep that safe it also made `get_period_start_end` **raise**
for `PeriodType.RUN`, because a run cap is lifetime-scoped and silently returning
today's date would make every run on a given day share one reservation key.

The raise is correct and load-bearing. The defect was blast radius: `PeriodType.RUN`
went into a shared enum whose values are read back **out of the database**, and the
pre-existing readers iterate `budget_configs` with no `period_type` filter. A single
run-scoped row therefore 500'd the org's budget pages for every user in the tenant —
and merely *configuring* a cap was enough, so the feature's off-switch did not help.

**This suite covers four distinct defects, which is the point.** The issue as filed
described one (`ValueError` on alerts) and prescribed a two-line fix. Reproduction
showed the headline endpoint fails a *different* way, before it ever reaches the
period math, plus two more defects that pre-date #4301 entirely:

  A  get_budget_alerts                 -> ValueError  (the one that was filed)
  B  get_organization_budget_overview  -> KeyError    (dies at the entities dict)
  B' get_budget_summary                -> ValueError  (a third, uncatalogued reader)
  C  total_spend_current_month         -> 2x-4x over-report (unfiltered SUM)

Bug B is **not** limited to run caps and is not #4301's fault: `entity_type="agent"`
rows have been written since #249, and `root_user` rows since #4300. Both already
crashed the overview endpoint. That is why `test_overview_survives_every_entity_type`
is parameterised over **every** `EntityType` member rather than just the new ones —
it is the acceptance gate, so the next member added to the enum breaks a test instead
of production.

Every test here fails on `main` @ `87c5eec`. A test that passes before the fix is not
testing the defect.

Harness is the sqlite one from `test_service.py` (#4328 touches only read
predicates, so no Redis/Lua is involved). Rows are inserted as raw `BudgetConfig`
models rather than through `create_budget`, deliberately: the point is that these
values are already in the database, however they got there.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.admin.service import AdminService
from src.budget.routes import get_budget_service, get_current_user
from src.budget.routes import router as budget_router
from src.budget.service import BudgetService
from src.budget.utils import CALENDAR_PERIOD_TYPES, get_period_start_end
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.schemas.budget import EntityType, PeriodType

ORG_ID = "org-4328"


@pytest.fixture
async def async_session():
    """In-memory SQLite session (same harness as tests/budget/test_service.py)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def budget_service(async_session):
    return BudgetService(async_session)


@pytest.fixture
def admin_service(async_session):
    return AdminService(async_session)


@pytest.fixture
def app(budget_service, async_session):
    """FastAPI app wired to a REAL BudgetService over the sqlite session.

    Deliberately not the mocked service from test_routes.py: a mock returns a dict
    the test itself wrote, so it asserts 200 no matter how broken the reader is. The
    500s in this issue were raised inside the service and surfaced through the route,
    so the route assertions only mean something against real code.
    """
    app = FastAPI()
    app.include_router(budget_router)
    app.dependency_overrides[get_budget_service] = lambda: budget_service
    app.dependency_overrides[get_db] = lambda: async_session
    app.dependency_overrides[get_current_user] = lambda: MagicMock(
        user_id="user-1",
        org_id=ORG_ID,
        team_id="team-1",
        department_id="dept-1",
        account_type="human",
        is_admin=True,  # Individual agent/run budgets require operator authority.
        cognito_username="",
    )
    return app


async def seed_config(
    session: AsyncSession,
    entity_type: str,
    entity_id: str,
    period_type: str,
    amount: str = "50.00",
) -> BudgetConfig:
    """Insert a budget_configs row directly — the row exists, however it got there."""
    config = BudgetConfig(
        org_id=ORG_ID,
        entity_type=entity_type,
        entity_id=entity_id,
        period_type=period_type,
        budget_amount_usd=Decimal(amount),
        enforcement_mode="soft",
    )
    session.add(config)
    await session.commit()
    return config


async def seed_usage(
    session: AsyncSession,
    entity_type: str,
    entity_id: str,
    cost: str,
    period_type: str = PeriodType.MONTHLY.value,
    period_start: date | None = None,
) -> BudgetUsage:
    """Insert a settled budget_usage row."""
    if period_start is None:
        period_start, _ = get_period_start_end(PeriodType.MONTHLY)
    usage = BudgetUsage(
        org_id=ORG_ID,
        entity_type=entity_type,
        entity_id=entity_id,
        period_start=period_start,
        period_type=period_type,
        total_cost_usd=Decimal(cost),
        total_tokens=1000,
        request_count=1,
    )
    session.add(usage)
    await session.commit()
    return usage


class TestLoadBearingInvariant:
    """The RUN raise is the thing that must NOT be "fixed" to close this issue.

    Pinning it here means a future author who hits a new instance of this bug class
    cannot make it go away by weakening the raise — which would silently collapse
    every run in a day onto one shared reservation key (run A exhausting run B's cap,
    the exact failure the raise prevents).
    """

    def test_get_period_start_end_still_raises_for_run(self):
        with pytest.raises(ValueError, match="lifetime-scoped"):
            get_period_start_end(PeriodType.RUN)

    def test_calendar_period_types_excludes_run(self):
        assert PeriodType.RUN.value not in CALENDAR_PERIOD_TYPES

    def test_calendar_period_types_is_exactly_the_calendar_periods(self):
        # Allowlist, not a denylist: a period type added to the enum stays inert in
        # the calendar readers until someone deliberately adds it here. If you are
        # here because you added a member to PeriodType, decide whether it has a
        # calendar window before touching this assertion.
        assert CALENDAR_PERIOD_TYPES == {
            PeriodType.DAILY.value,
            PeriodType.WEEKLY.value,
            PeriodType.MONTHLY.value,
        }

    def test_every_calendar_period_type_resolves_to_a_window(self):
        # The allowlist's contract: anything in it is safe to pass to the helper.
        for period_type in CALENDAR_PERIOD_TYPES:
            period_start, period_end = get_period_start_end(PeriodType(period_type))
            assert period_start <= period_end


class TestAlertsBugA:
    """get_budget_alerts raised ValueError with a run-scoped row present."""

    @pytest.mark.asyncio
    async def test_alerts_returns_with_run_row_present(self, budget_service, async_session):
        await seed_config(async_session, EntityType.RUN.value, "event-1", PeriodType.RUN.value)

        alerts = await budget_service.get_budget_alerts(ORG_ID)

        assert alerts == []

    @pytest.mark.asyncio
    async def test_run_row_is_absent_from_alerts(self, budget_service, async_session):
        # A run cap is not a calendar budget, so it has nothing to alert on even when
        # a same-key usage row exists.
        await seed_config(async_session, EntityType.RUN.value, "event-1", PeriodType.RUN.value, amount="10.00")
        await seed_usage(async_session, EntityType.RUN.value, "event-1", "9.00", period_type=PeriodType.RUN.value)

        alerts = await budget_service.get_budget_alerts(ORG_ID, threshold_percent=0.0)

        assert [a for a in alerts if a["period_type"] == PeriodType.RUN.value] == []

    @pytest.mark.asyncio
    async def test_calendar_alerts_still_fire_with_a_run_row_present(self, budget_service, async_session):
        """Catches an over-broad filter — the "worse, because quiet" failure mode.

        Suppressing legitimate daily/weekly/monthly alerts would keep the endpoint at
        200 while silently ending budget alerting for the org.
        """
        await seed_config(async_session, EntityType.RUN.value, "event-1", PeriodType.RUN.value)
        for period in (PeriodType.DAILY, PeriodType.WEEKLY, PeriodType.MONTHLY):
            await seed_config(async_session, EntityType.USER.value, f"user-{period.value}", period.value, amount="100.00")
            period_start, _ = get_period_start_end(period)
            await seed_usage(
                async_session,
                EntityType.USER.value,
                f"user-{period.value}",
                "90.00",
                period_type=period.value,
                period_start=period_start,
            )

        alerts = await budget_service.get_budget_alerts(ORG_ID, threshold_percent=80.0)

        assert {a["period_type"] for a in alerts} == {
            PeriodType.DAILY.value,
            PeriodType.WEEKLY.value,
            PeriodType.MONTHLY.value,
        }
        assert all(a["alert_level"] == "warning" for a in alerts)


class TestOverviewBugB:
    """get_organization_budget_overview raised KeyError, before any period math."""

    @pytest.mark.asyncio
    async def test_overview_returns_with_run_row_present(self, budget_service, async_session):
        await seed_config(async_session, EntityType.RUN.value, "event-1", PeriodType.RUN.value)

        overview = await budget_service.get_organization_budget_overview(ORG_ID)

        # Lifetime-scoped caps are not calendar budgets, so they are not counted here.
        assert overview["total_budgets"] == 0

    @pytest.mark.parametrize("entity_type", [e.value for e in EntityType])
    @pytest.mark.asyncio
    async def test_overview_survives_every_entity_type(self, budget_service, async_session, entity_type):
        """THE ACCEPTANCE GATE.

        Parameterised over every EntityType member with a calendar period, so the
        next member added to the enum breaks this test instead of 500ing the org's
        budget page in production. `entities` was a dict literal with five hard-coded
        keys while EntityType had grown to nine members.
        """
        await seed_config(async_session, entity_type, f"id-{entity_type}", PeriodType.MONTHLY.value)

        overview = await budget_service.get_organization_budget_overview(ORG_ID)

        assert overview["total_budgets"] == 1
        assert [row["entity_id"] for row in overview["entities"][entity_type]] == [f"id-{entity_type}"]

    @pytest.mark.asyncio
    async def test_agent_row_appears_rather_than_being_dropped(self, budget_service, async_session):
        """The #249 case that already crashed `main` — an agent budget is a real budget."""
        await seed_config(async_session, EntityType.AGENT.value, "agent-1", PeriodType.MONTHLY.value, amount="25.00")

        overview = await budget_service.get_organization_budget_overview(ORG_ID)

        agents = overview["entities"][EntityType.AGENT.value]
        assert len(agents) == 1
        assert agents[0]["entity_id"] == "agent-1"
        assert agents[0]["budget_amount_usd"] == 25.00

    @pytest.mark.asyncio
    async def test_five_seeded_keys_still_present_and_populated(self, budget_service, async_session):
        """Pins the response shape for existing clients (test_service.py:463-464)."""
        await seed_config(async_session, EntityType.ORGANIZATION.value, ORG_ID, PeriodType.MONTHLY.value, amount="1000.00")
        await seed_config(async_session, EntityType.DEPARTMENT.value, "dept-1", PeriodType.MONTHLY.value, amount="500.00")
        await seed_config(async_session, EntityType.RUN.value, "event-1", PeriodType.RUN.value)

        overview = await budget_service.get_organization_budget_overview(ORG_ID)

        for seeded in (
            EntityType.ORGANIZATION,
            EntityType.DEPARTMENT,
            EntityType.TEAM,
            EntityType.USER,
            EntityType.SERVICE_ACCOUNT,
        ):
            assert seeded.value in overview["entities"]
        assert len(overview["entities"][EntityType.ORGANIZATION.value]) == 1
        assert len(overview["entities"][EntityType.DEPARTMENT.value]) == 1
        assert overview["entities"][EntityType.TEAM.value] == []
        assert PeriodType.RUN.value not in overview["entities"]


class TestSummaryBugBPrime:
    """get_budget_summary — the third reader, uncatalogued in the issue as filed."""

    @pytest.mark.asyncio
    async def test_summary_returns_for_a_run_scoped_entity(self, budget_service, async_session):
        await seed_config(async_session, EntityType.RUN.value, "event-1", PeriodType.RUN.value)

        summary = await budget_service.get_budget_summary(EntityType.RUN.value, "event-1", ORG_ID)

        assert summary["entity_type"] == EntityType.RUN.value
        assert summary["budgets"] == []

    @pytest.mark.asyncio
    async def test_summary_still_returns_calendar_budgets(self, budget_service, async_session):
        """A run cap on the same entity must not suppress that entity's real budget."""
        await seed_config(async_session, EntityType.USER.value, "user-1", PeriodType.MONTHLY.value, amount="100.00")
        await seed_config(async_session, EntityType.USER.value, "user-1", PeriodType.RUN.value, amount="5.00")

        summary = await budget_service.get_budget_summary(EntityType.USER.value, "user-1", ORG_ID)

        assert [b["period_type"] for b in summary["budgets"]] == [PeriodType.MONTHLY.value]


class TestOverReportBugC:
    """total_spend_current_month summed every hierarchy level of the same dollar.

    Unrelated to #4301 — this has been wrong since the endpoint was written. The
    tracker writes one row per level for a single request (user + org + team + agent
    + root_user), so an unfiltered SUM counted one dollar once per level.
    """

    @pytest.mark.asyncio
    async def test_spend_is_not_multiplied_by_hierarchy_depth(self, budget_service, async_session):
        # One real $100 of org spend, written at three levels the way the tracker does.
        for entity_type in (EntityType.USER.value, EntityType.TEAM.value, EntityType.ORGANIZATION.value):
            await seed_usage(async_session, entity_type, f"id-{entity_type}", "100.00")

        overview = await budget_service.get_organization_budget_overview(ORG_ID)

        # Assert the CORRECT figure, not an over-report factor: the factor is
        # data-dependent (it tracks hierarchy depth), so pinning it would encode the
        # bug rather than the requirement.
        assert overview["total_spend_current_month"] == 100.00

    @pytest.mark.asyncio
    async def test_spend_ignores_agent_and_root_user_rows(self, budget_service, async_session):
        """The newer levels (#249, #4300) inflate the figure the same way."""
        await seed_usage(async_session, EntityType.ORGANIZATION.value, ORG_ID, "40.00")
        await seed_usage(async_session, EntityType.AGENT.value, "agent-1", "40.00")
        await seed_usage(async_session, EntityType.ROOT_USER.value, "user-1", "40.00")

        overview = await budget_service.get_organization_budget_overview(ORG_ID)

        assert overview["total_spend_current_month"] == 40.00

    @pytest.mark.asyncio
    async def test_spend_is_zero_when_no_org_row_exists(self, budget_service, async_session):
        await seed_usage(async_session, EntityType.USER.value, "user-1", "75.00")

        overview = await budget_service.get_organization_budget_overview(ORG_ID)

        assert overview["total_spend_current_month"] == 0

    @pytest.mark.asyncio
    async def test_spend_is_scoped_to_the_org(self, budget_service, async_session):
        """Tenant isolation is unchanged — the added predicate only narrows."""
        await seed_usage(async_session, EntityType.ORGANIZATION.value, ORG_ID, "60.00")
        other = BudgetUsage(
            org_id="org-other",
            entity_type=EntityType.ORGANIZATION.value,
            entity_id="org-other",
            period_start=get_period_start_end(PeriodType.MONTHLY)[0],
            period_type=PeriodType.MONTHLY.value,
            total_cost_usd=Decimal("999.00"),
            total_tokens=1,
            request_count=1,
        )
        async_session.add(other)
        await async_session.commit()

        overview = await budget_service.get_organization_budget_overview(ORG_ID)

        assert overview["total_spend_current_month"] == 60.00


class TestThroughTheRoutes:
    """The endpoints return 200, asserted through the route rather than the service.

    This is the layer the operator actually sees: an unhandled ValueError/KeyError in
    the reader becomes an HTTP 500 for every user in the org.
    """

    @pytest.mark.asyncio
    async def test_alerts_endpoint_200_with_noncalendar_rows(self, app, async_session):
        await seed_config(async_session, EntityType.RUN.value, "event-1", PeriodType.RUN.value)
        await seed_config(async_session, EntityType.CHAIN.value, "corr-1", PeriodType.RUN.value)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/budgets/organization/alerts")

        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_overview_endpoint_200_with_run_agent_and_root_user_rows(self, app, async_session):
        await seed_config(async_session, EntityType.RUN.value, "event-1", PeriodType.RUN.value)
        await seed_config(async_session, EntityType.AGENT.value, "agent-1", PeriodType.MONTHLY.value)
        await seed_config(async_session, EntityType.ROOT_USER.value, "user-1", PeriodType.MONTHLY.value)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/budgets/organization/overview")

        assert response.status_code == 200
        body = response.json()
        assert body["total_budgets"] == 2
        assert body["entities"][EntityType.AGENT.value][0]["entity_id"] == "agent-1"
        assert body["entities"][EntityType.ROOT_USER.value][0]["entity_id"] == "user-1"

    @pytest.mark.asyncio
    async def test_summary_endpoint_200_for_a_run_entity(self, app, async_session):
        await seed_config(async_session, EntityType.RUN.value, "event-1", PeriodType.RUN.value)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/budgets/summary/{EntityType.RUN.value}/event-1")

        assert response.status_code == 200
        assert response.json()["budgets"] == []


class TestAdminBudgetsListQuietFailure:
    """get_budgets_list did not 500 — it silently mislabelled a run cap as monthly.

    The issue's own impact table calls this class of failure *worse* than a 500,
    because nothing surfaces it. The reader re-implemented the period math inline with
    an `else: # monthly` fallthrough, so a run cap was rendered as a monthly budget
    with a wrong current_usage_usd and utilization_pct.
    """

    @pytest.mark.asyncio
    async def test_run_row_not_returned_as_a_monthly_budget(self, admin_service, async_session):
        await seed_config(async_session, EntityType.RUN.value, "event-1", PeriodType.RUN.value)

        result = await admin_service.get_budgets_list(ORG_ID)

        assert result.items == []
        assert result.total == 0

    @pytest.mark.asyncio
    async def test_total_and_has_more_stay_consistent_with_items(self, admin_service, async_session):
        """The count query is filtered too, or pagination reports phantom rows."""
        await seed_config(async_session, EntityType.USER.value, "user-1", PeriodType.MONTHLY.value)
        await seed_config(async_session, EntityType.RUN.value, "event-1", PeriodType.RUN.value)
        await seed_config(async_session, EntityType.CHAIN.value, "corr-1", PeriodType.RUN.value)

        result = await admin_service.get_budgets_list(ORG_ID)

        assert result.total == 1
        assert len(result.items) == 1
        assert result.has_more is False

    @pytest.mark.asyncio
    async def test_calendar_rows_keep_their_own_period_window(self, admin_service, async_session):
        """Replacing the inline math with the shared helper must not change results."""
        await seed_config(async_session, EntityType.USER.value, "u-daily", PeriodType.DAILY.value, amount="10.00")
        await seed_config(async_session, EntityType.USER.value, "u-weekly", PeriodType.WEEKLY.value, amount="20.00")
        await seed_config(async_session, EntityType.USER.value, "u-monthly", PeriodType.MONTHLY.value, amount="40.00")
        for entity_id, period in (
            ("u-daily", PeriodType.DAILY),
            ("u-weekly", PeriodType.WEEKLY),
            ("u-monthly", PeriodType.MONTHLY),
        ):
            period_start, _ = get_period_start_end(period)
            await seed_usage(
                async_session,
                EntityType.USER.value,
                entity_id,
                "5.00",
                period_type=period.value,
                period_start=period_start,
            )

        result = await admin_service.get_budgets_list(ORG_ID)

        by_id = {item.entity_id: item for item in result.items}
        assert by_id["u-daily"].current_usage_usd == Decimal("5.00")
        assert by_id["u-daily"].utilization_pct == 50.0
        assert by_id["u-weekly"].current_usage_usd == Decimal("5.00")
        assert by_id["u-weekly"].utilization_pct == 25.0
        assert by_id["u-monthly"].current_usage_usd == Decimal("5.00")
        assert by_id["u-monthly"].utilization_pct == 12.5
