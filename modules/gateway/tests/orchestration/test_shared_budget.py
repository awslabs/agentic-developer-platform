"""Live financial increases retain work, authority and recorded spend."""

import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.compile import ApprovalContext
from src.orchestration.continuation import digest
from src.orchestration.execution_policy import policy_hash
from src.orchestration.flow_budget import admission_cost_usd
from src.orchestration.models import OrchestrationDecision
from src.orchestration.policy_admission import load_in_force_policy
from src.orchestration.shared_amendment import SharedAppendError
from src.orchestration.shared_budget import (
    BudgetIncreaseError,
    BudgetIncreaseRequest,
    FinancialLimits,
    accept_budget_increase,
    only_spend_increased,
    preview_budget_increase,
)
from src.orchestration.state import ActorKind
from tests.orchestration.test_policy_admission import APPROVER
from tests.orchestration.test_shared_policy import engine, session, shared  # noqa: F401


@pytest.fixture(params=["shared", "protected"])
async def budget(shared, request):  # noqa: F811
    if request.param == "protected":
        document = copy.deepcopy(shared.plan.plan_document)
        document.pop("execution_continuation")
        shared.plan.plan_document = document
    shared.plan.plan_hash = digest(shared.plan.plan_document)
    shared.node.state, shared.node.attempts = "running", 1
    await shared.session.flush()
    return SimpleNamespace(
        s=shared,
        actor=ApprovalContext(org_id=shared.flow.org_id, actor_id=APPROVER, actor_role="platform_admin"),
        request=BudgetIncreaseRequest(
            expected_plan_version=shared.plan.version,
            expected_plan_hash=shared.plan.plan_hash,
            limits=FinancialLimits(max_spend_usd=1000, max_run_spend_usd=100, max_chain_spend_usd=1000),
            reason="Owner approves USD 1000 for this flow; preserve other limits.",
        ),
    )


async def preview(b):
    return await preview_budget_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=b.request)


async def accept(b):
    result = await preview(b)
    request = b.request.model_copy(update={"expected_snapshot": result["snapshot"]})
    return await accept_budget_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=request), request


async def effective(b):
    return await load_in_force_policy(b.s.session, org_id=b.s.flow.org_id, flow_id=b.s.flow.id)


async def test_live_increase_preserves_plan_assignments_and_all_meter_fields(budget):
    b = budget
    before = await effective(b)
    plan = copy.deepcopy(b.s.plan.plan_document)
    meter = await b.s.client.hgetall(b.s.target.key())
    ttl = await b.s.client.pttl(b.s.target.key())
    identity = (b.s.node.state, b.s.node.attempts, b.s.claim.active_run_id, b.s.claim.generation, b.s.plan.version)
    receipt, request = await accept(b)
    after = await effective(b)
    assert after.policy.limits.max_spend_usd == 1000 and admission_cost_usd(after.policy) == 100
    assert after.policy._shared_chain_spend_usd == 1000
    assert only_spend_increased(before.policy.model_dump(mode="json"), after.policy.model_dump(mode="json"))
    assert after.policy.policy_hash == policy_hash(after.policy)
    assert after.policy.principal_id == before.policy.principal_id
    assert after.policy._shared_budget_decision_id == receipt["decision_id"]
    assert b.s.plan.plan_document == plan and b.s.plan.plan_hash == digest(plan)
    assert identity == (b.s.node.state, b.s.node.attempts, b.s.claim.active_run_id, b.s.claim.generation, b.s.plan.version)
    assert await b.s.client.hgetall(b.s.target.key()) == meter
    assert await b.s.client.pttl(b.s.target.key()) <= ttl
    row = await b.s.session.get(OrchestrationDecision, receipt["decision_id"])
    assert row.actor_id == b.actor.actor_id and row.actor_kind == "human"
    replay = await accept_budget_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=request)
    assert not replay["created"] and replay["decision_id"] == receipt["decision_id"]


