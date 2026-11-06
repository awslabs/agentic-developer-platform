"""Financial display preserves tenant boundaries, prior charges and unknowns."""

import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.auth.dependencies import get_current_user
from src.budget.enforcement_settings import BudgetEnforcementSetting, flow_key
from src.budget.reservations import ReservationStore, ReservationTarget
from src.orchestration import budget_read, budget_read_routes
from src.orchestration.dispatch import graph_address
from src.orchestration.flow_meter import meter_target
from src.orchestration.models import OrchestrationDecision
from src.shared.database import get_db
from src.shared.models.budget import BudgetConfig
from tests.orchestration.test_cost import _usage
from tests.orchestration.test_list_flows import ORG_A, ORG_B, _token_context, seed_flow, seed_node, session  # noqa: F401
from tests.orchestration.test_policy_admission import _policy
from tests.orchestration.test_shared_budget import accept, budget, engine, shared  # noqa: F401


@pytest.fixture
async def display(session, monkeypatch):  # noqa: F811
    flow = await seed_flow(session, slug="sample_flow")
    node = await seed_node(session, flow, node_ref="story")
    policy = _policy()
    policy.limits = policy.limits.model_copy(update={"max_spend_usd": Decimal(2000), "max_concurrent_actions": 5})
    policy._shared_run_spend_usd = Decimal(100)
    monkeypatch.setattr(budget_read, "load_in_force_policy", AsyncMock(return_value=SimpleNamespace(policy=policy, refusal=None)))
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    clock = [1000.0]
    store = ReservationStore(redis_url=None, ttl_seconds=120, client=client, clock=lambda: clock[0])
    monkeypatch.setattr(budget_read, "get_flow_reservations", lambda: store)
    monkeypatch.setattr(budget_read.budget_enforcement_service, "_get_reservations", lambda: store)
    target = meter_target(org_id=flow.org_id, flow_id=flow.id, policy=policy)
    await store.reserve("__initialized__", Decimal(0), [replace(target, require_initialization=False)])
    yield SimpleNamespace(db=session, flow=flow, node=node, policy=policy, client=client, store=store, target=target, clock=clock)
    await client.aclose()


async def usage(d, request="settled", cost="40", run="run-one", **kwargs):
    row = _usage(org_id=d.flow.org_id, address=graph_address(d.node, flow_slug=d.flow.slug), cost=cost, run_id=run)
    row.request_id = request
    for key, value in kwargs.items():
        setattr(row, key, value)
    d.db.add(row)
    await d.db.flush()
    return row


async def test_flow_separates_recorded_cost_from_held_exposure_without_writes(display):
    d = display
    await usage(d)
    await d.store.reserve("settled", Decimal(40), [d.target])
    await d.store.reconcile("settled", Decimal(40), [d.target])
    await d.store.reserve("outstanding", Decimal("30.25"), [d.target])
    before = await d.client.hgetall(d.target.key())
    result = await budget_read.read_flow_budget(d.db, d.flow)
    assert result.balance.model_dump() == dict(
        limit_usd=Decimal(2000),
        spent_usd=Decimal(40),
        reserved_usd=Decimal("30.25"),
        remaining_usd=Decimal("1929.75"),
        request_reserved_usd=Decimal("30.25"),
        recovery_hold_usd=Decimal(0),
    )
    assert result.run_limit_usd == 100 and result.concurrency == 5
    assert await d.client.hgetall(d.target.key()) == before
    assert not d.db.new and not d.db.dirty and not d.db.deleted


@pytest.mark.parametrize("missing", [False, True])
async def test_empty_initialized_meter_is_zero_but_absence_is_unknown(display, missing):
    if missing:
        await display.client.delete(display.target.key())
    result = await budget_read.read_flow_budget(display.db, display.flow)
    assert result.balance.spent_usd == (None if missing else Decimal(0))
    assert result.balance.reserved_usd == (None if missing else Decimal(0))
    assert result.balance.remaining_usd == (None if missing else Decimal(2000))


async def test_unknown_usage_and_off_mode_are_not_displayed_as_available_credit(display):
    d = display
    await usage(d)
    await d.store.reserve("unreported", Decimal(20), [d.target])
    await d.store.mark_unknown("unreported", d.target)
    d.db.add(BudgetEnforcementSetting(scope_key=flow_key(d.flow.org_id, d.flow.id), enabled=False, updated_by="owner"))
    await d.db.flush()
    result = await budget_read.read_flow_budget(d.db, d.flow)
    assert not result.enforcement_enabled
    assert result.balance.spent_usd == 40
    assert result.balance.reserved_usd is None and result.balance.remaining_usd is None


