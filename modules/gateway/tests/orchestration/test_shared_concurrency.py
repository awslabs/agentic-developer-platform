"""Concurrency supplements preserve live work, financial authority and admission gates."""

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
from src.orchestration.execution_policy import policy_hash, stamp_policy
from src.orchestration.models import OrchestrationDecision
from src.orchestration.policy_admission import load_in_force_policy
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.shared_amendment import SharedAppendError
from src.orchestration.shared_budget import BudgetIncreaseRequest, accept_budget_increase, financial_limits, preview_budget_increase
from src.orchestration.shared_concurrency import (
    ConcurrencyIncreaseError,
    ConcurrencyIncreaseRequest,
    accept_concurrency_increase,
    preview_concurrency_increase,
)
from src.orchestration.shared_retry import verified_limits_increased
from src.orchestration.state import ActorKind
from tests.orchestration.test_policy_admission import APPROVER
from tests.orchestration.test_shared_policy import dispatch, engine, session, shared  # noqa: F401


@pytest.fixture
async def concurrency(shared):  # noqa: F811
    shared.plan.plan_hash = digest(shared.plan.plan_document)
    shared.node.state, shared.node.attempts = "running", 2
    await shared.session.flush()
    return SimpleNamespace(
        s=shared,
        actor=ApprovalContext(org_id=shared.flow.org_id, actor_id=APPROVER, actor_role="platform_admin"),
        request=ConcurrencyIncreaseRequest(
            expected_plan_version=shared.plan.version,
            expected_plan_hash=shared.plan.plan_hash,
            max_concurrent_actions=4,
            reason="Owner authorizes four concurrent workers, preserving all used attempts and other limits.",
        ),
    )


async def preview(b):
    return await preview_concurrency_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=b.request)


async def accept(b):
    result = await preview(b)
    request = b.request.model_copy(update={"expected_snapshot": result["snapshot"]})
    return await accept_concurrency_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=request), request


async def effective(b):
    return await load_in_force_policy(b.s.session, org_id=b.s.flow.org_id, flow_id=b.s.flow.id)


async def test_increase_preserves_live_plan_claims_consumed_attempts_and_meter(concurrency):
    b = concurrency
    before = (await effective(b)).policy
    plan = copy.deepcopy(b.s.plan.plan_document)
    meter = await b.s.client.hgetall(b.s.target.key())
    ttl = await b.s.client.pttl(b.s.target.key())
    identity = (b.s.node.state, b.s.node.attempts, b.s.claim.active_run_id, b.s.claim.generation, b.s.plan.version)
    receipt, request = await accept(b)
    after = (await effective(b)).policy
    assert after.limits.max_concurrent_actions == 4
    assert verified_limits_increased(before.model_dump(mode="json"), after)
    assert after.policy_hash == policy_hash(after)
    assert financial_limits(after) == financial_limits(before)
    assert after._shared_concurrency_decision_id == receipt["decision_id"]
    assert b.s.plan.plan_document == plan and b.s.plan.plan_hash == digest(plan)
    assert identity == (b.s.node.state, b.s.node.attempts, b.s.claim.active_run_id, b.s.claim.generation, b.s.plan.version)
    assert await b.s.client.hgetall(b.s.target.key()) == meter
    assert await b.s.client.pttl(b.s.target.key()) <= ttl
    row = await b.s.session.get(OrchestrationDecision, receipt["decision_id"])
    assert row.actor_id == b.actor.actor_id and row.actor_kind == "human"
    replay = await accept_concurrency_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=request)
    assert not replay["created"] and replay["decision_id"] == receipt["decision_id"]


@pytest.mark.parametrize("budget_first", [True, False])
async def test_financial_and_concurrency_supplements_compose_in_either_order(concurrency, budget_first):
    b = concurrency
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
    assert policy.limits.max_concurrent_actions == 4 and policy.limits.max_spend_usd == 1000
    assert policy._shared_run_spend_usd == 100 and policy._shared_chain_spend_usd == 1000
    assert policy._shared_budget_decision_id == budget["decision_id"] and policy._shared_concurrency_decision_id
    assert verified_limits_increased(original.model_dump(mode="json"), policy)


async def test_concurrency_increase_does_not_clear_unknown_usage_or_allow_dispatch(concurrency):
    b = concurrency
    await b.s.client.delete(b.s.target.key())
    await accept(b)
    assert (await effective(b)).policy.limits.max_concurrent_actions == 4
    b.s.node.state = "ready"
    decision = await dispatch(b.s)
    assert not decision.permitted and decision.reason.value == "budget_unavailable"
    assert not await b.s.client.exists(b.s.target.key())