@pytest.mark.parametrize("case", ["version", "hash", "decrease", "unknown_meter", "expired", "service", "other_org", "tampered_document"])
async def test_invalid_increase_writes_no_financial_decision(budget, case):
    b = budget
    if case == "version":
        b.request = b.request.model_copy(update={"expected_plan_version": 999})
    elif case == "hash":
        b.request = b.request.model_copy(update={"expected_plan_hash": "a" * 64})
    elif case == "decrease":
        b.request = b.request.model_copy(update={"limits": FinancialLimits(max_spend_usd=40, max_run_spend_usd=5, max_chain_spend_usd=40)})
    elif case == "unknown_meter":
        await b.s.client.delete(b.s.target.key())
    elif case == "expired":
        doc = copy.deepcopy(b.s.plan.plan_document)
        doc["execution_policy"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        b.s.plan.plan_document, b.s.plan.plan_hash = doc, digest(doc)
        b.request = b.request.model_copy(update={"expected_plan_hash": b.s.plan.plan_hash})
        await b.s.session.flush()
    elif case == "service":
        b.actor = ApprovalContext(org_id=b.actor.org_id, actor_id=b.actor.actor_id, actor_role="org_admin", actor_kind=ActorKind.SERVICE)
    elif case == "other_org":
        b.actor = ApprovalContext(org_id="other-org", actor_id=b.actor.actor_id, actor_role="org_admin")
    else:
        b.s.plan.plan_hash = "a" * 64
        b.request = b.request.model_copy(update={"expected_plan_hash": b.s.plan.plan_hash})
    with pytest.raises((BudgetIncreaseError, SharedAppendError)):
        await preview(b)
    assert not list(await b.s.session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "budget_increased")))


@pytest.mark.parametrize("change", ["actor", "hash", "principal", "scope", "contract"])
async def test_unverifiable_receipt_fails_closed(budget, change):
    b = budget
    receipt, _ = await accept(b)
    row = await b.s.session.get(OrchestrationDecision, receipt["decision_id"])
    data = json.loads(row.reason)
    actor_kind = "human"
    if change == "actor":
        actor_kind = "service"
    elif change == "hash":
        data["plan_hash"] = "a" * 64
    elif change == "principal":
        data["principal_id"] = "someone-else"
    elif change == "scope":
        doc = copy.deepcopy(b.s.plan.plan_document)
        doc["execution_policy"]["repository_ids"] = ["other/repo"]
        b.s.plan.plan_document = doc
    else:
        data["contract"] = "unknown"
    b.s.session.add(
        OrchestrationDecision(
            org_id=row.org_id,
            flow_id=row.flow_id,
            kind=row.kind,
            actor_id=row.actor_id,
            actor_role=row.actor_role,
            actor_kind=actor_kind,
            created_at=datetime.now(UTC) + timedelta(seconds=1),
            reason=json.dumps(data),
        )
    )
    await b.s.session.flush()
    result = await effective(b)
    assert result.policy is None and result.refusal is not None


def test_only_financial_change_is_compatible_during_upload():
    before = {"limits": {"max_spend_usd": "50", "max_attempts_per_node": 2}, "principal_id": "owner", "expires_at": "original"}
    after = copy.deepcopy(before)
    after["limits"]["max_spend_usd"] = "1000"
    assert only_spend_increased(before, after)
    after["limits"]["max_attempts_per_node"] = 3
    assert not only_spend_increased(before, after)
    after["limits"]["max_attempts_per_node"] = 2
    after["expires_at"] = "renewed"
    assert not only_spend_increased(before, after)


async def test_both_plan_and_budget_permission_are_required(monkeypatch):
    from fastapi import HTTPException

    from src.admin.config import Permission
    from src.orchestration import shared_budget_routes

    checked = []

    async def check(user, permission, **kwargs):
        checked.append(permission)
        if permission == Permission.BUDGET_UPDATE:
            raise HTTPException(403, "budget permission required")

    monkeypatch.setattr(
        shared_budget_routes, "AccessControl", lambda db: SimpleNamespace(check_permission=check, require_platform_admin=lambda user: None)
    )
    acceptor = AsyncMock()
    monkeypatch.setattr(shared_budget_routes, "accept_budget_increase", acceptor)
    with pytest.raises(HTTPException):
        await shared_budget_routes.accept_budget(flow_id="flow", body=None, current_user=SimpleNamespace(org_id="org"), db=None)
    assert checked == [Permission.PLAN_APPROVE, Permission.BUDGET_UPDATE]
    acceptor.assert_not_awaited()


