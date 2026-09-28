"""Retry supplements preserve live work, financial authority and admission gates."""

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from src.orchestration.compile import ApprovalContext
from src.orchestration.continuation import digest
from src.orchestration.execution_policy import policy_hash
from src.orchestration.models import OrchestrationDecision
from src.orchestration.policy_admission import load_in_force_policy
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.shared_amendment import SharedAppendError
from src.orchestration.shared_budget import BudgetIncreaseRequest, accept_budget_increase, financial_limits, preview_budget_increase
from src.orchestration.shared_retry import (
    RetryIncreaseError,
    RetryIncreaseRequest,
    accept_retry_increase,
    preview_retry_increase,
    verified_limits_increased,
)
from src.orchestration.state import ActorKind
from tests.orchestration.test_policy_admission import APPROVER
from tests.orchestration.test_shared_policy import dispatch, engine, session, shared  # noqa: F401


@pytest.fixture
async def retry(shared):  # noqa: F811
    shared.plan.plan_hash = digest(shared.plan.plan_document)
    shared.node.state, shared.node.attempts = "running", 2
    await shared.session.flush()
    return SimpleNamespace(
        s=shared,
        actor=ApprovalContext(org_id=shared.flow.org_id, actor_id=APPROVER, actor_role="platform_admin"),
        request=RetryIncreaseRequest(
            expected_plan_version=shared.plan.version,
            expected_plan_hash=shared.plan.plan_hash,
            max_attempts_per_node=20,
            reason="Owner authorizes attempt ceiling 20, preserving all used attempts and other limits.",
        ),
    )


async def preview(b):
    return await preview_retry_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=b.request)


async def accept(b):
    result = await preview(b)
    request = b.request.model_copy(update={"expected_snapshot": result["snapshot"]})
    return await accept_retry_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=request), request


async def effective(b):
    return await load_in_force_policy(b.s.session, org_id=b.s.flow.org_id, flow_id=b.s.flow.id)


async def test_increase_preserves_live_plan_claims_consumed_attempts_and_meter(retry):
    b = retry
    before = (await effective(b)).policy
    plan = copy.deepcopy(b.s.plan.plan_document)
    meter = await b.s.client.hgetall(b.s.target.key())
    ttl = await b.s.client.pttl(b.s.target.key())
    identity = (b.s.node.state, b.s.node.attempts, b.s.claim.active_run_id, b.s.claim.generation, b.s.plan.version)
    receipt, request = await accept(b)
    after = (await effective(b)).policy
    assert after.limits.max_attempts_per_node == 20
    assert verified_limits_increased(before.model_dump(mode="json"), after)
    assert after.policy_hash == policy_hash(after)
    assert financial_limits(after) == financial_limits(before)
    assert after._shared_retry_decision_id == receipt["decision_id"]
    assert b.s.plan.plan_document == plan and b.s.plan.plan_hash == digest(plan)
    assert identity == (b.s.node.state, b.s.node.attempts, b.s.claim.active_run_id, b.s.claim.generation, b.s.plan.version)
    assert await b.s.client.hgetall(b.s.target.key()) == meter
    assert await b.s.client.pttl(b.s.target.key()) <= ttl
    row = await b.s.session.get(OrchestrationDecision, receipt["decision_id"])
    assert row.actor_id == b.actor.actor_id and row.actor_kind == "human"
    replay = await accept_retry_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=request)
    assert not replay["created"] and replay["decision_id"] == receipt["decision_id"]


@pytest.mark.parametrize("budget_first", [True, False])
async def test_financial_and_retry_supplements_compose_in_either_order(retry, budget_first):
    b = retry
    original = (await effective(b)).policy
    request = BudgetIncreaseRequest(
        expected_plan_version=b.s.plan.version,
        expected_plan_hash=b.s.plan.plan_hash,
        limits={"max_spend_usd": 1000, "max_run_spend_usd": 100, "max_chain_spend_usd": 1000},
        reason=b.request.reason,
    )
    if not budget_first:
        await accept(b)
    result = await preview_budget_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=request)
    budget = await accept_budget_increase(
        b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=request.model_copy(update={"expected_snapshot": result["snapshot"]})
    )
    if budget_first:
        await accept(b)
    policy = (await effective(b)).policy
    assert policy.limits.max_attempts_per_node == 20 and policy.limits.max_spend_usd == 1000
    assert policy._shared_run_spend_usd == 100 and policy._shared_chain_spend_usd == 1000
    assert policy._shared_budget_decision_id == budget["decision_id"] and policy._shared_retry_decision_id
    assert verified_limits_increased(original.model_dump(mode="json"), policy)


