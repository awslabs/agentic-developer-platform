"""A failed story cannot keep verified unused capacity from the next story."""

# ruff: noqa: F401, F811
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.budget.config import budget_config
from src.orchestration import flow_budget
from src.orchestration.execution_policy import DenyReason
from src.orchestration.models import OrchestrationExecution, OrchestrationWorkClaim
from src.shared.models.budget import BudgetSettlementReceipt
from tests.orchestration.test_policy_admission import _make_node
from tests.orchestration.test_shared_policy import dispatch, engine, session, shared


async def stopped_story(s, mutation=None):
    old = await _make_node(s.session, s.flow, node_ref="finished", state="failed", attempts=1, issue_ref="999")
    claim = OrchestrationWorkClaim(
        org_id=old.org_id,
        provider_repository_id=12345,
        issue_number=999,
        owner_kind="engine_flow",
        owner_ref=old.flow_id,
        state="released",
        generation=1,
        active_run_id=None,
        released_at=datetime.now(UTC),
        release_reason="abandoned",
    )
    s.session.add(claim)
    await s.session.flush()
    execution = OrchestrationExecution(
        org_id=old.org_id,
        flow_id=old.flow_id,
        node_id=old.id,
        cycle=1,
        phase="concluded",
        status="concluded",
        claim_id=claim.id,
        claim_generation=1,
    )
    if mutation != "missing_execution":
        s.session.add(execution)
    if mutation == "held":
        claim.state = "held"
    if mutation == "active_successor":
        claim.active_run_id = "successor"
    if mutation == "other_flow":
        claim.owner_ref = "another-flow"
    if mutation == "other_issue":
        claim.issue_number = 998
    if mutation == "generation":
        claim.generation = 2
    if mutation == "no_evidence":
        claim.released_at = None
    if mutation == "handover":
        claim.release_reason = "handover"
    if mutation == "blocked":
        execution.status = "blocked"
    if mutation == "stale_attempt":
        old.attempts = 2
    await s.session.flush()
    assert (
        await flow_budget.reserve_flow_admission(org_id=old.org_id, flow_id=old.flow_id, node_id=old.id, policy=s.policy, settled_usd=Decimal(20))
    ).admitted
    return old


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "missing_execution",
        "held",
        "active_successor",
        "other_flow",
        "other_issue",
        "generation",
        "no_evidence",
        "handover",
        "blocked",
        "stale_attempt",
    ],
)
async def test_evidenced_exit_without_worker_report_releases_only_admission(shared, monkeypatch, mutation):
    monkeypatch.setattr(budget_config, "budget_run_cap_usd", Decimal(20))
    old = await stopped_story(shared, mutation)
    original_models = await shared.client.hgetall(shared.target.key())
    decision = await dispatch(shared)
    assert decision.permitted is (mutation is None)
    target = flow_budget.flow_reservation_target(org_id=old.org_id, flow_id=old.flow_id, policy=shared.policy, settled_usd=Decimal(20))
    amount = (await shared.client.hget(target.key(), flow_budget.admission_request_id(old.id))).split(":")[0]
    assert Decimal(amount) == (0 if mutation is None else 20)
    assert await shared.client.hgetall(shared.target.key()) == original_models


async def durable_receipt(s, request_id, *, cost="1", scope=True, org=None):
    s.session.add(
        BudgetSettlementReceipt(
            org_id=org or s.flow.org_id,
            request_id=request_id,
            user_id="worker",
            allocation_key="test",
            cost_usd=Decimal(cost),
            total_tokens=10,
            reservation_scope_keys=[s.target.key()] if scope else None,
        )
    )
    await s.session.commit()


@pytest.mark.parametrize("failure", ["pending", "bounded"])
async def test_next_story_recovers_committed_usage_after_interrupted_settlement(shared, monkeypatch, failure):
    monkeypatch.setattr(budget_config, "budget_run_cap_usd", Decimal(20))
    old = await stopped_story(shared)
    assert (await shared.store.reserve("request", Decimal(20), [shared.target])).admitted
    if failure == "bounded":
        assert await shared.store.retain_failed_bound("request", shared.target)
    before = (await shared.client.hget(shared.target.key(), "request")).split(":")[1]
    await durable_receipt(shared, "request")  # SQL committed; worker died before Redis settlement.
    assert (await dispatch(shared)).permitted
    assert (await shared.store.snapshot(shared.target)).total_usd == 21  # $20 baseline plus $1 actual.
    assert (await shared.client.hget(shared.target.key(), "request")).split(":")[1] == before
    assert not await shared.client.hexists(shared.target.key(), "bounded:request")
    assert not await shared.client.hexists(shared.target.key(), "pending:request")
    assert (await dispatch(shared)).permitted  # Replay cannot debit or release twice.
    assert (await shared.store.snapshot(shared.target)).total_usd == 21
    assert old.state == "failed"  # Accounting does not claim delivery success.


