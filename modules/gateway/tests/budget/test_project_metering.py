# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Project caps cover durable provider calls and survive interrupted dispatch."""

from dataclasses import replace
from decimal import Decimal

import fakeredis.aioredis
import pytest
from sqlalchemy import select

from src.budget.enforcement_service import budget_enforcement_service
from src.budget.metering_admission import MeteringBudgetExceededError
from src.budget.project_budget import project_target, reconcile_project_budget
from src.budget.reservations import ReservationStore
from src.products.models import Product, ProductProjectBinding, Project
from src.products.work_scope import ProjectAttribution
from src.shared.models.budget import BudgetConfig
from src.shared.models.organization import Organization, User
from src.shared.models.usage import UsageLog
from tests.budget.test_metering_scope_admission import BODY, admission_for, context, prepare
from tests.budget.test_metering_scope_admission import journal as journal
from tests.budget.test_metering_scope_admission import metering_db as metering_db
from tests.budget.test_metering_settlement_receipts import DESTINATION, complete
from tests.proxy.test_mantle_metering_admission import MODEL

# Bind fixtures locally: when their test module is collected before this one,
# registering it later as a plugin does not make its fixtures globally visible.
pytest_plugins = ("tests.migrations.conftest_postgres",)


async def setup_project(admission):
    async with admission._session_factory() as db:
        db.add(Organization(id="tenant-1", name="Fixture"))
        await db.flush()
        db.add(User(id="owner-1", org_id="tenant-1", team_id="team", email="owner@example.test", user_kind="human"))
        await db.flush()
        db.add(
            Product(
                tenant_id="tenant-1",
                id="product",
                name="Example",
                purpose="Test",
                intended_users=[],
                owner_user_id="owner-1",
                starting_point="idea",
                created_by="owner-1",
            )
        )
        db.add(Project(tenant_id="tenant-1", id="project", name="Base", owner_user_id="owner-1"))
        await db.flush()
        db.add(ProductProjectBinding(tenant_id="tenant-1", product_id="product", project_id="project", is_base=True, provenance={}))
        db.add(
            BudgetConfig(
                org_id="tenant-1",
                entity_type="project",
                entity_id="project",
                period_type="run",
                budget_amount_usd=Decimal(".01"),
                enforcement_mode="hard",
            )
        )
        await db.commit()
    caller = context()
    caller._policy_quote = replace(caller._policy_quote, upper_bound_usd=Decimal(".006"))
    caller._project_attribution = ProjectAttribution("tenant-1", "product", "project", 1, "run")
    return caller


async def test_durable_calls_share_project_cap_and_settle_once(metering_db, journal, monkeypatch):
    admission, engine = admission_for(metering_db, journal)
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = ReservationStore(None, 120, client=client)
    monkeypatch.setattr(budget_enforcement_service, "_get_reservations", lambda: store)
    try:
        caller = await setup_project(admission)
        first, receipt, decision = await complete(admission, caller, "first")
        second = await prepare(admission, caller, "second")
        with pytest.raises(MeteringBudgetExceededError):
            await admission.arm(second, submitted=BODY, model=MODEL, destination=DESTINATION)
        await admission.cancel_unarmed(second)
        assert await admission.settle_receipt(first, receipt)
        assert not await admission.settle_receipt(first, receipt)
        target = project_target("tenant-1", "project")
        async with admission._session_factory() as db:
            await reconcile_project_budget(db, store, target)
            logged = await db.scalar(select(UsageLog).where(UsageLog.request_id == "first"))
            assert (logged.product_id, logged.project_id, logged.agent_run_id) == ("product", "project", "run")
        assert (await store.snapshot(target)).total_usd == decision.ledger_cost
        third = await prepare(admission, caller, "third")
        await admission.arm(third, submitted=BODY, model=MODEL, destination=DESTINATION)
    finally:
        await engine.dispose()
        await client.aclose()


async def test_interrupted_arm_releases_only_after_durable_unsent_cancel(metering_db, journal, monkeypatch):
    admission, engine = admission_for(metering_db, journal)
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = ReservationStore(None, 120, client=client)
    monkeypatch.setattr(budget_enforcement_service, "_get_reservations", lambda: store)
    try:
        caller = await setup_project(admission)
        attempt = await prepare(admission, caller, "interrupted")
        reserve = store.reserve

        async def interrupted(*args, **kwargs):
            assert (await reserve(*args, **kwargs)).admitted
            raise RuntimeError("interrupted after reservation")

        monkeypatch.setattr(store, "reserve", interrupted)
        with pytest.raises(RuntimeError, match="interrupted"):
            await admission.arm(attempt, submitted=BODY, model=MODEL, destination=DESTINATION)
        target = project_target("tenant-1", "project")
        assert (await store.snapshot(target)).total_usd == Decimal(".006")
        await admission.cancel_unarmed(attempt)
        async with admission._session_factory() as db:
            await reconcile_project_budget(db, store, target)
        assert (await store.snapshot(target)).total_usd == 0
    finally:
        await engine.dispose()
        await client.aclose()
