"""Deterministic, inert PostgreSQL data for Delivery Flow read regressions."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import insert

from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationExecution, OrchestrationFlow, OrchestrationNode
from src.shared.models.usage import UsageLog

FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)
TENANTS = ("fixture-tenant-a", "fixture-tenant-b")
FLOW_COUNT = 20
NODES_PER_FLOW = 100
DECISION_COUNT = 65_000
HISTORY_COUNT = 34
HOPS = (1, 10, 30)


def fixed_id(*parts: object) -> str:
    return str(uuid5(NAMESPACE_URL, "flow-read-fixture/" + "/".join(map(str, parts))))


async def populate_flows(session):
    """Insert one fully owned data set; the caller owns transaction and database teardown."""
    flow_rows = []
    node_rows = []
    plan_rows = []
    receipt_rows = []
    execution_rows = []
    for flow_number in range(FLOW_COUNT):
        tenant = TENANTS[flow_number % len(TENANTS)]
        flow_id = fixed_id("flow", flow_number)
        flow_rows.append(
            dict(
                id=flow_id,
                org_id=tenant,
                slug=f"fixture-flow-{flow_number:02d}",
                title=f"Fixture flow {flow_number:02d}",
                state="pending",
                created_at=FIXED_TIME,
            )
        )
        for node_number in range(NODES_PER_FLOW):
            node_rows.append(
                dict(
                    id=fixed_id("node", flow_number, node_number),
                    flow_id=flow_id,
                    org_id=tenant,
                    epic_ref="epic-1",
                    wave_ref="wave-1",
                    node_ref=f"story-{node_number:03d}",
                    kind="story",
                    state="pending",
                    title=f"Story {node_number:03d}",
                    created_at=FIXED_TIME,
                )
            )
        hops = HOPS[flow_number] if flow_number < len(HOPS) else 0
        protected_address = f"{flow_rows[-1]['slug']}/epic-1/wave-1/story-000"
        for version in range(1, hops + 2):
            plan_rows.append(
                dict(
                    id=fixed_id("plan", flow_number, version),
                    flow_id=flow_id,
                    org_id=tenant,
                    version=version,
                    plan_document={"nodes": [{"address": protected_address, "kind": "story"}], "edges": [], "fixture_version": version},
                    plan_hash="0" * 64,
                    created_at=FIXED_TIME,
                    accepted_by_decision_id=fixed_id("receipt", flow_number, version) if version > 1 else None,
                    superseded_at=FIXED_TIME if version <= hops else None,
                )
            )
            if version > 1:
                receipt_rows.append(
                    dict(
                        id=fixed_id("receipt", flow_number, version),
                        org_id=tenant,
                        flow_id=flow_id,
                        kind="plan_amended",
                        actor_id="fixture-human",
                        actor_role="org_admin",
                        actor_kind="human",
                        reason="inert amendment receipt",
                        created_at=FIXED_TIME,
                    )
                )
        if flow_number < len(HOPS):
            node_rows[-NODES_PER_FLOW]["attempts"] = 1
            node_rows[-NODES_PER_FLOW]["state"] = "running"
            execution_rows.append(
                dict(
                    id=fixed_id("execution", flow_number),
                    org_id=tenant,
                    flow_id=flow_id,
                    node_id=node_rows[-NODES_PER_FLOW]["id"],
                    cycle=1,
                    phase="delivering",
                    status="runnable",
                    accepted_plan_version=1,
                    claim_id=fixed_id("claim", flow_number),
                    claim_generation=1,
                )
            )
    await session.execute(insert(OrchestrationFlow), flow_rows)
    await session.execute(insert(OrchestrationNode), node_rows)
    await session.execute(insert(OrchestrationAcceptedPlan), plan_rows)
    await session.execute(insert(OrchestrationDecision), receipt_rows)
    await session.execute(insert(OrchestrationExecution), execution_rows)
    await session.execute(
        insert(UsageLog),
        [
            dict(
                id=fixed_id("usage", flow_number),
                org_id=flow_rows[flow_number]["org_id"],
                timestamp=FIXED_TIME,
                department_id="fixture",
                team_id="fixture",
                user_id="fixture",
                account_type="service",
                model="fixture-only",
                input_tokens=1,
                output_tokens=1,
                cost_usd=Decimal(amount),
                latency_ms=0,
                status_code=200,
                graph_address=f"{flow_rows[flow_number]['slug']}/epic-1/wave-1/story-000",
                agent_run_id=fixed_id("event", flow_number),
            )
            for flow_number, amount in ((0, "1.250000"), (1, "9.750000"))
        ],
    )

    primary_flow = flow_rows[0]
    history_rows = DECISION_COUNT - len(receipt_rows)
    for start in range(0, history_rows, 1_000):
        await session.execute(
            insert(OrchestrationDecision),
            [
                dict(
                    id=fixed_id("decision", decision_number),
                    org_id=primary_flow["org_id"],
                    flow_id=primary_flow["id"],
                    node_id=node_rows[decision_number]["id"] if decision_number < HISTORY_COUNT else None,
                    kind="node_dispatched" if decision_number < HISTORY_COUNT else "fixture_history",
                    actor_id="fixture",
                    actor_role="service",
                    actor_kind="service",
                    reason=(f"history-{decision_number:02d}" if decision_number < HISTORY_COUNT else None),
                    created_at=FIXED_TIME,
                )
                for decision_number in range(start, min(start + 1_000, history_rows))
            ],
        )
    await session.flush()
    return {
        "tenants": TENANTS,
        "flow_ids": tuple(flow["id"] for flow in flow_rows),
        "flow_count": FLOW_COUNT,
        "nodes_per_flow": NODES_PER_FLOW,
        "decision_count": DECISION_COUNT,
        "history_count": HISTORY_COUNT,
        "amendment_hops": HOPS,
        "versions": tuple(hops + 1 for hops in HOPS),
        "clock": FIXED_TIME.isoformat(),
        "seed": "uuid5:NAMESPACE_URL:flow-read-fixture",
        "expected_states": {"pending": FLOW_COUNT * NODES_PER_FLOW - len(HOPS), "running": len(HOPS)},
        "expected_costs": {0: "1.250000", 1: "9.750000"},
    }