async def test_historical_continuation_cost_is_not_an_outstanding_request(display):
    d = display
    await usage(d)
    await d.store.reserve("__historical_spend__", Decimal(40), [d.target])
    await d.store.reconcile("__historical_spend__", Decimal(40), [d.target])
    result = await budget_read.read_flow_budget(d.db, d.flow)
    assert result.balance.spent_usd == 40 and result.balance.reserved_usd == 0
    assert result.balance.remaining_usd == 1960


async def test_flow_cost_does_not_mix_similar_slugs_or_other_tenants(display):
    d = display
    await usage(d)
    await usage(d, "other-flow", "999", graph_address="sample-flow/epic-1/wave-1/story")
    await usage(d, "other-tenant", "999", org_id=ORG_B)
    assert (await budget_read.read_flow_budget(d.db, d.flow)).balance.spent_usd == 40


async def test_run_balances_use_current_tighter_tenant_limit_and_preserve_expired_history(display):
    d = display
    d.db.add(BudgetConfig(org_id=ORG_A, entity_type="run", entity_id="*", period_type="run", budget_amount_usd=Decimal(60)))
    await usage(d)
    target = ReservationTarget(ORG_A, "run", "run-one", "run", "lifetime", Decimal(100))
    await d.store.reserve("settled", Decimal(40), [target])
    await d.store.reconcile("settled", Decimal(40), [target])
    await d.store.reserve("pending", Decimal(5), [target])
    result = await budget_read.read_node_budget(d.db, d.flow, d.node)
    assert result.runs[0].balance.model_dump() == dict(
        limit_usd=Decimal(60),
        spent_usd=Decimal(40),
        reserved_usd=Decimal(5),
        remaining_usd=Decimal(15),
        request_reserved_usd=None,
        recovery_hold_usd=None,
    )
    await d.client.delete(target.key())
    expired = (await budget_read.read_node_budget(d.db, d.flow, d.node)).runs[0].balance
    assert expired.spent_usd == 40 and expired.remaining_usd is None


async def test_expired_fields_are_not_shown_as_a_fresh_run_allowance(display):
    d = display
    await usage(d)
    target = ReservationTarget(ORG_A, "run", "run-one", "run", "lifetime", Decimal(100))
    await d.store.reserve("settled", Decimal(40), [target])
    await d.store.reconcile("settled", Decimal(40), [target])
    d.clock[0] += 86400 * 3
    result = (await budget_read.read_node_budget(d.db, d.flow, d.node)).runs[0].balance
    assert result.spent_usd == 40 and result.remaining_usd is None


async def test_actual_usage_above_limit_has_zero_remaining(display):
    d = display
    await usage(d, cost="120")
    target = ReservationTarget(ORG_A, "run", "run-one", "run", "lifetime", Decimal(100))
    await d.store.reserve("settled", Decimal(5), [target])
    await d.store.reconcile("settled", Decimal(120), [target])
    result = (await budget_read.read_node_budget(d.db, d.flow, d.node)).runs[0].balance
    assert result.spent_usd == 120 and result.remaining_usd == 0


async def test_real_approved_increase_is_used_instead_of_archived_plan_limits(budget, monkeypatch):  # noqa: F811
    await accept(budget)
    monkeypatch.setattr(budget_read, "get_flow_reservations", lambda: budget.s.store)
    result = await budget_read.read_flow_budget(budget.s.session, budget.s.flow)
    assert result.balance.limit_usd == 1000 and result.run_limit_usd == 100


async def test_route_permission_and_tenant_boundaries_precede_budget_reads(display, monkeypatch):
    d = display
    app = FastAPI()
    app.include_router(budget_read_routes.router, prefix="/orchestration")

    async def db():
        yield d.db

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda: _token_context(ORG_B)
    check = AsyncMock()
    monkeypatch.setattr(budget_read_routes, "AccessControl", lambda _db: SimpleNamespace(check_permission=check))
    reader = AsyncMock()
    monkeypatch.setattr(budget_read_routes, "read_flow_budget", reader)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(f"/orchestration/flows/{d.flow.id}/budget")).status_code == 404
        assert (await client.get(f"/orchestration/flows/{d.flow.id}/nodes/{d.node.id}/budget")).status_code == 404
        from fastapi import HTTPException

        check.side_effect = HTTPException(403, "Denied")
        assert (await client.get(f"/orchestration/flows/{d.flow.id}/budget")).status_code == 403
    reader.assert_not_awaited()


