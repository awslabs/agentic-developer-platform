"""Attributed concurrency increases for an exact accepted shared-worker plan.

A supplement changes only simultaneous worker capacity. Attempts, expiry, live
assignments and financial limits remain unchanged; admission checks still apply.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .continuation import digest
from .execution_policy import ExecutionPolicy, policy_hash, stamp_policy
from .models import OrchestrationDecision
from .state import ActorKind

CONTRACT = "shared-concurrency-increase/v1"
KIND = "concurrency_limit_increased"


class ConcurrencyIncreaseError(ValueError):
    pass


class ConcurrencyIncreaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_plan_version: int = Field(strict=True, gt=0)
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_concurrent_actions: int = Field(strict=True, ge=1, le=100)
    reason: str = Field(min_length=8, max_length=4000)
    expected_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def apply_concurrency_limit(policy, limit, decision_id):
    # Copy private financial attributes as well as the public policy. Rebuilding
    # from model_dump would discard the already verified run/chain ceilings.
    draft = policy.model_copy(deep=True)
    draft.limits = draft.limits.model_copy(update={"max_concurrent_actions": limit})
    draft.policy_id = draft.policy_hash = draft.principal_id = None
    effective = stamp_policy(draft, principal_id=policy.principal_id, org_id=policy.org_id)
    effective._shared_concurrency_decision_id = decision_id
    return effective


async def effective_shared_concurrency(session, plan, policy):
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
        limit = data.get("max_concurrent_actions")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid concurrency ceiling")
    except (ValueError, TypeError) as error:
        raise ConcurrencyIncreaseError("concurrency_receipt_unverifiable") from error
    if type(data.get("plan_version")) is not int or data["plan_version"] > plan.version:
        raise ConcurrencyIncreaseError("concurrency_receipt_unverifiable")
    if data["plan_version"] < plan.version:
        from .plan_lineage import receipt_plan

        plan = await receipt_plan(session, plan, data)
        if plan is None:
            return policy  # General plan changes still require a fresh approval.
    original = ExecutionPolicy.model_validate(plan.plan_document["execution_policy"])
    if (
        decision.actor_kind != "human"
        or decision.actor_role != "platform_admin"
        or not decision.actor_id
        or decision.actor_id != policy.principal_id
        or data.get("contract") != CONTRACT
        or data.get("flow_id") != plan.flow_id
        or data.get("plan_hash") != plan.plan_hash
        or plan.plan_hash != digest(plan.plan_document)
        or data.get("original_policy_hash") != original.policy_hash
        or original.policy_hash != policy_hash(original)
        or policy.policy_hash != policy_hash(policy)
        or data.get("principal_id") != policy.principal_id
    ):
        raise ConcurrencyIncreaseError("concurrency_receipt_unverifiable")
    if limit <= policy.limits.max_concurrent_actions:
        raise ConcurrencyIncreaseError("concurrency_receipt_reduces_limit")
    return apply_concurrency_limit(policy, limit, decision.id)


async def prepare_increase(session, *, flow_id, actor, request):
    from .shared_amendment import current_plan
    from .shared_policy import shared_inputs

    if actor.actor_kind != ActorKind.HUMAN or actor.actor_role != "platform_admin":
        raise ConcurrencyIncreaseError("human_platform_admin_required")
    flow, plan = await current_plan(session, flow_id=flow_id, actor=actor)
    if plan.version != request.expected_plan_version or plan.plan_hash != request.expected_plan_hash:
        raise ConcurrencyIncreaseError("accepted_plan_changed")
    if plan.plan_hash != digest(plan.plan_document):
        raise ConcurrencyIncreaseError("accepted_document_hash_changed")
    inputs, _ = await shared_inputs(session, org_id=actor.org_id, flow_id=flow_id)
    policy = inputs.policy
    if actor.actor_id != policy.principal_id:
        raise ConcurrencyIncreaseError("original_principal_required")
    if policy.expires_at <= datetime.now(UTC):
        raise ConcurrencyIncreaseError("policy_expired")
    if request.max_concurrent_actions <= policy.limits.max_concurrent_actions:
        raise ConcurrencyIncreaseError("concurrency_limit_must_increase")
    document = {
        "contract": CONTRACT,
        "flow_id": flow.id,
        "plan_version": plan.version,
        "plan_hash": plan.plan_hash,
        "original_policy_hash": plan.plan_document["execution_policy"]["policy_hash"],
        "principal_id": policy.principal_id,
        "previous_decision_id": policy._shared_concurrency_decision_id,
        "before": policy.limits.max_concurrent_actions,
        "max_concurrent_actions": request.max_concurrent_actions,
        "unchanged_limits": policy.limits.model_dump(mode="json", exclude={"max_concurrent_actions"}),
        "expires_at": policy.expires_at.isoformat(),
    }
    return document, {"snapshot": digest(document), **document, "attempts_preserved": True, "budget_meter_unchanged": True}


async def preview_concurrency_increase(session, *, flow_id, actor, request):
    _, result = await prepare_increase(session, flow_id=flow_id, actor=actor, request=request)
    return result


async def accept_concurrency_increase(session, *, flow_id, actor, request):
    from .shared_amendment import current_plan

    if actor.actor_kind != ActorKind.HUMAN or actor.actor_role != "platform_admin":
        raise ConcurrencyIncreaseError("human_platform_admin_required")
    if request.expected_snapshot is None:
        raise ConcurrencyIncreaseError("preview_snapshot_required")
    async with session.begin_nested():
        _, plan = await current_plan(session, flow_id=flow_id, actor=actor, lock=True)
        request_data = {"org_id": actor.org_id, "actor_id": actor.actor_id, "flow_id": flow_id, **request.model_dump(mode="json")}
        identity = str(uuid5(NAMESPACE_URL, CONTRACT + ":" + digest(request_data)))
        existing = await session.get(OrchestrationDecision, identity)
        if existing is not None:
            if existing.org_id != actor.org_id or existing.actor_id != actor.actor_id or existing.kind != KIND:
                raise ConcurrencyIncreaseError("concurrency_receipt_conflict")
            data = json.loads(existing.reason)
            if data["plan_version"] != plan.version or data["plan_hash"] != plan.plan_hash:
                raise ConcurrencyIncreaseError("accepted_plan_changed")
            return {"accepted": True, "created": False, "decision_id": identity, **data}
        document, result = await prepare_increase(session, flow_id=flow_id, actor=actor, request=request)
        if result["snapshot"] != request.expected_snapshot:
            raise ConcurrencyIncreaseError("concurrency_preview_changed")
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