async def test_retry_increase_does_not_clear_unknown_usage_or_allow_dispatch(retry):
    b = retry
    await b.s.client.delete(b.s.target.key())
    await accept(b)
    assert (await effective(b)).policy.limits.max_attempts_per_node == 20
    b.s.node.state = "ready"
    decision = await dispatch(b.s)
    assert not decision.permitted and decision.reason.value == "budget_unavailable"
    assert not await b.s.client.exists(b.s.target.key())


@pytest.mark.parametrize("case", ["version", "hash", "decrease", "unchanged", "expired", "service", "other_org", "tampered_document"])
async def test_invalid_increase_writes_no_retry_decision(retry, case):
    b = retry
    if case == "version":
        b.request = b.request.model_copy(update={"expected_plan_version": 999})
    elif case == "hash":
        b.request = b.request.model_copy(update={"expected_plan_hash": "a" * 64})
    elif case in {"decrease", "unchanged"}:
        b.request = b.request.model_copy(update={"max_attempts_per_node": 1 if case == "decrease" else b.s.policy.limits.max_attempts_per_node})
    elif case == "expired":
        doc = copy.deepcopy(b.s.plan.plan_document)
        doc["execution_policy"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        b.s.plan.plan_document, b.s.plan.plan_hash = doc, digest(doc)
        b.request = b.request.model_copy(update={"expected_plan_hash": b.s.plan.plan_hash})
        await b.s.session.flush()
    elif case == "service":
        b.actor = replace(b.actor, actor_kind=ActorKind.SERVICE)
    elif case == "other_org":
        b.actor = replace(b.actor, org_id="other-org")
    else:
        b.s.plan.plan_hash = "a" * 64
        b.request = b.request.model_copy(update={"expected_plan_hash": b.s.plan.plan_hash})
    with pytest.raises((RetryIncreaseError, SharedAppendError, CycleBlockedError)):
        await preview(b)
    assert not list(await b.s.session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "retry_limit_increased")))


@pytest.mark.parametrize("change", ["actor", "role", "hash", "policy_hash", "principal", "flow", "contract", "reduce", "string", "bool", "future"])
async def test_unverifiable_receipt_fails_closed(retry, change):
    b = retry
    receipt, _ = await accept(b)
    row = await b.s.session.get(OrchestrationDecision, receipt["decision_id"])
    data = json.loads(row.reason)
    field, value = {
        "hash": ("plan_hash", "a" * 64),
        "policy_hash": ("original_policy_hash", "a" * 64),
        "principal": ("principal_id", "someone-else"),
        "flow": ("flow_id", "another-flow"),
        "contract": ("contract", "unknown"),
        "reduce": ("max_attempts_per_node", 1),
        "string": ("max_attempts_per_node", "20"),
        "bool": ("max_attempts_per_node", True),
        "future": ("plan_version", 999),
    }.get(change, ("unused", None))
    data[field] = value
    b.s.session.add(
        OrchestrationDecision(
            org_id=row.org_id,
            flow_id=row.flow_id,
            kind=row.kind,
            actor_id=row.actor_id,
            actor_role="org_admin" if change == "role" else row.actor_role,
            actor_kind="service" if change == "actor" else "human",
            created_at=datetime.now(UTC) + timedelta(seconds=1),
            reason=json.dumps(data),
        )
    )
    await b.s.session.flush()
    result = await effective(b)
    assert result.policy is None and result.refusal is not None


@pytest.mark.parametrize("raw", ["null", "[]", "{", '{"plan_version": 1}'])
async def test_malformed_receipt_fails_closed_without_crashing(retry, raw):
    b = retry
    b.s.session.add(
        OrchestrationDecision(
            org_id=b.s.flow.org_id,
            flow_id=b.s.flow.id,
            kind="retry_limit_increased",
            actor_kind="human",
            actor_id=b.actor.actor_id,
            actor_role="platform_admin",
            reason=raw,
        )
    )
    await b.s.session.flush()
    result = await effective(b)
    assert result.policy is None and result.refusal is not None


