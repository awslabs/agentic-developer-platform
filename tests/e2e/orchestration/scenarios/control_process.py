"""Scoped native allowance/policy probes; no direct table or Redis writes.

Executed unchanged in the verified gateway interpreter. Admission fixtures use
real server-created gate node IDs and actual reservation services, but never
launch model work. Their reservations are canceled only while those nodes have
no invocation. These are service-boundary probes, not fabricated worker runs.
"""

import asyncio
from dataclasses import asdict
from decimal import Decimal
import json
import re
import sys


def validate(request):
    if set(request) != {
        "mode",
        "org_id",
        "flow_id",
        "qualification_id",
        "definition_hash",
        "plan_version",
        "plan_hash",
    }:
        raise ValueError("invalid control request")
    if request["mode"] not in {"policy", "budget", "cancel"}:
        raise ValueError("invalid control operation")
    if not re.fullmatch(r"q-[a-z0-9-]{8,50}", request["qualification_id"]):
        raise ValueError("invalid qualification")
    if not re.fullmatch(
        r"[0-9a-f]{64}", request["definition_hash"]
    ) or not re.fullmatch(r"[0-9a-f]{64}", request["plan_hash"]):
        raise ValueError("unverified definition/plan")
    if type(request["plan_version"]) is not int or request["plan_version"] < 1:
        raise ValueError("invalid plan version")
    return request


async def read_scope(session, request):
    from sqlalchemy import select
    from src.orchestration.models import (
        OrchestrationFlow,
        OrchestrationNode,
        OrchestrationAcceptedPlan,
        OrchestrationExecution,
    )
    from src.orchestration.policy_admission import load_in_force_policy

    flow = await session.scalar(
        select(OrchestrationFlow).where(
            OrchestrationFlow.id == request["flow_id"],
            OrchestrationFlow.org_id == request["org_id"],
        )
    )
    suffix = "-revocation" if request["mode"] == "policy" else "-allowance"
    if flow is None or flow.slug != request["qualification_id"] + suffix:
        raise ValueError("foreign control flow")
    plans = list(
        (
            await session.scalars(
                select(OrchestrationAcceptedPlan).where(
                    OrchestrationAcceptedPlan.flow_id == flow.id,
                    OrchestrationAcceptedPlan.org_id == flow.org_id,
                )
            )
        ).all()
    )
    current = [p for p in plans if p.superseded_at is None]
    if (
        len(current) != 1
        or current[0].plan_hash != request["plan_hash"]
        or current[0].version != request["plan_version"]
        or current[0].plan_document.get("spec_revision") != request["definition_hash"]
    ):
        raise ValueError("control plan changed")
    nodes = list(
        (
            await session.scalars(
                select(OrchestrationNode).where(
                    OrchestrationNode.flow_id == flow.id,
                    OrchestrationNode.org_id == flow.org_id,
                )
            )
        ).all()
    )
    executions = list(
        (
            await session.scalars(
                select(OrchestrationExecution.id).where(
                    OrchestrationExecution.flow_id == flow.id,
                    OrchestrationExecution.org_id == flow.org_id,
                )
            )
        ).all()
    )
    if (
        len(nodes) != 3
        or any(n.kind != "gate" or n.attempts for n in nodes)
        or executions
    ):
        raise ValueError("control fixture contains dispatchable or active work")
    inputs = await load_in_force_policy(session, org_id=flow.org_id, flow_id=flow.id)
    return flow, nodes, plans, inputs


