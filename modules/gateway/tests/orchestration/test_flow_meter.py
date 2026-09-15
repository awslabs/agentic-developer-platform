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


async def test_budget_service_enforces_flow_when_legacy_run_caps_are_disabled(meter, monkeypatch):
    monkeypatch.setattr(budget_config, "budget_run_cap_enabled", False)
    monkeypatch.setattr(budget_config, "budget_run_binding_mode", "shadow")
    service = BudgetEnforcementService()
    service._reservations = meter.store

    @asynccontextmanager
    async def session():
        yield _no_budget_session()

    monkeypatch.setattr(service, "_get_session", session)
    monkeypatch.setattr(service, "_note_check_succeeded", AsyncMock())
    context = TokenContext(
        user_id="authority-worker",
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
    await meter.store.reserve("earlier-worker", Decimal(20), [meter.target])
    await meter.store.reconcile("earlier-worker", Decimal(20), [meter.target])
    context._policy_request_id = "one"
    first = await service.check_budget_hierarchy(context, Decimal("0.01"), request_id="untrusted")
    assert first.allowed
    context._policy_request_id = "two"
    second = await service.check_budget_hierarchy(context, Decimal("0.01"), request_id="untrusted")
    assert not second.allowed and second.scope == "flow"
    await service.reconcile_reservation(context, "one", "unknown-model", 0, 0, actual_cost_usd=Decimal(0), usage_known=False)
    assert await meter.store.snapshot(meter.target) is None
    await service.reconcile_reservation(context, "one", "unknown-model", 1, 1, actual_cost_usd=Decimal(5))
    assert (await meter.store.snapshot(meter.target)).total_usd == 25
    context._policy_request_id = "three"
    assert (await service.check_budget_hierarchy(context, Decimal("0.01"), request_id="untrusted")).allowed


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