@pytest.mark.parametrize("value", [True, "20", 20.0, 0, 101])
def test_retry_request_requires_bounded_integer(value):
    with pytest.raises(ValidationError):
        RetryIncreaseRequest(expected_plan_version=1, expected_plan_hash="a" * 64, max_attempts_per_node=value, reason="Owner approval")


async def test_stale_preview_cannot_overwrite_later_retry_approval(retry):
    b = retry
    old = await preview(b)
    stale = b.request.model_copy(update={"expected_snapshot": old["snapshot"]})
    b.request = b.request.model_copy(update={"max_attempts_per_node": 10})
    await accept(b)
    with pytest.raises(RetryIncreaseError, match="retry_preview_changed"):
        await accept_retry_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=stale)
    assert (await effective(b)).policy.limits.max_attempts_per_node == 10


async def test_new_graph_acceptance_does_not_inherit_retry_supplement(retry):
    b = retry
    before = (await effective(b)).policy
    await accept(b)
    b.s.plan.version += 1
    await b.s.session.flush()
    after = (await effective(b)).policy
    assert after.limits.max_attempts_per_node == before.limits.max_attempts_per_node
    assert after._shared_retry_decision_id is None


@pytest.mark.parametrize("change", ["no_receipt", "spend", "expiry", "role", "decrease"])
async def test_upload_compatibility_requires_matching_receipts_and_unchanged_authority(retry, change):
    before = (await effective(retry)).policy.model_dump(mode="json")
    await accept(retry)
    policy = (await effective(retry)).policy
    if change == "no_receipt":
        policy._shared_retry_decision_id = None
    elif change == "spend":
        policy.limits.max_spend_usd *= 2
    elif change == "expiry":
        policy.expires_at += timedelta(hours=1)
    elif change == "role":
        policy.user_credentials.aws_role_arns = ["arn:aws:iam::123456789012:role/other"]
    else:
        policy.limits.max_attempts_per_node = 1
    assert not verified_limits_increased(before, policy)


@pytest.mark.parametrize("role", ["org_admin", "member", "dept_admin"])
async def test_tenant_roles_cannot_increase_retry_limit(retry, role):
    retry.actor = replace(retry.actor, actor_role=role)
    with pytest.raises(RetryIncreaseError, match="human_platform_admin_required"):
        await preview(retry)


async def test_route_requires_platform_admin_before_write():
    from src.admin.exceptions import AccessDeniedError
    from src.orchestration.shared_retry_routes import accept_retry

    with pytest.raises(AccessDeniedError):
        await accept_retry(flow_id="flow", body=None, current_user=SimpleNamespace(is_admin=False), db=None)


async def test_route_requires_plan_permission(monkeypatch):
    from fastapi import HTTPException

    from src.admin.config import Permission
    from src.orchestration import shared_retry_routes

    check = AsyncMock(side_effect=HTTPException(403, "plan permission required"))
    monkeypatch.setattr(
        shared_retry_routes, "AccessControl", lambda db: SimpleNamespace(check_permission=check, require_platform_admin=lambda user: None)
    )
    acceptor = AsyncMock()
    monkeypatch.setattr(shared_retry_routes, "accept_retry_increase", acceptor)
    user = SimpleNamespace(org_id="org")
    with pytest.raises(HTTPException):
        await shared_retry_routes.accept_retry(flow_id="flow", body=None, current_user=user, db=None)
    check.assert_awaited_once_with(user, Permission.PLAN_APPROVE, target_org_id="org")
    acceptor.assert_not_awaited()


@pytest.mark.parametrize("attempts,allowed", [(19, True), (20, False)])
async def test_story_dispatch_uses_twenty_total_attempts(retry, attempts, allowed):
    await accept(retry)
    retry.s.node.state, retry.s.node.attempts = "ready", attempts
    result = await dispatch(retry.s)
    assert result.permitted is allowed
    if not allowed:
        assert result.reason.value == "attempt_limit_exceeded"
    assert retry.s.node.attempts == attempts
