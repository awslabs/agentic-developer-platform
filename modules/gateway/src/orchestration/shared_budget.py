"""Attributed financial increases for a live shared-worker plan.

The accepted graph and worker assignments remain immutable. An append-only human
budget decision, bound to that exact plan/hash, supplements only its financial
limits. It never renews authority or resets the existing reservation accumulators.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from .continuation import digest
from .execution_policy import ExecutionPolicy, policy_hash, stamp_policy
from .models import OrchestrationDecision
from .state import ActorKind

CONTRACT = "shared-budget-increase/v1"
KIND = "budget_increased"


class BudgetIncreaseError(ValueError):
    pass


class FinancialLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_spend_usd: Decimal = Field(gt=0, le=100000)
    max_run_spend_usd: Decimal = Field(gt=0, le=100000)
    max_chain_spend_usd: Decimal = Field(gt=0, le=100000)

    @model_validator(mode="after")
    def bounded(self):
        if not self.max_run_spend_usd <= self.max_chain_spend_usd <= self.max_spend_usd:
            raise ValueError("run <= chain <= flow limits are required")
        return self


class BudgetIncreaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_plan_version: int = Field(gt=0)
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    limits: FinancialLimits
    reason: str = Field(min_length=8, max_length=4000)
    expected_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def financial_limits(policy):
    from src.budget.config import budget_config

    return FinancialLimits(
        max_spend_usd=policy.limits.max_spend_usd,
        max_run_spend_usd=policy._shared_run_spend_usd or min(budget_config.budget_run_cap_usd, policy.limits.max_spend_usd),
        max_chain_spend_usd=policy._shared_chain_spend_usd or min(budget_config.budget_chain_cap_usd, policy.limits.max_spend_usd),
    )


def only_spend_increased(before, after):
    before, after = json.loads(json.dumps(before)), json.loads(json.dumps(after))
    prior = Decimal(before["limits"].pop("max_spend_usd"))
    current = Decimal(after["limits"].pop("max_spend_usd"))
    for document in (before, after):
        document.pop("policy_id", None)
        document.pop("policy_hash", None)
    return current >= prior and before == after


def apply_limits(policy, limits, decision_id):
    raw = policy.model_dump(mode="json", exclude={"policy_id", "policy_hash", "principal_id"})
    raw["limits"]["max_spend_usd"] = str(limits.max_spend_usd)
    effective = stamp_policy(ExecutionPolicy.model_validate(raw), principal_id=policy.principal_id, org_id=policy.org_id)
    effective._shared_run_spend_usd = limits.max_run_spend_usd
    effective._shared_chain_spend_usd = limits.max_chain_spend_usd
    effective._shared_budget_decision_id = decision_id
    return effective


async def effective_shared_budget(session, plan, policy):
    """A budget receipt cannot confer authority outside its exact accepted plan."""
    marker = (plan.plan_document or {}).get("execution_continuation") or {}
    if marker.get("mode") != "shared_worker_role" or marker.get("contract_version") != 1:
        return policy
    decision = await session.scalar(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == plan.org_id,
            OrchestrationDecision.flow_id == plan.flow_id,
            OrchestrationDecision.kind == KIND,
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    if decision is None:
        return policy
    try:
        data = json.loads(decision.reason or "{}")
        if not isinstance(data, dict):
            raise ValueError("not an object")
        limits = FinancialLimits.model_validate(data.get("limits"))
    except (ValueError, TypeError) as error:
        raise BudgetIncreaseError("budget_receipt_unverifiable") from error
    if type(data.get("plan_version")) is not int or data["plan_version"] > plan.version:
        raise BudgetIncreaseError("budget_receipt_unverifiable")
    if data["plan_version"] < plan.version:
        from .plan_lineage import receipt_plan

        plan = await receipt_plan(session, plan, data)
        if plan is None:
            return policy  # General plan changes still require a fresh approval.
    if (
        decision.actor_kind != "human"
        or decision.actor_role != "platform_admin"
        or not decision.actor_id
        or data.get("contract") != CONTRACT
        or data.get("plan_hash") != plan.plan_hash
        or plan.plan_hash != digest(plan.plan_document)
        or data.get("original_policy_hash") != policy.policy_hash
        or policy.policy_hash != policy_hash(policy)
        or data.get("principal_id") != policy.principal_id
    ):
        raise BudgetIncreaseError("budget_receipt_unverifiable")
    before = financial_limits(policy)
    if any(getattr(limits, key) < getattr(before, key) for key in FinancialLimits.model_fields):
        raise BudgetIncreaseError("budget_receipt_reduces_authority")
    return apply_limits(policy, limits, decision.id)


async def prepare_increase(session, *, flow_id, actor, request, lock=False):
    from .shared_amendment import current_plan
    from .shared_policy import read_flow_meter, shared_inputs

    if actor.actor_kind != ActorKind.HUMAN or actor.actor_role != "platform_admin":
        raise BudgetIncreaseError("human_platform_admin_required")
    flow, plan = await current_plan(session, flow_id=flow_id, actor=actor, lock=lock)
    if plan.version != request.expected_plan_version or plan.plan_hash != request.expected_plan_hash:
        raise BudgetIncreaseError("accepted_plan_changed")
    if plan.plan_hash != digest(plan.plan_document):
        raise BudgetIncreaseError("accepted_document_hash_changed")
    inputs, marker = await shared_inputs(session, org_id=actor.org_id, flow_id=flow_id)
    policy = inputs.policy
    if policy.expires_at <= datetime.now(UTC):
        raise BudgetIncreaseError("policy_expired")
    before = financial_limits(policy)
    if any(getattr(request.limits, key) < getattr(before, key) for key in FinancialLimits.model_fields):
        raise BudgetIncreaseError("budget_increase_must_not_reduce_limits")
    if request.limits == before:
        raise BudgetIncreaseError("budget_limits_unchanged")
    meter = await read_flow_meter(org_id=actor.org_id, flow_id=flow_id, policy=policy)
    if meter is None or meter.total_usd < Decimal(marker["prior_spend_usd"]):
        raise BudgetIncreaseError("budget_usage_unavailable")
    document = {
        "contract": CONTRACT,
        "flow_id": flow.id,
        "plan_version": plan.version,
        "plan_hash": plan.plan_hash,
        "original_policy_hash": plan.plan_document["execution_policy"]["policy_hash"],
        "principal_id": policy.principal_id,
        "previous_decision_id": policy._shared_budget_decision_id,
        "before": before.model_dump(mode="json"),
        "limits": request.limits.model_dump(mode="json"),
        "unchanged_limits": policy.limits.model_dump(mode="json", exclude={"max_spend_usd"}),
        "expires_at": policy.expires_at.isoformat(),
    }
    snapshot = digest(document)
    return document, {"snapshot": snapshot, **document, "observed_reserved_and_settled_usd": str(meter.total_usd), "budget_meter_unchanged": True}


async def preview_budget_increase(session, *, flow_id, actor, request):
    _, result = await prepare_increase(session, flow_id=flow_id, actor=actor, request=request)
    return result


async def accept_budget_increase(session, *, flow_id, actor, request):
    from .shared_amendment import current_plan

    if actor.actor_kind != ActorKind.HUMAN or actor.actor_role != "platform_admin":
        raise BudgetIncreaseError("human_platform_admin_required")
    if request.expected_snapshot is None:
        raise BudgetIncreaseError("preview_snapshot_required")
    async with session.begin_nested():
        _, plan = await current_plan(session, flow_id=flow_id, actor=actor, lock=True)
        request_data = {"org_id": actor.org_id, "actor_id": actor.actor_id, "flow_id": flow_id, **request.model_dump(mode="json")}
        identity = str(uuid5(NAMESPACE_URL, CONTRACT + ":" + digest(request_data)))
        existing = await session.get(OrchestrationDecision, identity)
        if existing is not None:
            if existing.org_id != actor.org_id or existing.actor_id != actor.actor_id or existing.kind != KIND:
                raise BudgetIncreaseError("budget_receipt_conflict")
            data = json.loads(existing.reason)
            if data["plan_version"] != plan.version or data["plan_hash"] != plan.plan_hash:
                raise BudgetIncreaseError("accepted_plan_changed")
            return {"accepted": True, "created": False, "decision_id": identity, **data}
        document, result = await prepare_increase(session, flow_id=flow_id, actor=actor, request=request)
        if result["snapshot"] != request.expected_snapshot:
            raise BudgetIncreaseError("budget_preview_changed")
        session.add(
            OrchestrationDecision(
                id=identity,
                org_id=actor.org_id,
                flow_id=flow_id,
                kind=KIND,
                actor_id=actor.actor_id,
                actor_role=actor.actor_role,
                actor_kind="human",
                reason=json.dumps({**document, "snapshot": result["snapshot"], "reason": request.reason}),
            )
        )
        await session.flush()
        return {"accepted": True, "created": True, "decision_id": identity, **result}