@pytest.mark.parametrize("receipt", ["missing", "untrusted", "foreign", "wrong_scope"])
async def test_unresolved_usage_never_becomes_zero_to_admit_the_next_story(shared, monkeypatch, receipt):
    monkeypatch.setattr(budget_config, "budget_run_cap_usd", Decimal(20))
    assert (await shared.store.reserve("request", Decimal(20), [shared.target])).admitted
    assert await shared.store.retain_failed_bound("request", shared.target)
    if receipt != "missing":
        await durable_receipt(shared, "request", scope=receipt != "untrusted", org="other" if receipt == "foreign" else None)
        if receipt == "wrong_scope":
            row = await shared.session.get(BudgetSettlementReceipt, (shared.flow.org_id, "request"))
            row.reservation_scope_keys = ["different-flow-meter"]
            await shared.session.commit()
    before = await shared.client.hgetall(shared.target.key())
    assert not (await dispatch(shared)).permitted
    assert await shared.client.hgetall(shared.target.key()) == before


async def test_lost_meter_with_receipt_is_never_recreated(shared):
    await durable_receipt(shared, "request")
    await shared.client.flushdb()
    assert (await dispatch(shared)).reason == DenyReason.BUDGET_UNAVAILABLE
    assert await shared.client.hgetall(shared.target.key()) == {}


@pytest.mark.parametrize("known", [False, True])
async def test_proxy_usage_commit_can_recover_next_admission_without_redis_finalizer(shared, monkeypatch, known):
    from pricing_policy import RoutingEvidence, build_pricing_decision, load_snapshot, normalize_usage
    from src.shared.schemas.auth import TokenContext
    from src.usage.service import UsageService

    monkeypatch.setattr(budget_config, "budget_run_cap_usd", Decimal(20))
    await stopped_story(shared)
    assert (await shared.store.reserve("priced-request", Decimal(20), [shared.target])).admitted
    context = TokenContext(
        org_id=shared.flow.org_id, user_id="worker", team_id="", department_id="", account_type="human", expires_at=datetime.now(UTC)
    )
    context._run_scope_reservations = [shared.target]
    snapshot = load_snapshot()
    decision = build_pricing_decision(
        request_id="priced-request",
        org_id=shared.flow.org_id,
        usage=normalize_usage({"input_tokens": 10, "output_tokens": 2}, api_format="openai"),
        evidence=RoutingEvidence(
            original_model_id="openai.gpt-5.6-sol",
            billing_model_id="openai.gpt-5.6-sol",
            served_service_tier_raw="standard",
            geography="in_region",
            endpoint_region="us-east-1",
        ),
        rows=snapshot.rates,
        snapshot=snapshot,
    )
    await UsageService(shared.session).log_request(
        context=context,
        model="openai.gpt-5.6-sol",
        input_tokens=10,
        output_tokens=2,
        cost_usd=decision.ledger_cost,
        latency_ms=1,
        status_code=200,
        request_id="priced-request",
        pricing_decision=decision,
        reservation_usage_known=known,
    )
    receipt = await shared.session.get(BudgetSettlementReceipt, (shared.flow.org_id, "priced-request"))
    assert receipt.reservation_scope_keys == ([shared.target.key()] if known else None)
    assert (await dispatch(shared)).permitted is known
    total = (await shared.store.snapshot(shared.target)).total_usd
    assert total == (Decimal(20) + decision.ledger_cost if known else 40)


async def test_recovery_does_not_starve_behind_legacy_receipts(shared, monkeypatch):
    monkeypatch.setattr(budget_config, "budget_run_cap_usd", Decimal(20))
    for index in range(201):
        request = f"legacy-{index:03}"
        assert (await shared.store.reserve(request, Decimal(0), [shared.target])).admitted
        shared.session.add(
            BudgetSettlementReceipt(
                org_id=shared.flow.org_id, request_id=request, user_id="worker", allocation_key="test", cost_usd=Decimal(0), total_tokens=0
            )
        )
    assert (await shared.store.reserve("recoverable", Decimal(20), [shared.target])).admitted
    await durable_receipt(shared, "recoverable")
    assert (await dispatch(shared)).permitted
    assert (await shared.store.snapshot(shared.target)).total_usd == 21


async def test_actual_spend_above_estimate_still_blocks_next_story(shared, monkeypatch):
    monkeypatch.setattr(budget_config, "budget_run_cap_usd", Decimal(20))
    assert (await shared.store.reserve("request", Decimal(1), [shared.target])).admitted
    await durable_receipt(shared, "request", cost="35")
    assert not (await dispatch(shared)).permitted
    assert (await shared.store.snapshot(shared.target)).total_usd == 55


async def test_outstanding_handoff_keeps_admission_even_with_released_claim(shared, monkeypatch):
    from sqlalchemy import select

    from src.orchestration.execution_state import ExecutionIdentity
    from src.orchestration.handoff import handoff_receipt_ref

    monkeypatch.setattr(budget_config, "budget_run_cap_usd", Decimal(20))
    old = await stopped_story(shared)
    claim = await shared.session.scalar(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.issue_number == 999))
    next_execution = OrchestrationExecution(
        org_id=old.org_id,
        flow_id=old.flow_id,
        node_id=old.id,
        cycle=2,
        phase="awaiting_review",
        status="blocked",
        claim_id=claim.id,
        claim_generation=claim.generation,
    )
    shared.session.add(next_execution)
    await shared.session.flush()
    identity = ExecutionIdentity(
        org_id=old.org_id,
        node_id=old.id,
        cycle=2,
        accepted_plan_version=next_execution.accepted_plan_version,
        claim_id=claim.id,
        claim_generation=claim.generation,
    )
    next_execution.handoff_receipt_ref = handoff_receipt_ref(identity, next_execution.id)
    await shared.session.flush()
    assert not (await dispatch(shared)).permitted
