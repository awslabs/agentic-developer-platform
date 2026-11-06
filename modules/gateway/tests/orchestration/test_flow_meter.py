"""Persistent initialization plus real Lua shared model spend and reconciliation."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from src.budget.config import budget_config
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.reservations import ReservationStore
from src.budget.run_binding import RunBinding
from src.orchestration import flow_budget, flow_meter
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import DenyReason
from tests.agentauth.test_human_dispatch import store as authority_fixture
from tests.budget.test_run_spend_cap import _no_budget_session
from tests.orchestration.test_policy_admission import _policy

authority = authority_fixture


@pytest.fixture
async def meter(authority, monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    clock = [1000.0]
    store = ReservationStore(redis_url=None, ttl_seconds=120, client=client, clock=lambda: clock[0])
    monkeypatch.setattr(flow_budget, "_reservations", store)
    monkeypatch.setattr(budget_config, "budget_reservation_enabled", True)

    async def claim(*, org_id, flow_id, allow_create):
        return authority.claim_policy_budget_initialization(tenant_id=org_id, flow_id=flow_id, allow_create=allow_create)

    monkeypatch.setattr(flow_meter, "_claim_initialization", claim)
    policy = _policy()
    target = flow_meter.meter_target(org_id=policy.org_id, flow_id="flow", policy=policy)
    assert await flow_meter.prepare_flow_meter(org_id=policy.org_id, flow_id="flow", policy=policy, nodes=[])
    yield SimpleNamespace(client=client, clock=clock, store=store, policy=policy, target=target)
    await client.aclose()


async def test_parallel_descendants_and_new_run_ids_share_one_remaining_allowance(meter):
    results = await asyncio.gather(*(meter.store.reserve(run, Decimal(20), [meter.target]) for run in ("developer", "reviewer", "repair")))
    assert sum(result.admitted for result in results) == 2
    assert (await meter.store.snapshot(meter.target)).total_usd == 40
    assert not (await meter.store.reserve("restarted-run", Decimal(20), [meter.target])).admitted
    await meter.store.reconcile("developer", Decimal(10), [meter.target])
    assert (await meter.store.reserve("new-evaluator", Decimal(20), [meter.target])).admitted
    assert (await meter.store.snapshot(meter.target)).total_usd == 50


async def test_settled_spend_survives_a_long_flow_and_later_window_extension(meter):
    from src.orchestration.execution_policy import MAX_WALL_CLOCK_SECONDS

    assert (await meter.store.reserve("early-call", Decimal(40), [meter.target])).admitted
    await meter.store.reconcile("early-call", Decimal(40), [meter.target])
    # The initial policy can be short, then renewed to the maximum allowed
    # shared window. Both snapshot and Lua admission must keep the first charge.
    meter.clock[0] += MAX_WALL_CLOCK_SECONDS
    assert (await meter.store.snapshot(meter.target)).total_usd == 40
    assert not (await meter.store.reserve("renewed-worker", Decimal(11), [meter.target])).admitted
    assert (await meter.store.reserve("fits", Decimal(10), [meter.target])).admitted
    assert (await meter.store.snapshot(meter.target)).total_usd == 50
    assert await meter.client.ttl(meter.target.key()) >= MAX_WALL_CLOCK_SECONDS


def test_meter_retention_covers_policy_accepted_before_work_can_start():
    policy = _policy(expires_at=datetime.now(UTC) + timedelta(days=60))
    target = flow_meter.meter_target(org_id=policy.org_id, flow_id="future", policy=policy)
    assert target.ttl_seconds >= 60 * 86400


async def test_redis_loss_cannot_reinitialize_an_existing_flow(meter):
    assert (await meter.store.reserve("spent-run", Decimal(20), [meter.target])).admitted
    await meter.client.flushdb()
    assert await meter.store.snapshot(meter.target) is None
    assert await meter.store.reserve("restart", Decimal(1), [meter.target]) is None
    # Even a retry before the first SQL dispatch commit cannot claim a new zero.
    assert not await flow_meter.prepare_flow_meter(org_id=meter.policy.org_id, flow_id="flow", policy=meter.policy, nodes=[])
    assert await flow_meter.prepare_flow_meter(org_id=meter.policy.org_id, flow_id="new-flow", policy=meter.policy, nodes=[])


async def test_existing_work_without_a_meter_requires_reconciliation(meter):
    assert not await flow_meter.prepare_flow_meter(
        org_id=meter.policy.org_id, flow_id="old-flow", policy=meter.policy, nodes=[SimpleNamespace(attempts=1, kind="story", state="running")]
    )


async def test_a_node_superseded_before_its_first_attempt_does_not_block_meter_creation(meter):
    """A plan revision approved before anything ran must not strand the flow (#7408).

    The superseded node never dispatched, so nothing was spent; the meter can start
    at zero exactly as it would for a flow of only pending nodes.
    """
    never_ran = SimpleNamespace(attempts=0, kind="story", state="superseded")
    pending = SimpleNamespace(attempts=0, kind="story", state="pending")
    assert await flow_meter.prepare_flow_meter(org_id=meter.policy.org_id, flow_id="revised-flow", policy=meter.policy, nodes=[never_ran, pending])


async def test_a_superseded_node_that_did_run_still_requires_reconciliation(meter):
    ran_then_superseded = SimpleNamespace(attempts=1, kind="story", state="superseded")
    assert not await flow_meter.prepare_flow_meter(
        org_id=meter.policy.org_id, flow_id="revised-old-flow", policy=meter.policy, nodes=[ran_then_superseded]
    )


async def test_unknown_usage_retains_reservation_and_receipt_restores_only_valid_headroom(meter):
    await meter.store.reserve("call", Decimal(20), [meter.target])
    await meter.store.mark_unknown("call", meter.target)
    assert await meter.store.snapshot(meter.target) is None
    assert await meter.store.reserve("new-call", Decimal(1), [meter.target]) is None
    assert (await meter.client.hget(meter.target.key(), "call")).startswith("20:")
    await meter.store.reconcile("call", Decimal(7), [meter.target])
    snapshot = await meter.store.snapshot(meter.target)
    assert snapshot.total_usd == 7 and not snapshot.has_pending
    assert not (await meter.store.reserve("too-large", Decimal(44), [meter.target])).admitted
    assert (await meter.store.reserve("fits", Decimal(43), [meter.target])).admitted


async def test_lost_worker_cannot_expire_unreconciled_usage_into_zero(meter):
    await meter.store.reserve("lost", Decimal(20), [meter.target])
    meter.clock[0] += 3661
    assert await meter.store.snapshot(meter.target) is None
    assert await meter.store.reserve("next", Decimal(1), [meter.target]) is None


async def test_tenant_and_policy_amendment_do_not_change_spend_identity(meter):
    amended = meter.policy.model_copy(update={"policy_id": "another-version"})
    assert flow_meter.meter_target(org_id=amended.org_id, flow_id="flow", policy=amended).key() == meter.target.key()
    other = replace(meter.target, org_id="other-tenant")
    assert other.key() != meter.target.key()
    assert await meter.store.snapshot(other) is None


async def _confirmable_quote():
    """A real quote for a real request, via the production entry point.

    Built through ``quote_request`` rather than hand-constructed so it carries the
    live pricing revision and therefore confirms at the reservation boundary. Its
    ``total_usd`` is deliberately NOT used as the cost under test — the caller
    pins that separately — so this cannot make the cap arithmetic depend on
    today's published rates.
    """
    from src.orchestration.provider_quotes import quote_request

    body = json.dumps({"model": "anthropic.claude-sonnet-4-6", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 16}).encode()
    return await quote_request(body, "/v1/messages")


async def test_budget_service_enforces_flow_when_legacy_run_caps_are_disabled(meter, monkeypatch):
    monkeypatch.setattr(budget_config, "budget_run_cap_enabled", False)
    monkeypatch.setattr(budget_config, "budget_run_binding_mode", "shadow")
    service = BudgetEnforcementService()
    service._reservations = meter.store

    @asynccontextmanager
    async def session():
        db = _no_budget_session()
        # The verified run belongs to an existing legacy flow without Product scope.
        db.scalar = AsyncMock(return_value=SimpleNamespace(product_id=None, project_id=None))
        yield db

    monkeypatch.setattr(service, "_get_session", session)
    monkeypatch.setattr(service, "_note_check_succeeded", AsyncMock())
    context = TokenContext(
        user_id="iam-agent:authority-worker",
        agent_registry_id="authority-worker",
        org_id="__platform__",
        attributed_org_id=meter.policy.org_id,
        team_id="",
        department_id="",
        account_type="service",
        auth_source="iam",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    context._protected_run_binding = RunBinding("worker", "flow", meter.policy.org_id, "", "", False, "flow")
    context._policy_flow_target = meter.target
    context._policy_estimated_cost = Decimal(20)
    # Issue #5225: the reservation is bound to a typed quote, which it re-confirms
    # immediately before spending. This test predates that contract and pins the
    # cost directly to keep the arithmetic below exact, so it needs a quote whose
    # identity still confirms. Only expiry and the pricing revision are checked at
    # that boundary, so a quote issued here from the live snapshot satisfies it —
    # while the $20 figure the flow-cap assertions depend on stays untouched.
    context._policy_quote = await _confirmable_quote()
    await meter.store.reserve("earlier-worker", Decimal(20), [meter.target])
    await meter.store.reconcile("earlier-worker", Decimal(20), [meter.target])
    context._policy_request_id = "one"
    first = await service.check_budget_hierarchy(context, Decimal("0.01"), request_id="untrusted")
    assert first.allowed
    context._policy_request_id = "two"
    second = await service.check_budget_hierarchy(context, Decimal("0.01"), request_id="untrusted")
    assert not second.allowed and second.scope == "flow"
    assert second.deny_reason == DenyReason.RESERVATIONS_PENDING
    # A repeated request must recheck the still-live denominator, not bypass it.
    again = await service.check_budget_hierarchy(context, Decimal("0.01"), request_id="untrusted")
    assert not again.allowed and again.deny_reason == DenyReason.RESERVATIONS_PENDING
    await service.reconcile_reservation(context, "one", "unknown-model", 0, 0, actual_cost_usd=Decimal(0), usage_known=False)
    assert await meter.store.snapshot(meter.target) is None
    await service.reconcile_reservation(context, "one", "unknown-model", 1, 1, actual_cost_usd=Decimal(5))
    assert (await meter.store.snapshot(meter.target)).total_usd == 25
    context._policy_request_id = "three"
    assert (await service.check_budget_hierarchy(context, Decimal("0.01"), request_id="untrusted")).allowed


async def test_a_policy_cost_with_no_quote_behind_it_is_never_reserved(meter, monkeypatch):
    """An amount whose provenance is gone cannot be spent (#5225).

    A bare Decimal says nothing about which bytes, which billing model or which
    published revision produced it. Reserving against one would reintroduce
    exactly the unbound spend the typed quote exists to prevent, so the policy
    path refuses rather than falling back to the number.
    """
    service = BudgetEnforcementService()
    service._reservations = meter.store

    @asynccontextmanager
    async def session():
        yield _no_budget_session()

    monkeypatch.setattr(service, "_get_session", session)
    monkeypatch.setattr(service, "_note_check_succeeded", AsyncMock())
    context = TokenContext(
        user_id="iam-agent:authority-worker",
        agent_registry_id="authority-worker",
        org_id="__platform__",
        attributed_org_id=meter.policy.org_id,
        team_id="",
        department_id="",
        account_type="service",
        auth_source="iam",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    context._policy_flow_target = meter.target
    context._policy_estimated_cost = Decimal(20)
    context._policy_request_id = "quoteless"
    result = await service.check_budget_hierarchy(context, Decimal("0.01"), request_id="untrusted")
    assert not result.allowed and result.scope == "flow"
    # Refused before any hold was taken. The meter already exists (the fixture
    # initialized the flow), so the invariant is that nothing was ADDED to it.
    snapshot = await meter.store.snapshot(meter.target)
    assert snapshot.total_usd == 0 and not snapshot.has_pending


@pytest.mark.parametrize("failure", ["disabled", "backend", "missing_id", "missing_anchor"])
async def test_policy_budget_service_never_degrades_open(meter, monkeypatch, failure):
    service = BudgetEnforcementService()
    service._reservations = meter.store
    request_id = "call"
    if failure == "disabled":
        monkeypatch.setattr(budget_config, "budget_reservation_enabled", False)
    elif failure == "backend":
        service._reservations = ReservationStore(redis_url=None, ttl_seconds=120)
    elif failure == "missing_id":
        request_id = None
    else:
        await meter.client.flushdb()
    result = await service._reserve_or_degrade(request_id, Decimal(1), [meter.target], strict=True)
    assert not result.allowed and result.scope == "flow"


def test_quote_prices_requested_output_and_refuses_unbounded_provider_features():
    body = {"model": "anthropic.claude-sonnet-4-6", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 16}

    def quote(document):
        return flow_meter.estimate_policy_model_cost(json.dumps(document).encode(), "/v1/messages")

    assert quote(dict(body, max_tokens=10000)) > quote(body) > 0
    # Hidden provider framing cannot be estimated from bytes. Both requests
    # reserve the entire published input context and the same output maximum.
    assert quote(dict(body, messages=[{"role": "user", "content": "long text " * 1000}])) == quote(body)
    for changes in ({"max_tokens": None}, {"model": "unknown"}, {"mcp_servers": ["remote"]}, {"messages": [{"content": [{"type": "image"}]}]}):
        with pytest.raises(ValueError):
            quote(dict(body, **changes))


# --- An unreported request must not strand the flow (#7451) ----------------------


async def _expired_marker(meter, request_id="lost-call", amount=Decimal(20)):
    """A strict reservation whose usage never arrived: marker expired, reservation live."""
    await meter.store.reserve(request_id, amount, [meter.target])
    assert await meter.store.snapshot(meter.target) is not None  # live marker: pending, but readable
    meter.clock[0] += 3661  # the strict marker lasts 61 minutes; the reservation itself 48 hours
    assert await meter.store.expired_pending_requests(meter.target) == []  # recently expired: a receipt may still be on its way
    meter.clock[0] += 301  # expired for the settlement grace: abandoned
    assert await meter.store.snapshot(meter.target) is None
    assert await meter.store.reserve("next-call", Decimal(1), [meter.target]) is None


async def test_expired_unreported_request_is_settled_at_its_bound_and_recorded(meter, db_session_factory):
    from src.budget.enforcement_settings import BudgetAccountingGap

    await _expired_marker(meter)
    assert await meter.store.expired_pending_requests(meter.target) == ["lost-call"]
    async with db_session_factory() as session:
        snapshot = await flow_meter.reconcile_flow_meter(session, org_id=meter.policy.org_id, flow_id="flow", policy=meter.policy)
        assert snapshot is not None and snapshot.total_usd == Decimal(20) and not snapshot.has_pending
        gap = await session.get(BudgetAccountingGap, "lost-call")
        assert gap is not None and gap.scope_key == meter.target.key()
    # The flow can spend again; the bound stays charged and is never lowered.
    assert (await meter.store.reserve("next-call", Decimal(1), [meter.target])).admitted
    assert (await meter.store.snapshot(meter.target)).total_usd == Decimal(21)


async def test_live_marker_is_never_settled_early(meter, db_session_factory):
    await meter.store.reserve("in-flight", Decimal(5), [meter.target])
    assert await meter.store.expired_pending_requests(meter.target) == []
    assert await meter.store.settle_expired_pending("in-flight", meter.target) is False
    async with db_session_factory() as session:
        snapshot = await flow_meter.reconcile_flow_meter(session, org_id=meter.policy.org_id, flow_id="flow", policy=meter.policy)
    assert snapshot is not None and snapshot.has_pending


async def test_receipt_still_wins_over_settlement_at_bound(meter, db_session_factory):
    from src.budget.enforcement_settings import BudgetAccountingGap
    from src.shared.models.budget import BudgetSettlementReceipt

    await _expired_marker(meter, "late-receipt", Decimal(20))
    async with db_session_factory() as session:
        session.add(
            BudgetSettlementReceipt(
                org_id=meter.policy.org_id,
                request_id="late-receipt",
                allocation_key="test",
                user_id="worker",
                total_tokens=0,
                cost_usd=Decimal("0.25"),
                reservation_scope_keys=[meter.target.key()],
            )
        )
        await session.commit()
        snapshot = await flow_meter.reconcile_flow_meter(session, org_id=meter.policy.org_id, flow_id="flow", policy=meter.policy)
        assert snapshot is not None and snapshot.total_usd == Decimal("0.25")
        assert await session.get(BudgetAccountingGap, "late-receipt") is None


async def test_unknown_usage_waits_the_settlement_grace_for_its_receipt(meter, db_session_factory):
    await meter.store.reserve("unknown-usage", Decimal(5), [meter.target])
    await meter.store.mark_unknown("unknown-usage", meter.target)
    assert await meter.store.snapshot(meter.target) is None
    meter.clock[0] += 240
    assert await meter.store.expired_pending_requests(meter.target) == []
    assert await meter.store.settle_expired_pending("unknown-usage", meter.target) is False
    async with db_session_factory() as session:
        assert await flow_meter.reconcile_flow_meter(session, org_id=meter.policy.org_id, flow_id="flow", policy=meter.policy) is None
    meter.clock[0] += 61
    assert await meter.store.expired_pending_requests(meter.target) == ["unknown-usage"]
