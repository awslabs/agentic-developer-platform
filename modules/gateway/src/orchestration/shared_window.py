"""Attributed execution-window renewal for an exact accepted plan.

Expiry may advance, with an explicit optional increase of the elapsed-time ceiling.
The elapsed time, claims, attempts, scope and spend are retained.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select

from .continuation import digest
from .execution_policy import ExecutionPolicy, policy_hash, stamp_policy
from .models import OrchestrationDecision, OrchestrationExecution
from .state import ActorKind

CONTRACT = "shared-window-renewal/v1"
KIND = "execution_window_renewed"


class WindowRenewalError(ValueError):
    pass


class WindowRenewalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_plan_version: int = Field(strict=True, gt=0)
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: AwareDatetime
    max_wall_clock_seconds: int | None = Field(default=None, strict=True, gt=0, le=604_800)
    resume_expired: bool = Field(default=False, strict=True)
    reason: str = Field(min_length=8, max_length=4000)
    expected_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def accepted_document_hash(plan):
    document = plan.plan_document or {}
    if document.get("execution_continuation") is not None:
        return digest(document)
    # Normal acceptance hashes the compiler's canonical executable document,
    # excluding presentation provenance and absent optional policy/eval fields.
    # A shared continuation deliberately uses its complete adoption document.
    from .compile import plan_hash
    from .proposal import LoopProposal

    try:
        return plan_hash(LoopProposal.model_validate(document))
    except ValueError as error:
        raise WindowRenewalError("accepted_document_unverifiable") from error


async def policy_owner_matches(session, actor_id, policy):
    if actor_id == policy.principal_id:
        return True
    # Protected plans can retain the verified login subject, while the human
    # control route attributes decisions to the canonical workspace user ID.
    from src.shared.identity.resolver import UnresolvableUserEntityError, resolve_root_user_entity_id

    try:
        owner = await resolve_root_user_entity_id(session, policy.org_id, policy.principal_id or "")
    except UnresolvableUserEntityError:
        return False
    return actor_id == owner


def apply_window(policy, limit, decision_id, wall_clock=None):
    # Copy private financial attributes as well as the public policy. Rebuilding
    # from model_dump would discard the already verified run/chain ceilings.
    draft = policy.model_copy(deep=True)
    draft.expires_at = limit
    if wall_clock is not None:
        draft.limits = draft.limits.model_copy(update={"max_wall_clock_seconds": wall_clock})
    draft.policy_id = draft.policy_hash = draft.principal_id = None
    effective = stamp_policy(draft, principal_id=policy.principal_id, org_id=policy.org_id)
    effective._shared_window_decision_id = decision_id
    return effective


async def effective_shared_window(session, plan, policy):
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
        limit = datetime.fromisoformat(data["expires_at"])
        before = datetime.fromisoformat(data["before"])
        accepted = datetime.fromisoformat(data["accepted_at"])
        started = datetime.fromisoformat(data["wall_clock_started_at"]) if data.get("wall_clock_started_at") else None
        if (
            any(t.tzinfo is None for t in (limit, before, accepted))
            or not valid_extension(
                before,
                limit,
                accepted,
                data.get("before_wall_clock_seconds"),
                data.get("max_wall_clock_seconds"),
                started=started,
                resume_expired=data.get("resume_expired") is True,
            )
            or (accepted >= before and data.get("resume_expired") is not True)
        ):
            raise ValueError("invalid renewal window")
    except (ValueError, TypeError, KeyError) as error:
        raise WindowRenewalError("window_receipt_unverifiable") from error
    if type(data.get("plan_version")) is not int or data["plan_version"] > plan.version:
        raise WindowRenewalError("window_receipt_unverifiable")
    if data["plan_version"] < plan.version:
        from .plan_lineage import receipt_plan

        plan = await receipt_plan(session, plan, data)
        if plan is None:
            return policy  # General plan changes still require a fresh approval.
    original = ExecutionPolicy.model_validate(plan.plan_document["execution_policy"])
    wall_clock = data.get("max_wall_clock_seconds")
    prior_wall_clock = data.get("before_wall_clock_seconds", original.limits.max_wall_clock_seconds)
    if wall_clock is not None and (
        type(wall_clock) is not int
        or type(prior_wall_clock) is not int
        or not original.limits.max_wall_clock_seconds <= prior_wall_clock <= wall_clock <= 604_800
        or (
            wall_clock != prior_wall_clock
            and not valid_wall_clock(prior_wall_clock, wall_clock, accepted, started=started, resume_expired=data.get("resume_expired") is True)
        )
    ):
        raise WindowRenewalError("window_wall_clock_receipt_unverifiable")
    if (
        decision.actor_kind != "human"
        or decision.actor_role != "platform_admin"
        or not decision.actor_id
        or not await policy_owner_matches(session, decision.actor_id, policy)
        or data.get("contract") != CONTRACT
        or data.get("org_id") != plan.org_id
        or data.get("flow_id") != plan.flow_id
        or data.get("plan_hash") != plan.plan_hash
        or plan.plan_hash != accepted_document_hash(plan)
        or data.get("original_policy_hash") != original.policy_hash
        or original.policy_hash != policy_hash(original)
        or policy.policy_hash != policy_hash(policy)
        or data.get("principal_id") != policy.principal_id
    ):
        raise WindowRenewalError("window_receipt_unverifiable")
    if limit < policy.expires_at or (limit == policy.expires_at and (wall_clock is None or wall_clock <= policy.limits.max_wall_clock_seconds)):
        raise WindowRenewalError("window_receipt_reduces_limit")
    return apply_window(policy, limit, decision.id, wall_clock)


def valid_wall_clock(prior, seconds, now, *, started=None, resume_expired=False):
    if type(prior) is not int or type(seconds) is not int or not prior < seconds <= 604_800:
        return False
    if seconds - prior <= 86_400:
        return True
    # An explicit owner resumption can recover after a long gate wait. Keep
    # elapsed time and cap the newly granted future window at 24 hours.
    return bool(
        resume_expired
        and started is not None
        and started.tzinfo is not None
        and started + timedelta(seconds=prior) <= now
        and now < started + timedelta(seconds=seconds) <= now + timedelta(hours=24)
    )


def valid_extension(before, after, now, prior_seconds, seconds, *, started=None, resume_expired=False):
    if after == before:
        return after > now and valid_wall_clock(prior_seconds, seconds, now, started=started, resume_expired=resume_expired)
    return max(now, before) < after <= now + timedelta(hours=24)


async def prepare_renewal(session, *, flow_id, actor, request):
    from .shared_amendment import current_plan
    from .shared_policy import shared_inputs

    if actor.actor_kind != ActorKind.HUMAN or actor.actor_role != "platform_admin":
        raise WindowRenewalError("human_platform_admin_required")
    flow, plan = await current_plan(session, flow_id=flow_id, actor=actor)
    if plan.version != request.expected_plan_version or plan.plan_hash != request.expected_plan_hash:
        raise WindowRenewalError("accepted_plan_changed")
    if plan.plan_hash != accepted_document_hash(plan):
        raise WindowRenewalError("accepted_document_hash_changed")
    marker = (plan.plan_document or {}).get("execution_continuation")
    if marker is not None:
        inputs, marker = await shared_inputs(
            session, org_id=actor.org_id, flow_id=flow_id, allow_elapsed_window=request.max_wall_clock_seconds is not None
        )
        started = datetime.fromisoformat(marker["accepted_at"].replace("Z", "+00:00"))
    else:
        from .policy_admission import load_in_force_policy
        from .runtime_policy import flow_started_at

        inputs = await load_in_force_policy(session, org_id=actor.org_id, flow_id=flow_id)
        started = await flow_started_at(session, org_id=actor.org_id, flow_id=flow_id)
        if inputs.refusal is not None or inputs.policy is None:
            raise WindowRenewalError("accepted_policy_unverifiable")
        if started is None:
            raise WindowRenewalError("flow_start_unavailable")
    policy = inputs.policy
    now = datetime.now(UTC)
    if policy.expires_at <= now and not request.resume_expired:
        raise WindowRenewalError("expired_policy_requires_explicit_reacceptance")
    if not valid_extension(
        policy.expires_at,
        request.expires_at,
        now,
        policy.limits.max_wall_clock_seconds,
        request.max_wall_clock_seconds,
        started=started,
        resume_expired=request.resume_expired,
    ):
        raise WindowRenewalError("renewal_must_extend_within_24_hours")
    if not await policy_owner_matches(session, actor.actor_id, policy):
        raise WindowRenewalError("original_principal_required")
    if flow.state not in {"pending", "running"}:
        raise WindowRenewalError("flow_not_running")
    current_wall_clock = policy.limits.max_wall_clock_seconds
    wall_clock = request.max_wall_clock_seconds or current_wall_clock
    if request.max_wall_clock_seconds is not None and not valid_wall_clock(
        current_wall_clock, wall_clock, now, started=started, resume_expired=request.resume_expired
    ):
        raise WindowRenewalError("wall_clock_must_increase_by_at_most_24_hours")
    if started + timedelta(seconds=wall_clock) <= now:
        raise WindowRenewalError("renewed_wall_clock_already_elapsed")
    deadlines = await capped_deadlines(session, flow, plan, policy.expires_at, request.expires_at, current_wall_clock, wall_clock)
    document = {
        "contract": CONTRACT,
        "flow_id": flow.id,
        "org_id": flow.org_id,
        "plan_version": plan.version,
        "plan_hash": plan.plan_hash,
        "original_policy_hash": plan.plan_document["execution_policy"]["policy_hash"],
        "principal_id": policy.principal_id,
        "previous_decision_id": policy._shared_window_decision_id,
        "before": policy.expires_at.isoformat(),
        "resume_expired": request.resume_expired,
        "expires_at": request.expires_at.isoformat(),
        "before_wall_clock_seconds": current_wall_clock,
        "max_wall_clock_seconds": wall_clock,
        "wall_clock_started_at": started.isoformat(),
        "unchanged_limits": policy.limits.model_dump(mode="json", exclude={"max_wall_clock_seconds"} if wall_clock != current_wall_clock else set()),
        "resulting_limits": {**policy.limits.model_dump(mode="json"), "max_wall_clock_seconds": wall_clock},
        "deadlines": deadlines,
    }
    return document, {"snapshot": digest(document), **document, "attempts_preserved": True, "budget_meter_unchanged": True}


async def preview_window_renewal(session, *, flow_id, actor, request):
    _, result = await prepare_renewal(session, flow_id=flow_id, actor=actor, request=request)
    return result


async def accept_window_renewal(session, *, flow_id, actor, request):
    from .shared_amendment import current_plan

    if actor.actor_kind != ActorKind.HUMAN or actor.actor_role != "platform_admin":
        raise WindowRenewalError("human_platform_admin_required")
    if request.expected_snapshot is None:
        raise WindowRenewalError("preview_snapshot_required")
    async with session.begin_nested():
        _, plan = await current_plan(session, flow_id=flow_id, actor=actor, lock=True)
        request_data = {"org_id": actor.org_id, "actor_id": actor.actor_id, "flow_id": flow_id, **request.model_dump(mode="json")}
        identity = str(uuid5(NAMESPACE_URL, CONTRACT + ":" + digest(request_data)))
        existing = await session.get(OrchestrationDecision, identity)
        if existing is not None:
            if existing.org_id != actor.org_id or existing.actor_id != actor.actor_id or existing.kind != KIND:
                raise WindowRenewalError("window_receipt_conflict")
            data = json.loads(existing.reason)
            if data["plan_version"] != plan.version or data["plan_hash"] != plan.plan_hash:
                raise WindowRenewalError("accepted_plan_changed")
            return {"accepted": True, "created": False, "decision_id": identity, **data}
        document, result = await prepare_renewal(session, flow_id=flow_id, actor=actor, request=request)
        if result["snapshot"] != request.expected_snapshot:
            raise WindowRenewalError("window_preview_changed")
        await advance_deadlines(session, document)
        accepted_at = datetime.now(UTC)
        if accepted_at >= datetime.fromisoformat(document["before"]) and not request.resume_expired:
            raise WindowRenewalError("expired_policy_requires_explicit_reacceptance")
        if accepted_at >= request.expires_at:
            raise WindowRenewalError("renewal_already_expired")
        session.add(
            OrchestrationDecision(
                id=identity,
                org_id=actor.org_id,
                flow_id=flow_id,
                kind=KIND,
                actor_id=actor.actor_id,
                actor_role=actor.actor_role,
                actor_kind="human",
                reason=json.dumps({**document, "accepted_at": accepted_at.isoformat(), "snapshot": result["snapshot"], "reason": request.reason}),
            )
        )
        await session.flush()
        return {"accepted": True, "created": True, "decision_id": identity, **result}


def aware(moment):
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


async def capped_deadlines(session, flow, plan, before, after, max_seconds, renewed_seconds=None):
    from .plan_lineage import ancestor_plan

    rows = list(
        await session.scalars(
            select(OrchestrationExecution)
            .where(
                OrchestrationExecution.org_id == flow.org_id,
                OrchestrationExecution.flow_id == flow.id,
                OrchestrationExecution.status.not_in({"concluded", "superseded"}),
            )
            .order_by(OrchestrationExecution.id)
            .limit(1001)
        )
    )
    if len(rows) > 1000:
        raise WindowRenewalError("renewal_history_limit")
    result = []
    for row in rows:
        if await ancestor_plan(session, plan, row.accepted_plan_version, node_id=row.node_id) is None:
            continue
        old_cap = aware(row.created_at) + timedelta(seconds=max_seconds)
        if row.deadline_at is None or aware(row.deadline_at) not in {before, old_cap}:
            continue
        deadline = min(after, aware(row.created_at) + timedelta(seconds=renewed_seconds or max_seconds))
        if deadline > aware(row.deadline_at):
            result.append(
                {
                    "execution_id": row.id,
                    "accepted_plan_version": row.accepted_plan_version,
                    "before": aware(row.deadline_at).isoformat(),
                    "after": deadline.isoformat(),
                }
            )
    return result


async def advance_deadlines(session, document):
    # NOWAIT prevents lock inversion with dispatch/settlement; callers retry the
    # unchanged preview after concurrent work finishes. No worker is restarted.
    from sqlalchemy.exc import DBAPIError

    from .plan_lineage import execution_plan_matches

    for expected in document["deadlines"]:
        try:
            row = await session.scalar(
                select(OrchestrationExecution)
                .where(
                    OrchestrationExecution.id == expected["execution_id"],
                    OrchestrationExecution.org_id == document.get("org_id"),
                )
                .execution_options(populate_existing=True)
                .with_for_update(nowait=True)
            )
        except DBAPIError as error:
            raise WindowRenewalError("execution_update_in_progress") from error
        if (
            row is None
            or row.flow_id != document["flow_id"]
            or row.accepted_plan_version != expected.get("accepted_plan_version", document["plan_version"])
            or row.status in {"concluded", "superseded"}
            or row.deadline_at is None
            or aware(row.deadline_at).isoformat() != expected["before"]
        ):
            raise WindowRenewalError("execution_deadline_changed")
        if not await execution_plan_matches(
            session,
            org_id=row.org_id,
            flow_id=row.flow_id,
            node_id=row.node_id,
            version=row.accepted_plan_version,
            current_version=document["plan_version"],
        ):
            raise WindowRenewalError("execution_deadline_changed")
        row.deadline_at = datetime.fromisoformat(expected["after"])
        row.revision += 1
