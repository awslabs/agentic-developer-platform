"""Read-only flow and run balances; missing counters never imply fresh credit."""

import json
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select

from src.budget.enforcement_service import budget_enforcement_service
from src.budget.enforcement_settings import read_enforcement
from src.budget.reservations import ReservationTarget
from src.shared.models.base import utcnow
from src.shared.models.usage import UsageLog
from src.shared.schemas.budget import EntityType

from .cost import escape_like
from .dispatch import graph_address
from .flow_budget import get_flow_reservations
from .flow_meter import meter_target
from .models import OrchestrationAcceptedPlan, OrchestrationDecision
from .plan_lineage import PolicyDisplayLineage
from .policy_admission import load_in_force_policy


class BudgetBalance(BaseModel):
    limit_usd: Decimal | None = None
    spent_usd: Decimal | None = None
    reserved_usd: Decimal | None = None
    remaining_usd: Decimal | None = None
    # Components of reserved_usd, never additional charges. Unknown on old/run
    # projections; neither component may be used to relax admission.
    request_reserved_usd: Decimal | None = None
    recovery_hold_usd: Decimal | None = None


class ProposedBudget(BaseModel):
    """Bounds an unaccepted plan asks for. Display only: nothing is enforced from this.

    A draft's `proposed_execution_policy` is demoted at registration and stamped
    into `execution_policy` only by a human's acceptance. Until then the budget
    panel used to show nothing, so an owner deciding whether to accept could not
    see the limits they were about to grant. This carries those limits, labelled
    as proposed, beside the plan version and hash the owner must quote to accept.
    """

    limit_usd: Decimal | None = None
    concurrency: int | None = None
    attempts_per_node: int | None = None
    wall_clock_seconds: int | None = None
    expires_at: datetime | None = None
    plan_version: int
    plan_hash: str


class FlowBudgetSummary(BaseModel):
    flow_id: str
    observed_at: datetime
    balance: BudgetBalance
    run_limit_usd: Decimal | None = None
    concurrency: int | None = None
    enforcement_enabled: bool
    accounting_incomplete: bool
    # `accepted`: `balance.limit_usd` is the in-force limit. `proposed`: no policy is
    # in force; `proposed` carries what acceptance would grant. `none`: neither.
    policy_status: Literal["accepted", "proposed", "none"] = "none"
    proposed: ProposedBudget | None = None


class RunBudgetSummary(BaseModel):
    run_id: str
    persona: str | None
    last_call_at: datetime
    balance: BudgetBalance


class NodeBudgetSummary(BaseModel):
    flow_id: str
    node_id: str
    observed_at: datetime
    enforcement_enabled: bool
    runs: list[RunBudgetSummary]
    truncated: bool


async def context(db, flow):
    inputs = await load_in_force_policy(db, org_id=flow.org_id, flow_id=flow.id, display_lineage=PolicyDisplayLineage())
    policy = inputs.policy if inputs.refusal is None else None
    posture = await read_enforcement(db, org_id=flow.org_id, flow_id=flow.id)
    run_limit = None
    if policy is not None:
        # Use the admission resolver: an approved increase cannot override a
        # tighter tenant limit. Do not expose the platform default as this flow's cap.
        run_limit = await budget_enforcement_service._resolve_scope_cap(
            db, flow.org_id, EntityType.RUN, approved_platform_cap=policy._shared_run_spend_usd
        )
    return policy, posture, run_limit


async def balance(db, *, org_id, limit, spent, target, store, incomplete=False, recovery_hold_ids=None):
    result = BudgetBalance(limit_usd=limit, spent_usd=spent)
    if target is None or not store.enabled or incomplete:
        return result
    snapshot = await store.snapshot(target, existing_scope=target.entity_type == "run", include_requests=True)
    if snapshot is None or snapshot.request_amounts is None:
        return result
    if target.entity_type == "run" and not snapshot.request_amounts:
        return result  # An expired lifetime counter is not unused run credit.
    amounts = snapshot.request_amounts
    # A continuation seeds already-recorded historical spend in its meter.
    # That seed is never an outstanding model request.
    requests = [key for key in amounts if key != "__historical_spend__"]
    recorded = {}
    for offset in range(0, len(requests), 1000):
        rows = await db.execute(
            select(UsageLog.request_id, func.sum(UsageLog.cost_usd))
            .where(UsageLog.org_id == org_id, UsageLog.request_id.in_(requests[offset : offset + 1000]))
            .group_by(UsageLog.request_id)
        )
        recorded.update(rows.all())
    held = {key: max(Decimal(0), amounts[key] - recorded.get(key, Decimal(0))) for key in requests}
    result.reserved_usd = sum(held.values(), Decimal(0))
    if recovery_hold_ids is not None:
        result.recovery_hold_usd = sum((held[key] for key in held if key in recovery_hold_ids), Decimal(0))
        result.request_reserved_usd = result.reserved_usd - result.recovery_hold_usd
    if spent is None and snapshot.total_usd == 0:
        result.spent_usd = Decimal(0)  # Verified empty initialized meter, not absent usage.
    if result.spent_usd is not None and limit is not None:
        # Preserve all recorded history even if an old counter has expired.
        # This is a display balance, never an admission decision.
        exposure = max(snapshot.total_usd, result.spent_usd + result.reserved_usd)
        result.remaining_usd = max(Decimal(0), limit - exposure)
    return result