async def allowance(session, flow, nodes, inputs, *, cancel=False):
    from src.orchestration.flow_budget import (
        admission_cost_usd,
        flow_reservation_target,
        get_flow_reservations,
        reserve_flow_admission,
        release_flow_admission,
    )
    from src.orchestration.policy_admission import _observed_spend

    if inputs.refusal or inputs.policy is None:
        raise ValueError("accepted allowance unavailable")
    policy = inputs.policy
    spend = await _observed_spend(
        session, org_id=flow.org_id, flow_slug=flow.slug, nodes=nodes, policy=policy
    )
    if spend.total_usd is None or spend.total_usd != 0:
        raise ValueError("unused control fixture spend is not verified zero")
    target = flow_reservation_target(
        org_id=flow.org_id, flow_id=flow.id, policy=policy, settled_usd=spend.total_usd
    )
    store = get_flow_reservations()
    if not store.enabled:
        raise ValueError("reservation service unavailable")
    kwargs = dict(
        org_id=flow.org_id, flow_id=flow.id, policy=policy, settled_usd=spend.total_usd
    )
    if cancel:
        for node in nodes:
            await release_flow_admission(**kwargs, node_id=node.id)
        snapshot = await store.snapshot(target)
        if snapshot is None or snapshot.total_usd != 0:
            raise ValueError("fixture admission cancellation not verified")
        return {
            "canceled_node_ids": [n.id for n in nodes],
            "remaining_usd": "0",
            "allowance_id": target.key(),
        }
    cost = admission_cost_usd(policy)
    # The manifest pins this cap. Never change a service cap or expand policy to
    # make the test fit; insufficient configuration is explicitly NOT_RUN.
    if cost <= 0 or policy.limits.max_spend_usd != 2 * cost:
        raise ValueError(
            "allowance fixture policy must equal two actual per-run ceilings"
        )
    anchor = await store.reserve("__initialized__", Decimal(0), [target])
    if anchor is None or not anchor.admitted:
        raise ValueError("cannot observe reservation accumulator")
    before = await store.snapshot(target)
    if before is None or before.total_usd != 0:
        raise ValueError("fixture allowance already used; reconcile before repeat")
    try:
        admitted = await asyncio.gather(
            *(reserve_flow_admission(**kwargs, node_id=n.id) for n in nodes[:2])
        )
        if not all(r.admitted and not r.degraded for r in admitted):
            raise ValueError("two concurrent fixture reservations not admitted")
        held = await store.snapshot(target)
        first = await reserve_flow_admission(**kwargs, node_id=nodes[2].id)
        # A repair retry cannot address a fresh allowance: node and flow identities
        # come from the same server rows. A second call must still refuse it.
        repair = await reserve_flow_admission(**kwargs, node_id=nodes[2].id)
        after = await store.snapshot(target)
        if held is None or after is None:
            raise ValueError("reservation accounting unavailable")
        result = dict(
            flow_id=flow.id,
            policy_id=policy.policy_id,
            plan_version=inputs.plan_version,
            allowance_id=target.key(),
            limit=str(policy.limits.max_spend_usd),
            settled_usd=str(spend.total_usd),
            reserved_usd=str(held.total_usd),
            after_usd=str(after.total_usd),
            node_ids=[n.id for n in nodes],
            fanout={
                "admitted": first.admitted,
                "degraded": first.degraded,
                "allowance_id": first.target.key(),
            },
            repair={
                "admitted": repair.admitted,
                "degraded": repair.degraded,
                "allowance_id": repair.target.key(),
            },
            executions=[],
            boundary="flow_budget.reserve_flow_admission",
            fixture="undispatched server-created gate nodes",
        )
    finally:
        # These holds have never admitted remote work. Their real nodes were
        # verified non-dispatchable; cancellation cannot release running spend.
        for node in nodes:
            await release_flow_admission(**kwargs, node_id=node.id)
    canceled = await store.snapshot(target)
    if canceled is None or canceled.total_usd != 0:
        raise ValueError("canceled fixture holds not reconciled")
    result["cancellation"] = {"remaining_usd": str(canceled.total_usd)}
    return result


async def execute(request):
    validate(request)
    from src.shared.database import get_session_factory

    async with get_session_factory()() as session:
        flow, nodes, plans, inputs = await read_scope(session, request)
        if request["mode"] == "policy":
            previous = [
                p
                for p in plans
                if p.version < inputs.plan_version
                and p.plan_document.get("execution_policy")
            ]
            return dict(
                flow_id=flow.id,
                plan_version=inputs.plan_version,
                policy=inputs.policy.model_dump(mode="json") if inputs.policy else None,
                refusal=asdict(inputs.refusal) if inputs.refusal else None,
                previous_policy_versions=[p.version for p in previous],
                executions=[],
                boundary="policy_admission.load_in_force_policy",
            )
        return await allowance(
            session, flow, nodes, inputs, cancel=request["mode"] == "cancel"
        )


if __name__ == "__main__":
    try:
        value = asyncio.run(execute(json.loads(sys.argv[1])))
    except Exception as exc:
        value = {
            "status": "NOT_RUN",
            "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
        }
    print("ADP_Q2_RESULT:" + json.dumps(value, default=str), flush=True)
