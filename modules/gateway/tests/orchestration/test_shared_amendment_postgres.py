"""Real PostgreSQL races preserve shared plans across dispatch and resubmission."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.budget.enforcement_settings import BudgetAccountingGap, BudgetEnforcementSetting
from src.orchestration.amend import AmendmentContext, amend_plan
from src.orchestration.amendment_impact import assess_amendment
from src.orchestration.amendment_review_routes import _impact_model
from src.orchestration.compile import ExpiredExecutionPolicyError, ProposalRejectedError, compile_proposal
from src.orchestration.continuation import digest
from src.orchestration.dispatch_pass import _fetch_ready_nodes
from src.orchestration.execution_policy import Action
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationAmendmentRequest,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from src.orchestration.plan_lineage import MAX_HOPS
from src.orchestration.policy_admission import load_in_force_policy
from src.orchestration.routes import AmendmentResponse
from src.orchestration.run_reports import OrchestrationRunReport
from src.orchestration.shared_amendment import SharedAppendError, accept_shared_append
from src.orchestration.shared_budget import CONTRACT as BUDGET_CONTRACT
from src.orchestration.shared_budget import (
    BudgetIncreaseError,
    BudgetIncreaseRequest,
    FinancialLimits,
    accept_budget_increase,
    apply_limits,
    preview_budget_increase,
)
from src.orchestration.shared_concurrency import ConcurrencyIncreaseRequest, accept_concurrency_increase, preview_concurrency_increase
from src.orchestration.shared_policy import shared_inputs
from src.orchestration.shared_retry import CONTRACT as RETRY_CONTRACT
from src.orchestration.shared_retry import KIND as RETRY_KIND
from src.orchestration.shared_retry import RetryIncreaseError, RetryIncreaseRequest, accept_retry_increase, preview_retry_increase
from src.orchestration.shared_window import WindowRenewalRequest, accept_window_renewal, preview_window_renewal
from src.shared.models.base import Base
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401
from tests.orchestration.legacy_plan import persist_legacy_plan
from tests.orchestration.test_amend import address, approval, valid_proposal  # noqa: F401
from tests.orchestration.test_policy_admission import _limits, _policy
from tests.orchestration.test_shared_amendment import appendable, preview  # noqa: F401


@pytest.fixture
async def session(pg_url):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url))
    models = [
        BudgetEnforcementSetting,
        BudgetAccountingGap,
        OrchestrationFlow,
        OrchestrationNode,
        OrchestrationEdge,
        OrchestrationAcceptedPlan,
        OrchestrationDecision,
        OrchestrationWorkClaim,
        OrchestrationExecution,
        OrchestrationAction,
        OrchestrationRunReport,
        OrchestrationAmendmentRequest,
        OrchestrationPullRequestBinding,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: Base.metadata.create_all(c, tables=[m.__table__ for m in models]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.info["factory"] = factory
        yield db
    await engine.dispose()


async def test_receipt_survives_compatible_amendment(session, approval):  # noqa: F811
    policy = _policy()
    proposal = valid_proposal(execution_policy=policy)
    compiled = await persist_legacy_plan(session, proposal, approval)
    base = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == compiled.flow_id))
    accepted = base.plan_document["execution_policy"]
    budget = OrchestrationDecision(
        org_id=approval.org_id,
        flow_id=compiled.flow_id,
        kind="budget_increased",
        actor_id=approval.actor_id,
        actor_role="platform_admin",
        actor_kind="human",
        reason=json.dumps(
            {
                "contract": BUDGET_CONTRACT,
                "flow_id": compiled.flow_id,
                "plan_version": base.version,
                "plan_hash": base.plan_hash,
                "original_policy_hash": accepted["policy_hash"],
                "principal_id": accepted["principal_id"],
                "limits": {"max_spend_usd": "75", "max_run_spend_usd": "75", "max_chain_spend_usd": "75"},
            }
        ),
    )
    retry = OrchestrationDecision(
        org_id=approval.org_id,
        flow_id=compiled.flow_id,
        kind=RETRY_KIND,
        actor_id=approval.actor_id,
        actor_role="platform_admin",
        actor_kind="human",
        reason=json.dumps(
            {
                "contract": RETRY_CONTRACT,
                "flow_id": compiled.flow_id,
                "plan_version": base.version,
                "plan_hash": base.plan_hash,
                "original_policy_hash": accepted["policy_hash"],
                "principal_id": accepted["principal_id"],
                "max_attempts_per_node": 5,
            }
        ),
    )
    session.add_all([budget, retry])
    await session.flush()
    amendment = valid_proposal(execution_policy=policy, title="Content revision")
    result = await amend_plan(
        session,
        compiled.flow_id,
        amendment,
        AmendmentContext(org_id=approval.org_id, actor_id=approval.actor_id, actor_role=approval.actor_role),
    )
    assert result.plan_version == 2
    effective = await load_in_force_policy(session, org_id=approval.org_id, flow_id=compiled.flow_id)
    assert effective.refusal is None
    assert effective.policy.limits.max_spend_usd == 75
    assert effective.policy._shared_run_spend_usd == 75
    assert effective.policy.limits.max_attempts_per_node == 5
    assert effective.policy._shared_budget_decision_id == budget.id
    assert effective.policy._shared_retry_decision_id == retry.id
    latest = await session.scalar(
        select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == compiled.flow_id, OrchestrationAcceptedPlan.version == 2)
    )
    original_hash = latest.plan_hash
    latest.plan_hash = "0" * 64
    await session.flush()
    unsafe = await load_in_force_policy(session, org_id=approval.org_id, flow_id=compiled.flow_id)
    assert unsafe.policy is not None
    assert unsafe.policy._shared_budget_decision_id is None
    assert unsafe.policy._shared_retry_decision_id is None
    latest.plan_hash = original_hash
    base.accepted_by_decision_id = None
    await session.flush()
    missing_history = await load_in_force_policy(session, org_id=approval.org_id, flow_id=compiled.flow_id)
    assert missing_history.policy is not None
    assert missing_history.policy._shared_budget_decision_id is None
    assert missing_history.policy._shared_retry_decision_id is None


async def test_accepted_receipts_reach_readers_after_content_amendment(session, approval, monkeypatch):  # noqa: F811
    policy = _policy()
    proposal = valid_proposal(execution_policy=policy)
    compiled = await persist_legacy_plan(session, proposal, approval)
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == compiled.flow_id))
    actor = replace(approval, actor_role="platform_admin")
    meter = AsyncMock(return_value=SimpleNamespace(total_usd=Decimal("0")))
    monkeypatch.setattr("src.orchestration.shared_policy.reconcile_flow_meter", meter)
    budget_request = BudgetIncreaseRequest(
        expected_plan_version=plan.version,
        expected_plan_hash=plan.plan_hash,
        limits={"max_spend_usd": "75", "max_run_spend_usd": "75", "max_chain_spend_usd": "75"},
        reason="Platform owner approves this flow's larger run cap.",
    )
    budget_preview = await preview_budget_increase(session, flow_id=compiled.flow_id, actor=actor, request=budget_request)
    budget = await accept_budget_increase(
        session,
        flow_id=compiled.flow_id,
        actor=actor,
        request=budget_request.model_copy(update={"expected_snapshot": budget_preview["snapshot"]}),
    )
    retry_request = RetryIncreaseRequest(
        expected_plan_version=plan.version,
        expected_plan_hash=plan.plan_hash,
        max_attempts_per_node=5,
        reason="Platform owner approves five attempts on this flow.",
    )
    retry_preview = await preview_retry_increase(session, flow_id=compiled.flow_id, actor=actor, request=retry_request)
    retry = await accept_retry_increase(
        session,
        flow_id=compiled.flow_id,
        actor=actor,
        request=retry_request.model_copy(update={"expected_snapshot": retry_preview["snapshot"]}),
    )
    nodes = [node.model_copy(update={"title": "Updated story content"}) if index == 0 else node for index, node in enumerate(proposal.nodes)]
    content_proposal = valid_proposal(execution_policy=policy, nodes=nodes)
    content_preview = await assess_amendment(
        session,
        org_id=approval.org_id,
        flow_id=compiled.flow_id,
        proposal=content_proposal,
        actor_id=approval.actor_id,
    )
    assert {item["kind"] for item in content_preview.receipts_preserved} == {"budget", "retry"}
    assert content_preview.effective_limits_after == content_preview.effective_limits_before
    amended = await amend_plan(
        session,
        compiled.flow_id,
        valid_proposal(execution_policy=policy, nodes=nodes),
        AmendmentContext(org_id=approval.org_id, actor_id=approval.actor_id, actor_role=approval.actor_role),
    )
    assert amended.plan_version == 2
    assert content_preview.receipts_preserved == amended.receipts_preserved
    assert {item["kind"] for item in amended.receipts_preserved} == {"budget", "retry"}
    assert amended.receipts_dropped == []
    expected_limits = vars(content_preview.effective_limits_before)
    assert amended.effective_limits_before == amended.effective_limits_after == expected_limits
    assert AmendmentResponse.model_validate(amended, from_attributes=True).effective_limits_after["max_spend_usd"] == "75"
    amendment_decision = await session.get(OrchestrationDecision, amended.decision_id)
    saved = json.loads(amendment_decision.reason.rpartition("[receipt_disposition/v1] ")[2])
    assert saved["receipts_preserved"] == amended.receipts_preserved
    assert saved["effective_limits_before"] == saved["effective_limits_after"] == expected_limits
    readback = await load_in_force_policy(session, org_id=approval.org_id, flow_id=compiled.flow_id)
    assert readback.refusal is None
    assert readback.policy.limits.max_spend_usd == 75
    assert readback.policy._shared_run_spend_usd == 75
    assert readback.policy.limits.max_attempts_per_node == 5
    assert readback.policy._shared_budget_decision_id == budget["decision_id"]
    assert readback.policy._shared_retry_decision_id == retry["decision_id"]
    next_budget = await preview_budget_increase(
        session,
        flow_id=compiled.flow_id,
        actor=actor,
        request=budget_request.model_copy(
            update={
                "expected_plan_version": 2,
                "expected_plan_hash": amended.plan_hash,
                "limits": FinancialLimits(max_spend_usd=100, max_run_spend_usd=100, max_chain_spend_usd=100),
            }
        ),
    )
    next_retry = await preview_retry_increase(
        session,
        flow_id=compiled.flow_id,
        actor=actor,
        request=retry_request.model_copy(update={"expected_plan_version": 2, "expected_plan_hash": amended.plan_hash, "max_attempts_per_node": 7}),
    )
    assert next_budget["before"]["max_run_spend_usd"] == "75"
    assert next_retry["before"] == 5
    extended = apply_limits(
        readback.policy,
        FinancialLimits(max_spend_usd=80, max_run_spend_usd=75, max_chain_spend_usd=80),
        budget["decision_id"],
    )
    assert extended._shared_retry_decision_id == retry["decision_id"]
    assert extended._shared_run_spend_usd == 75
    assert meter.await_count == 3
    assert (
        len(list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind.in_(["budget_increased", RETRY_KIND]))))) == 2
    )


@pytest.mark.timeout(120)
async def test_receipt_dispositions_at_lineage_boundary(session, approval, monkeypatch):  # noqa: F811
    monkeypatch.setattr(
        "src.orchestration.shared_policy.reconcile_flow_meter",
        AsyncMock(return_value=SimpleNamespace(total_usd=Decimal("0"))),
    )
    proposal = valid_proposal(execution_policy=_policy())
    compiled = await persist_legacy_plan(session, proposal, approval)
    actor = replace(approval, actor_role="platform_admin")
    request = BudgetIncreaseRequest(
        expected_plan_version=1,
        expected_plan_hash=compiled.plan_hash,
        limits=FinancialLimits(max_spend_usd=75, max_run_spend_usd=75, max_chain_spend_usd=75),
        reason="Approve larger run and flow caps.",
    )
    preview = await preview_budget_increase(session, flow_id=compiled.flow_id, actor=actor, request=request)
    budget = await accept_budget_increase(
        session, flow_id=compiled.flow_id, actor=actor, request=request.model_copy(update={"expected_snapshot": preview["snapshot"]})
    )
    amender = AmendmentContext(org_id=approval.org_id, actor_id=approval.actor_id, actor_role=approval.actor_role)
    for version in range(2, MAX_HOPS + 2):
        proposal = proposal.model_copy(update={"title": f"Content revision {version}"})
        amended = await amend_plan(session, compiled.flow_id, proposal, amender)
    assert amended.plan_version == 101
    before = await load_in_force_policy(session, org_id=approval.org_id, flow_id=compiled.flow_id)
    assert before.policy._shared_run_spend_usd == 75
    request = RetryIncreaseRequest(
        expected_plan_version=amended.plan_version,
        expected_plan_hash=amended.plan_hash,
        max_attempts_per_node=5,
        reason="Approve five attempts at the current revision.",
    )
    preview = await preview_retry_increase(session, flow_id=compiled.flow_id, actor=actor, request=request)
    retry = await accept_retry_increase(
        session, flow_id=compiled.flow_id, actor=actor, request=request.model_copy(update={"expected_snapshot": preview["snapshot"]})
    )
    proposal = proposal.model_copy(update={"title": "Content revision 102"})
    preview = await assess_amendment(session, org_id=approval.org_id, flow_id=compiled.flow_id, proposal=proposal, actor_id=approval.actor_id)
    assert not preview.blocking
    assert [item["decision_id"] for item in preview.receipts_preserved] == [retry["decision_id"]]
    assert [item["decision_id"] for item in preview.receipts_dropped] == [budget["decision_id"]]
    dropped = preview.receipts_dropped[0]
    assert dropped["reason"] == "receipt_lineage_limit_exceeded"
    assert dropped["before"]["max_run_spend_usd"] == "75"
    assert dropped["after"]["max_run_spend_usd"] == "25.00"
    assert Decimal(preview.effective_limits_before.max_spend_usd) == 75
    assert Decimal(preview.effective_limits_after.max_spend_usd) == 50
    assert preview.effective_limits_after.max_attempts_per_node == 5
    amended = await amend_plan(session, compiled.flow_id, proposal, amender)
    assert amended.plan_version == 102
    assert amended.receipts_dropped == preview.receipts_dropped
    assert amended.receipts_preserved == preview.receipts_preserved
    assert amended.effective_limits_after == vars(preview.effective_limits_after)
    readback = await load_in_force_policy(session, org_id=approval.org_id, flow_id=compiled.flow_id)
    assert readback.refusal is None
    assert readback.policy.limits.max_spend_usd == 50
    assert readback.policy._shared_run_spend_usd is None
    assert readback.policy.limits.max_attempts_per_node == 5
    replay = await amend_plan(session, compiled.flow_id, proposal, amender)
    assert replay.already_amended
    assert replay.receipts_dropped == amended.receipts_dropped
    assert replay.receipts_preserved == amended.receipts_preserved
    assert replay.effective_limits_after == amended.effective_limits_after


@pytest.mark.parametrize("condition", ["compatible", "expired", "incompatible"])
async def test_amendment_uses_preserved_window_expiry(session, approval, monkeypatch, condition):  # noqa: F811
    fixed = datetime(2030, 1, 1, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, timezone=None):
            return fixed if timezone is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr("src.orchestration.compile.utcnow", lambda: fixed)
    monkeypatch.setattr("src.orchestration.shared_window.datetime", FrozenDatetime)
    original_expiry = fixed + timedelta(minutes=15)
    renewed_expiry = fixed + timedelta(hours=2)
    proposal = valid_proposal(execution_policy=_policy(expires_at=original_expiry))
    compiled = await persist_legacy_plan(session, proposal, approval)
    session.add(
        OrchestrationDecision(
            org_id=approval.org_id,
            flow_id=compiled.flow_id,
            kind="node_dispatched",
            actor_id=approval.actor_id,
            actor_role=approval.actor_role,
            actor_kind="human",
            reason="first dispatch",
            created_at=fixed,
        )
    )
    await session.flush()
    actor = replace(approval, actor_role="platform_admin")
    request = WindowRenewalRequest(
        expected_plan_version=1,
        expected_plan_hash=compiled.plan_hash,
        expires_at=renewed_expiry,
        reason="Approve a two-hour execution window.",
    )
    preview = await preview_window_renewal(session, flow_id=compiled.flow_id, actor=actor, request=request)
    window = await accept_window_renewal(
        session, flow_id=compiled.flow_id, actor=actor, request=request.model_copy(update={"expected_snapshot": preview["snapshot"]})
    )
    fixed += timedelta(minutes=30)
    proposal = proposal.model_copy(update={"title": "Content amended during the renewed window"})
    if condition == "expired":
        fixed = renewed_expiry
    elif condition == "incompatible":
        proposal = proposal.model_copy(update={"execution_policy": _policy(expires_at=original_expiry, limits=_limits(max_spend_usd=40))})
    preview = await assess_amendment(session, org_id=approval.org_id, flow_id=compiled.flow_id, proposal=proposal, actor_id=approval.actor_id)
    amender = AmendmentContext(org_id=approval.org_id, actor_id=approval.actor_id, actor_role=approval.actor_role)
    if condition != "compatible":
        assert "expired_execution_policy" in {item.rule for item in preview.blocking}
        with pytest.raises(ExpiredExecutionPolicyError):
            await amend_plan(session, compiled.flow_id, proposal, amender)
        return
    assert not preview.blocking
    assert preview.effective_limits_after.expires_at == renewed_expiry.isoformat()
    amended = await amend_plan(session, compiled.flow_id, proposal, amender)
    assert amended.receipts_preserved == preview.receipts_preserved
    assert amended.receipts_dropped == []
    assert amended.effective_limits_after["expires_at"] == renewed_expiry.isoformat()
    readback = await load_in_force_policy(session, org_id=approval.org_id, flow_id=compiled.flow_id)
    assert readback.refusal is None
    assert readback.policy.expires_at == renewed_expiry
    assert readback.policy._shared_window_decision_id == window["decision_id"]
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.version == 2))
    assert datetime.fromisoformat(plan.plan_document["execution_policy"]["expires_at"]) == original_expiry
    decision = await session.get(OrchestrationDecision, window["decision_id"])
    assert datetime.fromisoformat(json.loads(decision.reason)["expires_at"]) == renewed_expiry
    fixed = renewed_expiry + timedelta(minutes=1)
    replay = await amend_plan(session, compiled.flow_id, proposal, amender)
    assert replay.already_amended
    assert replay.receipts_preserved == amended.receipts_preserved
    assert replay.effective_limits_after == amended.effective_limits_after


@pytest.mark.parametrize(
    "change,reason",
    [
        ("limit", "base_limits_changed"),
        ("repository", "repository_scope_changed"),
        ("actions", "policy_authority_changed"),
        ("expiry", "policy_authority_changed"),
    ],
)
async def test_amendment_reports_dropped_receipts(session, approval, change, reason):  # noqa: F811
    original = _policy()
    proposal = valid_proposal(execution_policy=original)
    compiled = await persist_legacy_plan(session, proposal, approval)
    base = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == compiled.flow_id))
    budget = OrchestrationDecision(
        org_id=approval.org_id,
        flow_id=compiled.flow_id,
        kind="budget_increased",
        actor_id=approval.actor_id,
        actor_role="platform_admin",
        actor_kind="human",
        reason=json.dumps(
            {
                "contract": BUDGET_CONTRACT,
                "flow_id": compiled.flow_id,
                "plan_version": base.version,
                "plan_hash": base.plan_hash,
                "original_policy_hash": base.plan_document["execution_policy"]["policy_hash"],
                "principal_id": approval.actor_id,
                "limits": {"max_spend_usd": "75", "max_run_spend_usd": "75", "max_chain_spend_usd": "75"},
            }
        ),
    )
    session.add(budget)
    await session.flush()
    if change == "limit":
        replacement = _policy(limits=_limits(max_spend_usd=Decimal("40")))
    elif change == "repository":
        replacement = _policy(repository_ids=["other/repository"])
    elif change == "actions":
        replacement = _policy(allowed_actions=[*original.allowed_actions, Action.REVIEW])
    else:
        replacement = _policy(expires_at=original.expires_at + timedelta(days=1))
    amended_proposal = valid_proposal(execution_policy=replacement, title="Revised plan")
    actor = AmendmentContext(org_id=approval.org_id, actor_id=approval.actor_id, actor_role=approval.actor_role)
    preview = await assess_amendment(
        session,
        org_id=approval.org_id,
        flow_id=compiled.flow_id,
        proposal=amended_proposal,
        actor_id=approval.actor_id,
    )
    assert preview.effective_limits_before.max_spend_usd == "75"
    assert preview.effective_limits_after.max_spend_usd == ("40" if change == "limit" else "50.00")
    result = await amend_plan(session, compiled.flow_id, amended_proposal, actor)
    expected_before = vars(preview.effective_limits_before)
    expected_after = vars(preview.effective_limits_after)
    assert result.effective_limits_before == expected_before
    assert result.effective_limits_after == expected_after
    assert result.receipts_preserved == []
    assert preview.receipts_dropped == result.receipts_dropped
    assert _impact_model(preview).model_dump(mode="json")["receipts_dropped"] == result.receipts_dropped
    assert len(result.receipts_dropped) == 1
    dropped = result.receipts_dropped[0]
    assert dropped == {
        "kind": "budget",
        "decision_id": budget.id,
        "before": {"max_spend_usd": "75", "max_run_spend_usd": "75", "max_chain_spend_usd": "75"},
        "after": {
            "max_spend_usd": "40" if change == "limit" else "50.00",
            "max_run_spend_usd": "25.00",
            "max_chain_spend_usd": "40" if change == "limit" else "50.00",
        },
        "reason": reason,
    }
    decision = await session.get(OrchestrationDecision, result.decision_id)
    saved = json.loads(decision.reason.rpartition("[receipt_disposition/v1] ")[2])
    assert saved["receipts_dropped"] == [dropped]
    assert saved["effective_limits_before"] == expected_before
    assert saved["effective_limits_after"] == expected_after
    replay = await amend_plan(session, compiled.flow_id, amended_proposal, actor)
    assert replay.already_amended and replay.decision_id == decision.id
    assert replay.receipts_dropped == result.receipts_dropped
    assert replay.effective_limits_before == expected_before
    assert replay.effective_limits_after == expected_after
    api = AmendmentResponse.model_validate(result, from_attributes=True).model_dump(mode="json")
    assert api["receipts_dropped"] == [dropped]
    assert api["effective_limits_before"] == expected_before
    assert api["effective_limits_after"] == expected_after
    assert len(list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "plan_amended")))) == 1
    connection = await session.connection()
    await connection.execute(
        OrchestrationDecision.__table__.update()
        .where(OrchestrationDecision.id == decision.id)
        .values(reason=decision.reason.rpartition("[receipt_disposition/v1] ")[0])
    )
    session.expire(decision)
    with pytest.raises(ProposalRejectedError, match="receipt disposition missing"):
        await amend_plan(session, compiled.flow_id, amended_proposal, actor)


@pytest.mark.parametrize("first", ["increase", "amendment"])
@pytest.mark.parametrize("kind", ["budget", "retry"])
async def test_amendment_and_increase_share_flow_lock(session, approval, monkeypatch, first, kind):  # noqa: F811
    policy = _policy()
    proposal = valid_proposal(execution_policy=policy)
    compiled = await persist_legacy_plan(session, proposal, approval)
    base = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == compiled.flow_id))
    actor = replace(approval, actor_role="platform_admin")
    monkeypatch.setattr(
        "src.orchestration.shared_policy.reconcile_flow_meter",
        AsyncMock(return_value=SimpleNamespace(total_usd=Decimal("0"))),
    )
    if kind == "budget":
        request = BudgetIncreaseRequest(
            expected_plan_version=base.version,
            expected_plan_hash=base.plan_hash,
            limits=FinancialLimits(max_spend_usd=75, max_run_spend_usd=75, max_chain_spend_usd=75),
            reason="Platform owner approves this flow's larger run cap.",
        )
    else:
        request = RetryIncreaseRequest(
            expected_plan_version=base.version,
            expected_plan_hash=base.plan_hash,
            max_attempts_per_node=5,
            reason="Platform owner approves this flow's retry increase.",
        )
    preview_increase = preview_budget_increase if kind == "budget" else preview_retry_increase
    accept_increase = accept_budget_increase if kind == "budget" else accept_retry_increase
    expected = await preview_increase(session, flow_id=compiled.flow_id, actor=actor, request=request)
    request = request.model_copy(update={"expected_snapshot": expected["snapshot"]})
    nodes = [node.model_copy(update={"title": "Updated story content"}) if index == 0 else node for index, node in enumerate(proposal.nodes)]
    amendment = valid_proposal(execution_policy=policy, nodes=nodes)
    amender = AmendmentContext(org_id=approval.org_id, actor_id=approval.actor_id, actor_role=approval.actor_role)
    await session.commit()
    queued = None
    async with session.info["factory"]() as writer:
        await writer.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == compiled.flow_id).with_for_update())
        try:
            if first == "increase":
                queued = asyncio.create_task(amend_plan(session, compiled.flow_id, amendment, amender))
            else:
                queued = asyncio.create_task(accept_increase(session, flow_id=compiled.flow_id, actor=actor, request=request))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(queued), timeout=0.1)
            if first == "increase":
                accepted = await accept_increase(writer, flow_id=compiled.flow_id, actor=actor, request=request)
            else:
                committed = await amend_plan(writer, compiled.flow_id, amendment, amender)
            await writer.commit()
            if first == "increase":
                committed = await asyncio.wait_for(queued, timeout=3)
            else:
                error = BudgetIncreaseError if kind == "budget" else RetryIncreaseError
                with pytest.raises(error, match="accepted_plan_changed"):
                    await asyncio.wait_for(queued, timeout=3)
        finally:
            if queued is not None and not queued.done():
                queued.cancel()
                await asyncio.gather(queued, return_exceptions=True)

    assert committed.plan_version == 2
    readback = await load_in_force_policy(session, org_id=approval.org_id, flow_id=compiled.flow_id)
    assert readback.refusal is None
    decision_kind = "budget_increased" if kind == "budget" else RETRY_KIND
    decisions = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == decision_kind)))
    if first == "increase":
        assert len(decisions) == 1 and decisions[0].id == accepted["decision_id"]
        if kind == "budget":
            assert readback.policy._shared_budget_decision_id == decisions[0].id
            assert readback.policy._shared_run_spend_usd == 75
        else:
            assert readback.policy._shared_retry_decision_id == decisions[0].id
            assert readback.policy.limits.max_attempts_per_node == 5
        assert [item["decision_id"] for item in committed.receipts_preserved] == [decisions[0].id]
    else:
        assert decisions == []
        assert readback.policy._shared_budget_decision_id is None and readback.policy._shared_retry_decision_id is None
        assert committed.receipts_preserved == committed.receipts_dropped == []
    amendment_decision = await session.get(OrchestrationDecision, committed.decision_id)
    assert json.loads(amendment_decision.reason.rpartition("[receipt_disposition/v1] ")[2])["receipts_preserved"] == committed.receipts_preserved
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == compiled.flow_id)))) == 2


@pytest.mark.parametrize("display", [False, True])
@pytest.mark.parametrize("tamper", [None, "hash", "tenant", "repository", "actions"])
async def test_four_receipts_cross_two_amendments_without_renewing_expiry(session, approval, monkeypatch, tamper, display):  # noqa: F811
    from src.orchestration.plan_lineage import PolicyDisplayLineage

    fixed = datetime(2030, 1, 1, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, timezone=None):
            return fixed if timezone is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr("src.orchestration.compile.utcnow", lambda: fixed)
    monkeypatch.setattr("src.orchestration.shared_window.datetime", FrozenDatetime)
    monkeypatch.setattr("src.orchestration.shared_concurrency.datetime", FrozenDatetime)
    monkeypatch.setattr(
        "src.orchestration.shared_policy.reconcile_flow_meter",
        AsyncMock(return_value=SimpleNamespace(total_usd=Decimal("0"))),
    )
    policy = _policy(expires_at=fixed + timedelta(minutes=15))
    proposal = valid_proposal(execution_policy=policy)
    compiled = await persist_legacy_plan(session, proposal, approval)
    base = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == compiled.flow_id))
    session.add(
        OrchestrationDecision(
            org_id=approval.org_id,
            flow_id=compiled.flow_id,
            kind="node_dispatched",
            actor_id=approval.actor_id,
            actor_role=approval.actor_role,
            actor_kind="human",
            reason="first dispatch",
            created_at=fixed,
        )
    )
    await session.flush()
    actor = replace(approval, actor_role="platform_admin")

    budget_request = BudgetIncreaseRequest(
        expected_plan_version=1,
        expected_plan_hash=base.plan_hash,
        limits=FinancialLimits(max_spend_usd=75, max_run_spend_usd=75, max_chain_spend_usd=75),
        reason="Platform owner approves this flow's run cap.",
    )
    budget_preview = await preview_budget_increase(session, flow_id=compiled.flow_id, actor=actor, request=budget_request)
    budget = await accept_budget_increase(
        session,
        flow_id=compiled.flow_id,
        actor=actor,
        request=budget_request.model_copy(update={"expected_snapshot": budget_preview["snapshot"]}),
    )
    retry_request = RetryIncreaseRequest(
        expected_plan_version=1,
        expected_plan_hash=base.plan_hash,
        max_attempts_per_node=5,
        reason="Platform owner approves five attempts.",
    )
    retry_preview = await preview_retry_increase(session, flow_id=compiled.flow_id, actor=actor, request=retry_request)
    retry = await accept_retry_increase(
        session,
        flow_id=compiled.flow_id,
        actor=actor,
        request=retry_request.model_copy(update={"expected_snapshot": retry_preview["snapshot"]}),
    )
    renewed_expiry = fixed + timedelta(hours=2)
    window_request = WindowRenewalRequest(
        expected_plan_version=1,
        expected_plan_hash=base.plan_hash,
        expires_at=renewed_expiry,
        reason="Platform owner approves a longer delivery window.",
    )
    window_preview = await preview_window_renewal(session, flow_id=compiled.flow_id, actor=actor, request=window_request)
    window = await accept_window_renewal(
        session,
        flow_id=compiled.flow_id,
        actor=actor,
        request=window_request.model_copy(update={"expected_snapshot": window_preview["snapshot"]}),
    )
    concurrency_request = ConcurrencyIncreaseRequest(
        expected_plan_version=1,
        expected_plan_hash=base.plan_hash,
        max_concurrent_actions=7,
        reason="Platform owner approves seven workers.",
    )
    concurrency_preview = await preview_concurrency_increase(session, flow_id=compiled.flow_id, actor=actor, request=concurrency_request)
    concurrency = await accept_concurrency_increase(
        session,
        flow_id=compiled.flow_id,
        actor=actor,
        request=concurrency_request.model_copy(update={"expected_snapshot": concurrency_preview["snapshot"]}),
    )
    sources = {
        "budget": budget["decision_id"],
        "retry": retry["decision_id"],
        "window": window["decision_id"],
        "concurrency": concurrency["decision_id"],
    }
    amender = AmendmentContext(org_id=approval.org_id, actor_id=approval.actor_id, actor_role=approval.actor_role)
    for version in (2, 3):
        nodes = [node.model_copy(update={"title": f"Story content v{version}"}) if index == 0 else node for index, node in enumerate(proposal.nodes)]
        amendment = valid_proposal(execution_policy=policy, nodes=nodes)
        preview = await assess_amendment(
            session,
            org_id=approval.org_id,
            flow_id=compiled.flow_id,
            proposal=amendment,
            actor_id=approval.actor_id,
        )
        result = await amend_plan(session, compiled.flow_id, amendment, amender)
        assert result.plan_version == version and result.receipts_dropped == []
        assert {item["kind"]: item["decision_id"] for item in result.receipts_preserved} == sources
        assert preview.receipts_preserved == result.receipts_preserved
        effective = await load_in_force_policy(
            session, org_id=approval.org_id, flow_id=compiled.flow_id, display_lineage=PolicyDisplayLineage() if display else None
        )
        assert effective.refusal is None
        assert effective.policy._shared_run_spend_usd == 75
        assert effective.policy.limits.max_attempts_per_node == 5
        assert effective.policy.expires_at == renewed_expiry
        assert effective.policy.limits.max_concurrent_actions == 7
        assert {
            "budget": effective.policy._shared_budget_decision_id,
            "retry": effective.policy._shared_retry_decision_id,
            "window": effective.policy._shared_window_decision_id,
            "concurrency": effective.policy._shared_concurrency_decision_id,
        } == sources
        decision = await session.get(OrchestrationDecision, result.decision_id)
        assert json.loads(decision.reason.rpartition("[receipt_disposition/v1] ")[2])["receipts_preserved"] == result.receipts_preserved

    assert (
        len(
            list(
                await session.scalars(
                    select(OrchestrationDecision).where(
                        OrchestrationDecision.kind.in_(["budget_increased", RETRY_KIND, "execution_window_renewed", "concurrency_limit_increased"])
                    )
                )
            )
        )
        == 4
    )
    assert json.loads((await session.get(OrchestrationDecision, window["decision_id"])).reason)["expires_at"] == renewed_expiry.isoformat()

    if tamper is not None:
        intermediate = await session.scalar(
            select(OrchestrationAcceptedPlan).where(
                OrchestrationAcceptedPlan.flow_id == compiled.flow_id,
                OrchestrationAcceptedPlan.version == 2,
            )
        )
        if tamper == "hash":
            intermediate.plan_hash = "0" * 64
        elif tamper == "tenant":
            intermediate.org_id = "unrelated-org"
        else:
            document = dict(intermediate.plan_document)
            document["execution_policy"] = dict(document["execution_policy"])
            field = "repository_ids" if tamper == "repository" else "allowed_actions"
            document["execution_policy"][field] = ["unrelated/repo" if tamper == "repository" else "evaluate"]
            intermediate.plan_document = document
        await session.flush()
        refused = await load_in_force_policy(
            session, org_id=approval.org_id, flow_id=compiled.flow_id, display_lineage=PolicyDisplayLineage() if display else None
        )
        assert refused.refusal is None
        assert refused.policy._shared_budget_decision_id is None
        assert refused.policy._shared_retry_decision_id is None
        assert refused.policy._shared_window_decision_id is None
        assert refused.policy._shared_concurrency_decision_id is None
        assert refused.policy.expires_at == policy.expires_at


async def test_worker_committing_while_append_waits_refreshes_active_node_state(session, appendable):  # noqa: F811
    ctx = appendable
    expected = await preview(session, ctx)
    request = ctx.request.model_copy(update={"expected_snapshot": expected["snapshot"]})
    stale = await session.get(OrchestrationNode, ctx.nodes[address("story-a")])
    assert stale.state == "pending"
    await session.commit()
    async with session.info["factory"]() as dispatcher:
        await dispatcher.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).with_for_update())
        running = await dispatcher.scalar(select(OrchestrationNode).where(OrchestrationNode.id == stale.id).with_for_update())
        task = asyncio.create_task(accept_shared_append(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            running.state, running.attempts = "running", 1
            await dispatcher.commit()
            with pytest.raises(SharedAppendError, match="active_nodes_cross_plan_boundary"):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1
    current = await session.get(OrchestrationNode, ctx.nodes[address("story-a")], populate_existing=True)
    assert current.state == "running" and current.attempts == 1


async def test_append_yields_to_production_dispatch_node_then_flow_lock_order(session, appendable):  # noqa: F811
    ctx = appendable
    flow = await session.get(OrchestrationFlow, ctx.flow.id)
    flow.execution_paused = False
    ready = await session.get(OrchestrationNode, ctx.nodes[address("story-a")])
    ready.state = "ready"
    await session.commit()
    expected = await preview(session, ctx)
    request = ctx.request.model_copy(update={"expected_snapshot": expected["snapshot"]})
    await session.commit()
    async with session.info["factory"]() as dispatcher:
        # Use the actual scheduler query: it takes READY node locks BEFORE
        # authorize_shared_dispatch calls shared_inputs(lock=True).
        fetched = await _fetch_ready_nodes(dispatcher, limit=10)
        assert [node.id for node in fetched] == [ready.id]
        with pytest.raises(SharedAppendError, match="amendment_dispatch_in_progress"):
            await asyncio.wait_for(accept_shared_append(session, flow_id=ctx.flow.id, actor=ctx.actor, request=request), timeout=2)
        # Do not roll back the request session: the failed append's savepoint
        # must have released its flow/plan locks without relying on route cleanup.
        inputs, marker = await asyncio.wait_for(shared_inputs(dispatcher, org_id=ctx.actor.org_id, flow_id=ctx.flow.id, lock=True), timeout=2)
        assert inputs.plan_version == ctx.plan.version
        assert marker == ctx.original["execution_continuation"]
        fetched[0].state, fetched[0].attempts = "running", 1
        await dispatcher.commit()
    plans = list(await session.scalars(select(OrchestrationAcceptedPlan)))
    assert len(plans) == 1 and plans[0].plan_document == ctx.original
    assert await session.scalar(select(OrchestrationNode).where(OrchestrationNode.node_ref == "producer")) is None
    current = await session.get(OrchestrationNode, ready.id, populate_existing=True)
    assert current.state == "running" and current.attempts == 1


@pytest.mark.parametrize("ingress", ["compile", "amend"])
async def test_resubmission_rechecks_continuation_after_flow_lock_wait(session, appendable, ingress):  # noqa: F811
    ctx = appendable
    # Keep a cached plan without a continuation marker in the request session.
    legacy = dict(ctx.original)
    legacy.pop("execution_continuation")
    legacy["execution_policy"] = None
    ctx.plan.plan_document, ctx.plan.plan_hash = legacy, digest(legacy)
    await session.commit()
    async with session.info["factory"]() as accepter:
        await accepter.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).with_for_update())
        current = await accepter.get(OrchestrationAcceptedPlan, ctx.plan.id)
        current.plan_document, current.plan_hash = ctx.original, digest(ctx.original)
        await accepter.flush()
        operation = (
            compile_proposal(session, valid_proposal(), ctx.actor)
            if ingress == "compile"
            else amend_plan(
                session,
                ctx.flow.id,
                valid_proposal(),
                AmendmentContext(org_id=ctx.actor.org_id, actor_id=ctx.actor.actor_id, actor_role=ctx.actor.actor_role),
            )
        )
        task = asyncio.create_task(operation)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            await accepter.commit()
            with pytest.raises(ProposalRejectedError, match="bounded append preview/accept"):
                await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    plans = list(await session.scalars(select(OrchestrationAcceptedPlan)))
    assert len(plans) == 1 and plans[0].plan_document == ctx.original