async def recovery_hold_ids(db, flow):
    """Only a tenant/flow-bound restoration decision identifies a recovery hold.

    A request with a recovery-looking name alone is not sufficient provenance.
    Keep unknown entries in the request reservation total instead of hiding them.
    """
    reasons = await db.scalars(
        select(OrchestrationDecision.reason).where(
            OrchestrationDecision.org_id == flow.org_id,
            OrchestrationDecision.flow_id == flow.id,
            OrchestrationDecision.kind == "meter_ceiling_restored",
        )
    )
    result = set()
    for reason in reasons:
        try:
            recovery_id = json.loads(reason)["recovery_id"]
            if str(UUID(recovery_id)) == recovery_id:
                result.add(f"recovery-{recovery_id}")
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return result


def _as_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_decimal(value):
    try:
        return Decimal(str(value)) if value is not None else None
    except (ArithmeticError, ValueError, TypeError):
        return None


def _as_datetime(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
    except ValueError:
        return None


async def read_proposed_budget(db, flow):
    """Project the current draft's proposed limits; never a substitute for acceptance."""
    plan = await db.scalar(
        select(OrchestrationAcceptedPlan)
        .where(
            OrchestrationAcceptedPlan.org_id == flow.org_id,
            OrchestrationAcceptedPlan.flow_id == flow.id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
        .order_by(OrchestrationAcceptedPlan.version.desc())
        .limit(1)
    )
    document = plan.plan_document if plan is not None and isinstance(plan.plan_document, dict) else None
    proposed = document.get("proposed_execution_policy") if document else None
    if not isinstance(proposed, dict):
        return None
    limits = proposed.get("limits") if isinstance(proposed.get("limits"), dict) else {}
    return ProposedBudget(
        limit_usd=_as_decimal(limits.get("max_spend_usd")),
        concurrency=_as_int(limits.get("max_concurrent_actions")),
        attempts_per_node=_as_int(limits.get("max_attempts_per_node")),
        wall_clock_seconds=_as_int(limits.get("max_wall_clock_seconds")),
        expires_at=_as_datetime(proposed.get("expires_at")),
        plan_version=plan.version,
        plan_hash=plan.plan_hash,
    )


async def read_flow_budget(db, flow):
    policy, posture, run_limit = await context(db, flow)
    proposed = await read_proposed_budget(db, flow) if policy is None else None
    spent = await db.scalar(
        select(func.sum(UsageLog.cost_usd)).where(
            UsageLog.org_id == flow.org_id,
            UsageLog.graph_address.like(escape_like(flow.slug) + "/%", escape="\\"),
        )
    )
    return FlowBudgetSummary(
        flow_id=flow.id,
        observed_at=utcnow(),
        balance=await balance(
            db,
            org_id=flow.org_id,
            limit=policy.limits.max_spend_usd if policy else None,
            spent=spent,
            target=meter_target(org_id=flow.org_id, flow_id=flow.id, policy=policy) if policy else None,
            store=get_flow_reservations(),
            incomplete=posture.accounting_incomplete,
            recovery_hold_ids=await recovery_hold_ids(db, flow),
        ),
        run_limit_usd=run_limit,
        concurrency=policy.limits.max_concurrent_actions if policy else None,
        enforcement_enabled=posture.enabled,
        accounting_incomplete=posture.accounting_incomplete,
        policy_status="accepted" if policy is not None else "proposed" if proposed is not None else "none",
        proposed=proposed,
    )


async def read_node_budget(db, flow, node):
    policy, posture, run_limit = await context(db, flow)
    rows = (
        await db.execute(
            select(UsageLog.agent_run_id, func.max(UsageLog.persona_key), func.max(UsageLog.timestamp), func.sum(UsageLog.cost_usd))
            .where(
                UsageLog.org_id == flow.org_id, UsageLog.graph_address == graph_address(node, flow_slug=flow.slug), UsageLog.agent_run_id.is_not(None)
            )
            .group_by(UsageLog.agent_run_id)
            .order_by(func.max(UsageLog.timestamp).desc(), UsageLog.agent_run_id)
            .limit(21)
        )
    ).all()
    runs = []
    for run_id, persona, last_call_at, spent in rows[:20]:
        runs.append(
            RunBudgetSummary(
                run_id=run_id,
                persona=persona,
                last_call_at=last_call_at,
                balance=await balance(
                    db,
                    org_id=flow.org_id,
                    limit=run_limit,
                    spent=spent,
                    target=ReservationTarget(flow.org_id, "run", run_id, "run", "lifetime", Decimal(0)) if policy else None,
                    store=budget_enforcement_service._get_reservations(),
                    incomplete=posture.accounting_incomplete,
                ),
            )
        )
    return NodeBudgetSummary(
        flow_id=flow.id, node_id=node.id, observed_at=utcnow(), enforcement_enabled=posture.enabled, runs=runs, truncated=len(rows) > 20
    )