async def test_routes_serialize_money_and_reject_nodes_outside_the_flow(display, monkeypatch):
    d = display
    await usage(d)
    other = await seed_flow(d.db, slug="other")
    other_node = await seed_node(d.db, other, node_ref="story")
    app = FastAPI()
    app.include_router(budget_read_routes.router, prefix="/orchestration")

    async def db():
        yield d.db

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda: _token_context(ORG_A)
    monkeypatch.setattr(budget_read_routes, "AccessControl", lambda _db: SimpleNamespace(check_permission=AsyncMock()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        flow_response = await client.get(f"/orchestration/flows/{d.flow.id}/budget")
        assert flow_response.status_code == 200
        assert Decimal(flow_response.json()["balance"]["spent_usd"]) == 40
        assert flow_response.json()["run_limit_usd"] == "100"
        node_response = await client.get(f"/orchestration/flows/{d.flow.id}/nodes/{d.node.id}/budget")
        assert node_response.status_code == 200
        assert node_response.json()["runs"][0]["run_id"] == "run-one"
        assert node_response.json()["runs"][0]["balance"]["remaining_usd"] is None
        assert (await client.get(f"/orchestration/flows/{d.flow.id}/nodes/{other_node.id}/budget")).status_code == 404


async def test_unaccepted_draft_shows_proposed_limits_without_treating_them_as_in_force(display, monkeypatch):
    """An owner deciding whether to accept sees the bounds acceptance would grant.

    The proposed policy is display only: `balance.limit_usd` stays unknown and no
    run limit is derived, because nothing is in force until a human accepts.
    """
    from src.orchestration.models import OrchestrationAcceptedPlan

    d = display
    monkeypatch.setattr(budget_read, "load_in_force_policy", AsyncMock(return_value=SimpleNamespace(policy=None, refusal="not accepted")))
    d.db.add(
        OrchestrationAcceptedPlan(
            org_id=d.flow.org_id,
            flow_id=d.flow.id,
            version=1,
            plan_hash="c" * 64,
            plan_document={
                "flow_slug": d.flow.slug,
                "proposed_execution_policy": {
                    "schema_version": 1,
                    "expires_at": "2026-11-16T20:31:17Z",
                    "limits": {"max_spend_usd": "1000", "max_wall_clock_seconds": 1209600, "max_attempts_per_node": 3, "max_concurrent_actions": 2},
                },
            },
        )
    )
    await d.db.flush()
    result = await budget_read.read_flow_budget(d.db, d.flow)
    assert result.policy_status == "proposed"
    assert result.balance.limit_usd is None and result.balance.remaining_usd is None
    assert result.run_limit_usd is None and result.concurrency is None
    assert result.proposed.model_dump(mode="json") == {
        "limit_usd": "1000",
        "concurrency": 2,
        "attempts_per_node": 3,
        "wall_clock_seconds": 1209600,
        "expires_at": "2026-11-16T20:31:17Z",
        "plan_version": 1,
        "plan_hash": "c" * 64,
    }


async def test_accepted_policy_reports_accepted_status_and_no_proposal(display):
    result = await budget_read.read_flow_budget(display.db, display.flow)
    assert result.policy_status == "accepted" and result.proposed is None


async def test_flow_without_any_plan_reports_none(display, monkeypatch):
    monkeypatch.setattr(budget_read, "load_in_force_policy", AsyncMock(return_value=SimpleNamespace(policy=None, refusal="not accepted")))
    result = await budget_read.read_flow_budget(display.db, display.flow)
    assert result.policy_status == "none" and result.proposed is None and result.balance.limit_usd is None


@pytest.mark.parametrize("provenance", ["same_flow", "other_tenant", "other_flow", "missing", "malformed"])
async def test_recovery_breakdown_requires_matching_audit_and_does_not_change_headroom(display, provenance):
    d = display
    recovery = "00000000-0000-4000-8000-000000000001"
    hold = "recovery-" + recovery
    await usage(d)
    await d.store.reserve("settled", Decimal(40), [d.target])
    await d.store.reconcile("settled", Decimal(40), [d.target])
    await d.store.reserve(hold, Decimal(100), [d.target])
    await d.store.retain_failed_bound(hold, d.target)
    await d.store.reserve("pending-request", Decimal(15), [d.target])
    if provenance != "missing":
        other = await seed_flow(d.db, slug="other") if provenance == "other_flow" else d.flow
        d.db.add(
            OrchestrationDecision(
                org_id=ORG_B if provenance == "other_tenant" else d.flow.org_id,
                flow_id=other.id,
                kind="meter_ceiling_restored",
                actor_id="operator",
                actor_role="operator",
                actor_kind="service",
                reason="not-json" if provenance == "malformed" else json.dumps({"recovery_id": recovery}),
            )
        )
        await d.db.flush()
    before = await d.client.hgetall(d.target.key())
    result = (await budget_read.read_flow_budget(d.db, d.flow)).balance
    assert result.reserved_usd == 115 and result.remaining_usd == 1845
    assert result.recovery_hold_usd == (100 if provenance == "same_flow" else 0)
    assert result.request_reserved_usd == (15 if provenance == "same_flow" else 115)
    assert await d.client.hgetall(d.target.key()) == before