@pytest.mark.parametrize("role", ["org_admin", "member", "dept_admin"])
async def test_tenant_roles_cannot_author_platform_ceiling_increases(budget, role):
    from dataclasses import replace

    budget.actor = replace(budget.actor, actor_role=role)
    with pytest.raises(BudgetIncreaseError, match="human_platform_admin_required"):
        await preview(budget)


async def test_route_requires_platform_admin_before_any_budget_write():
    from src.admin.exceptions import AccessDeniedError
    from src.orchestration.shared_budget_routes import accept_budget

    with pytest.raises(AccessDeniedError):
        await accept_budget(flow_id="flow", body=None, current_user=SimpleNamespace(is_admin=False), db=None)


async def test_stale_preview_cannot_overwrite_later_financial_approval(budget):
    b = budget
    old = await preview(b)
    stale = b.request.model_copy(update={"expected_snapshot": old["snapshot"]})
    b.request = b.request.model_copy(update={"limits": FinancialLimits(max_spend_usd=500, max_run_spend_usd=50, max_chain_spend_usd=500)})
    await accept(b)
    with pytest.raises(BudgetIncreaseError, match="budget_preview_changed"):
        await accept_budget_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=stale)
    assert (await effective(b)).policy.limits.max_spend_usd == 500


async def test_new_graph_acceptance_does_not_inherit_old_budget_exception(budget):
    b = budget
    before = await effective(b)
    await accept(b)
    b.s.plan.version += 1
    await b.s.session.flush()
    after = await effective(b)
    assert after.policy.limits.max_spend_usd == before.policy.limits.max_spend_usd
    assert after.policy._shared_budget_decision_id is None


@pytest.mark.parametrize("raw", ["null", "[]", "{", '{"plan_version": 1}'])
async def test_malformed_financial_receipt_fails_closed_without_crashing(budget, raw):
    b = budget
    b.s.session.add(
        OrchestrationDecision(
            org_id=b.s.flow.org_id,
            flow_id=b.s.flow.id,
            kind="budget_increased",
            actor_kind="human",
            actor_id=b.actor.actor_id,
            actor_role="platform_admin",
            reason=raw,
        )
    )
    await b.s.session.flush()
    result = await effective(b)
    assert result.policy is None and result.refusal is not None


@pytest.mark.parametrize("change", ["missing", "service", "other_flow", "inactive"])
async def test_protected_budget_requires_active_human_accepted_plan(budget, change):
    b = budget
    document = copy.deepcopy(b.s.plan.plan_document)
    document.pop("execution_continuation", None)
    b.s.plan.plan_document, b.s.plan.plan_hash = document, digest(document)
    b.request = b.request.model_copy(update={"expected_plan_hash": b.s.plan.plan_hash})
    if change == "missing":
        b.s.plan.accepted_by_decision_id = None
    elif change in {"service", "other_flow"}:
        acceptance = OrchestrationDecision(
            org_id=b.actor.org_id,
            flow_id="another-flow" if change == "other_flow" else b.s.flow.id,
            kind="plan_accepted",
            actor_kind="service" if change == "service" else "human",
            actor_id=b.actor.actor_id,
            actor_role=b.actor.actor_role,
            reason="Invalid acceptance fixture",
        )
        b.s.session.add(acceptance)
        await b.s.session.flush()
        b.s.plan.accepted_by_decision_id = acceptance.id
    else:
        b.s.flow.state = "passed"
    await b.s.session.flush()
    with pytest.raises(BudgetIncreaseError, match="plan_acceptance_unverifiable|flow_not_active"):
        await preview(b)