@pytest.mark.parametrize("case", ["version", "hash", "decrease", "unchanged", "expired", "service", "other_org", "tampered_document"])
async def test_invalid_increase_writes_no_concurrency_decision(concurrency, case):
    b = concurrency
    if case == "version":
        b.request = b.request.model_copy(update={"expected_plan_version": 999})
    elif case == "hash":
        b.request = b.request.model_copy(update={"expected_plan_hash": "a" * 64})
    elif case in {"decrease", "unchanged"}:
        b.request = b.request.model_copy(update={"max_concurrent_actions": 1 if case == "decrease" else b.s.policy.limits.max_concurrent_actions})
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
    with pytest.raises((ConcurrencyIncreaseError, SharedAppendError, CycleBlockedError)):
        await preview(b)
    assert not list(await b.s.session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "concurrency_limit_increased")))


@pytest.mark.parametrize("change", ["actor", "role", "hash", "policy_hash", "principal", "flow", "contract", "reduce", "string", "bool", "future"])
async def test_unverifiable_receipt_fails_closed(concurrency, change):
    b = concurrency
    receipt, _ = await accept(b)
    row = await b.s.session.get(OrchestrationDecision, receipt["decision_id"])
    data = json.loads(row.reason)
    field, value = {
        "hash": ("plan_hash", "a" * 64),
        "policy_hash": ("original_policy_hash", "a" * 64),
        "principal": ("principal_id", "someone-else"),
        "flow": ("flow_id", "another-flow"),
        "contract": ("contract", "unknown"),
        "reduce": ("max_concurrent_actions", 1),
        "string": ("max_concurrent_actions", "20"),
        "bool": ("max_concurrent_actions", True),
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
async def test_malformed_receipt_fails_closed_without_crashing(concurrency, raw):
    b = concurrency
    b.s.session.add(
        OrchestrationDecision(
            org_id=b.s.flow.org_id,
            flow_id=b.s.flow.id,
            kind="concurrency_limit_increased",
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
def test_concurrency_request_requires_bounded_integer(value):
    with pytest.raises(ValidationError):
        ConcurrencyIncreaseRequest(expected_plan_version=1, expected_plan_hash="a" * 64, max_concurrent_actions=value, reason="Owner approval")


async def test_stale_preview_cannot_overwrite_later_concurrency_approval(concurrency):
    b = concurrency
    old = await preview(b)
    stale = b.request.model_copy(update={"expected_snapshot": old["snapshot"]})
    b.request = b.request.model_copy(update={"max_concurrent_actions": 3})
    await accept(b)
    with pytest.raises(ConcurrencyIncreaseError, match="concurrency_preview_changed"):
        await accept_concurrency_increase(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=stale)
    assert (await effective(b)).policy.limits.max_concurrent_actions == 3


async def test_new_graph_acceptance_does_not_inherit_concurrency_supplement(concurrency):
    b = concurrency
    before = (await effective(b)).policy
    await accept(b)
    b.s.plan.version += 1
    await b.s.session.flush()
    after = (await effective(b)).policy
    assert after.limits.max_concurrent_actions == before.limits.max_concurrent_actions
    assert after._shared_concurrency_decision_id is None


@pytest.mark.parametrize("change", ["no_receipt", "spend", "expiry", "role", "decrease"])
async def test_upload_compatibility_requires_matching_receipts_and_unchanged_authority(concurrency, change):
    before = (await effective(concurrency)).policy.model_dump(mode="json")
    await accept(concurrency)
    policy = (await effective(concurrency)).policy
    if change == "no_receipt":
        policy._shared_concurrency_decision_id = None
    elif change == "spend":
        policy.limits.max_spend_usd *= 2
    elif change == "expiry":
        policy.expires_at += timedelta(hours=1)
    elif change == "role":
        policy.user_credentials.aws_role_arns = ["arn:aws:iam::123456789012:role/other"]
    else:
        policy.limits.max_concurrent_actions = 1
    assert not verified_limits_increased(before, policy)


@pytest.mark.parametrize("role", ["org_admin", "member", "dept_admin"])
async def test_tenant_roles_cannot_increase_concurrency_limit(concurrency, role):
    concurrency.actor = replace(concurrency.actor, actor_role=role)
    with pytest.raises(ConcurrencyIncreaseError, match="human_platform_admin_required"):
        await preview(concurrency)


async def test_route_requires_platform_admin_before_write():
    from src.admin.exceptions import AccessDeniedError
    from src.orchestration.shared_concurrency_routes import accept_concurrency

    with pytest.raises(AccessDeniedError):
        await accept_concurrency(flow_id="flow", body=None, current_user=SimpleNamespace(is_admin=False), db=None)


async def test_route_requires_plan_permission(monkeypatch):
    from fastapi import HTTPException

    from src.admin.config import Permission
    from src.orchestration import shared_concurrency_routes

    check = AsyncMock(side_effect=HTTPException(403, "plan permission required"))
    monkeypatch.setattr(
        shared_concurrency_routes, "AccessControl", lambda db: SimpleNamespace(check_permission=check, require_platform_admin=lambda user: None)
    )
    acceptor = AsyncMock()
    monkeypatch.setattr(shared_concurrency_routes, "accept_concurrency_increase", acceptor)
    user = SimpleNamespace(org_id="org")
    with pytest.raises(HTTPException):
        await shared_concurrency_routes.accept_concurrency(flow_id="flow", body=None, current_user=user, db=None)
    check.assert_awaited_once_with(user, Permission.PLAN_APPROVE, target_org_id="org")
    acceptor.assert_not_awaited()


@pytest.mark.parametrize("attempts,allowed", [(2, True), (3, False)])
async def test_story_dispatch_retains_original_attempt_limit(concurrency, attempts, allowed):
    await accept(concurrency)
    concurrency.s.node.state, concurrency.s.node.attempts = "ready", attempts
    result = await dispatch(concurrency.s)
    assert result.permitted is allowed
    if not allowed:
        assert result.reason.value == "attempt_limit_exceeded"
    assert concurrency.s.node.attempts == attempts


@pytest.mark.parametrize("active,allowed", [(3, True), (4, False)])
async def test_story_dispatch_uses_four_worker_limit(concurrency, active, allowed):
    from tests.orchestration.test_policy_admission import _make_node
    from tests.orchestration.test_shared_policy import report

    b = concurrency
    await accept(b)
    b.s.node.state = "ready"
    for index in range(active):
        node = await _make_node(b.s.session, b.s.flow, node_ref=f"other-{index}", state="running", attempts=1, issue_ref=str(900 + index))
        await report(b.s, node, f"other-worker-{index}")
    result = await dispatch(b.s)
    assert result.permitted is allowed
    if not allowed:
        assert result.reason.value == "concurrency_limit_exceeded"


async def test_another_platform_admin_cannot_change_owners_concurrency(concurrency):
    concurrency.actor = replace(concurrency.actor, actor_id="another-admin")
    with pytest.raises(ConcurrencyIncreaseError, match="original_principal_required"):
        await preview(concurrency)


@pytest.mark.parametrize("concurrency_first", [True, False])
async def test_window_and_concurrency_supplements_compose(concurrency, concurrency_first):
    from src.orchestration.shared_window import WindowRenewalRequest, accept_window_renewal, preview_window_renewal

    b = concurrency
    draft = b.s.policy.model_copy(deep=True)
    draft.policy_id = draft.policy_hash = draft.principal_id = None
    draft.expires_at = datetime.now(UTC) + timedelta(hours=1)
    b.s.policy = stamp_policy(draft, principal_id=APPROVER, org_id=b.s.flow.org_id)
    b.s.plan.plan_document = {**b.s.plan.plan_document, "execution_policy": b.s.policy.model_dump(mode="json")}
    b.s.plan.plan_hash = digest(b.s.plan.plan_document)
    b.request = b.request.model_copy(update={"expected_plan_hash": b.s.plan.plan_hash})
    await b.s.session.flush()
    before = (await effective(b)).policy
    if concurrency_first:
        await accept(b)
    request = WindowRenewalRequest(
        expected_plan_version=b.s.plan.version,
        expected_plan_hash=b.s.plan.plan_hash,
        expires_at=before.expires_at + timedelta(hours=1),
        reason="Owner requests continuation of the same delivery.",
    )
    observed = await preview_window_renewal(b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=request)
    await accept_window_renewal(
        b.s.session, flow_id=b.s.flow.id, actor=b.actor, request=request.model_copy(update={"expected_snapshot": observed["snapshot"]})
    )
    if not concurrency_first:
        await accept(b)
    after = (await effective(b)).policy
    assert after.expires_at == request.expires_at and after.limits.max_concurrent_actions == 4
    assert after._shared_window_decision_id and after._shared_concurrency_decision_id
    assert verified_limits_increased(before.model_dump(mode="json"), after)
