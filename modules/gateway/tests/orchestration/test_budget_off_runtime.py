# ruff: noqa: F811
"""Budget off lifts financial stalls while preserving execution authority."""

from decimal import Decimal

import pytest
from sqlalchemy import delete

from src.budget.enforcement_settings import BudgetEnforcementSetting, flow_key
from src.orchestration.execution_policy import DenyReason
from src.orchestration.policy_admission import load_in_force_policy
from src.shared.models.onboarding import TenantMembership
from tests.orchestration.test_shared_policy import dispatch, engine, session, shared  # noqa: F401


async def off(s, *, global_scope=False):
    row = BudgetEnforcementSetting(
        scope_key="global" if global_scope else flow_key(s.flow.org_id, s.flow.id), enabled=False, revision=1, updated_by="operator"
    )
    s.session.add(row)
    await s.session.flush()
    return row


@pytest.mark.parametrize("scope", ["global", "flow", "environment"])
@pytest.mark.parametrize("accounting", ["unknown", "lost", "exhausted"])
async def test_budget_off_admits_without_rewriting_usage_or_plan(shared, monkeypatch, scope, accounting):
    s = shared
    if accounting == "unknown":
        await s.store.reserve("unsettled", Decimal(1), [s.target])
        await s.store.mark_unknown("unsettled", s.target)
    elif accounting == "lost":
        await s.client.flushdb()
    else:
        await s.store.reserve("spent", Decimal(30), [s.target])
        await s.store.reconcile("spent", Decimal(30), [s.target])
    before = await s.client.hgetall(s.target.key())
    plan = s.plan.plan_document.copy()
    assert not (await dispatch(s)).permitted
    if scope == "environment":
        monkeypatch.setenv("BUDGET_ENFORCEMENT_ENABLED", "false")
    else:
        await off(s, global_scope=scope == "global")
    assert (await dispatch(s)).permitted
    assert await s.client.hgetall(s.target.key()) == before
    assert s.plan.plan_document == plan
    assert s.node.attempts == 0


@pytest.mark.parametrize("gate", ["member", "claim", "attempts", "expiry"])
async def test_budget_off_keeps_nonfinancial_gates(shared, gate):
    from datetime import UTC, datetime, timedelta

    s = shared
    await off(s)
    if gate == "member":
        await s.session.execute(delete(TenantMembership))
    elif gate == "claim":
        s.claim.active_run_id = "another-worker"
    elif gate == "attempts":
        s.node.attempts = s.policy.limits.max_attempts_per_node
    else:
        raw = dict(s.plan.plan_document["execution_policy"])
        raw["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        s.plan.plan_document = {**s.plan.plan_document, "execution_policy": raw}
    await s.session.flush()
    result = await dispatch(s)
    assert not result.permitted
    assert result.reason not in {DenyReason.BUDGET_UNAVAILABLE, DenyReason.SPEND_UNKNOWN, DenyReason.SPEND_LIMIT_EXCEEDED}


async def test_switching_on_uses_retained_unknown_usage(shared):
    s = shared
    await s.store.reserve("unsettled", Decimal(1), [s.target])
    await s.store.mark_unknown("unsettled", s.target)
    row = await off(s)
    assert (await dispatch(s)).permitted
    row.enabled = True
    await s.session.flush()
    assert (await dispatch(s)).reason == DenyReason.BUDGET_UNAVAILABLE


async def test_global_off_dominates_flow_on_and_private_setting_is_not_policy_authority(shared):
    row = await off(shared, global_scope=True)
    shared.session.add(
        BudgetEnforcementSetting(scope_key=flow_key(shared.flow.org_id, shared.flow.id), enabled=True, revision=1, updated_by="operator")
    )
    await shared.session.flush()
    inputs = await load_in_force_policy(shared.session, org_id=shared.flow.org_id, flow_id=shared.flow.id)
    assert not inputs.policy._budget_enforcement_enabled
    assert "_budget_enforcement_enabled" not in inputs.policy.model_dump()
    row.enabled = True
    await shared.session.flush()
    assert (await load_in_force_policy(shared.session, org_id=shared.flow.org_id, flow_id=shared.flow.id)).policy._budget_enforcement_enabled
