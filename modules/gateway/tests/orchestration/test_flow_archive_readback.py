"""Archiving keeps graph lineage, usage rows and reconciled cost readable."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select

from src.orchestration import flow_meter
from src.orchestration.repository import OrchestrationRepository
from src.shared.models.budget import BudgetSettlementReceipt
from src.shared.models.usage import UsageLog
from tests.orchestration import test_flow_controls as control_fixtures
from tests.orchestration import test_flow_meter as meter_fixtures
from tests.orchestration import test_read_api as read_fixtures

session = read_fixtures.session
app_with_router = read_fixtures.app_with_router
controls = control_fixtures.controls
authority = meter_fixtures.authority
meter = meter_fixtures.meter


async def test_archived_flow_detail_cost_and_reconciled_receipt_remain_readable(session, app_with_router, controls, meter):
    flow = await read_fixtures.seed_flow(session)
    assert await flow_meter.prepare_flow_meter(org_id=flow.org_id, flow_id=flow.id, policy=meter.policy, nodes=[])
    node = await read_fixtures.seed_node(session, flow, node_ref="story-1", state="passed")
    await read_fixtures.seed_usage(session, address=read_fixtures.address_of(node), cost_usd=Decimal("3.250000"))
    target = flow_meter.meter_target(org_id=flow.org_id, flow_id=flow.id, policy=meter.policy)
    assert (await meter.store.reserve("spent-before-archive", Decimal(20), [target])).admitted
    session.add(
        BudgetSettlementReceipt(
            org_id=flow.org_id,
            request_id="spent-before-archive",
            allocation_key="test",
            user_id="worker",
            total_tokens=150,
            cost_usd=Decimal("3.250000"),
            reservation_scope_keys=[target.key()],
        )
    )
    await session.commit()
    settled = await flow_meter.reconcile_flow_meter(session, org_id=flow.org_id, flow_id=flow.id, policy=meter.policy)
    assert settled is not None and settled.total_usd == Decimal("3.250000") and not settled.has_pending

    client = read_fixtures.client_for(app_with_router)
    baseline_detail = client.get(f"/orchestration/flows/{flow.id}")
    baseline_cost = client.get(f"/orchestration/flows/{flow.id}/cost")
    assert baseline_detail.status_code == 200 and baseline_cost.status_code == 200
    archived = await controls[0].post(f"/orchestration/flows/{flow.id}/archive", json={"reason": "Delivery is complete"})
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"

    detail = client.get(f"/orchestration/flows/{flow.id}")
    cost = client.get(f"/orchestration/flows/{flow.id}/cost")
    assert detail.status_code == 200, detail.text
    assert cost.status_code == 200, cost.text
    assert datetime.fromisoformat(detail.json()["archived_at"]).tzinfo is not None
    assert baseline_detail.json()["archived_at"] is None
    assert detail.json()["flow_id"] == flow.id
    assert detail.json()["slug"] == flow.slug
    assert detail.json()["nodes"] == baseline_detail.json()["nodes"]
    assert detail.json()["cost"] == baseline_detail.json()["cost"]
    assert cost.json() == baseline_cost.json()
    assert Decimal(cost.json()["amount_usd"]) == Decimal("3.250000")
    assert (await session.scalar(select(func.count()).select_from(UsageLog).where(UsageLog.graph_address == read_fixtures.address_of(node)))) == 1

    reconciled_again = await flow_meter.reconcile_flow_meter(session, org_id=flow.org_id, flow_id=flow.id, policy=meter.policy)
    assert reconciled_again is not None and reconciled_again.total_usd == Decimal("3.250000") and not reconciled_again.has_pending


async def test_archived_flow_retains_lineage_and_usage_after_meter_reconciliation(session, app_with_router, controls, meter):
    flow = await read_fixtures.seed_flow(session)
    node = await read_fixtures.seed_node(session, flow, node_ref="story-1", state="passed")
    address = read_fixtures.address_of(node)
    lineage = {"story_id": "STORY-1", "team_id": "delivery"}
    node.lineage = lineage
    await OrchestrationRepository(session).record_accepted_plan(
        org_id=flow.org_id,
        flow_id=flow.id,
        plan_document={"nodes": [{"address": address, "lineage": lineage}]},
        plan_hash="story-1-plan",
    )
    await read_fixtures.seed_usage(session, address=address, cost_usd=Decimal("4.125000"))
    assert await flow_meter.prepare_flow_meter(org_id=flow.org_id, flow_id=flow.id, policy=meter.policy, nodes=[])
    target = flow_meter.meter_target(org_id=flow.org_id, flow_id=flow.id, policy=meter.policy)
    assert (await meter.store.reserve("lineage-spend", Decimal(20), [target])).admitted
    session.add(
        BudgetSettlementReceipt(
            org_id=flow.org_id,
            request_id="lineage-spend",
            allocation_key="test",
            user_id="worker",
            total_tokens=150,
            cost_usd=Decimal("4.125000"),
            reservation_scope_keys=[target.key()],
        )
    )
    await session.commit()
    before = await flow_meter.reconcile_flow_meter(session, org_id=flow.org_id, flow_id=flow.id, policy=meter.policy)
    assert before is not None and before.total_usd == Decimal("4.125000") and not before.has_pending

    client = read_fixtures.client_for(app_with_router)
    archived = await controls[0].post(f"/orchestration/flows/{flow.id}/archive", json={"reason": "Delivery is complete"})
    assert archived.status_code == 200, archived.text

    detail = client.get(f"/orchestration/flows/{flow.id}")
    cost = client.get(f"/orchestration/flows/{flow.id}/cost")
    plans = client.get(f"/orchestration/flows/{flow.id}/plans")
    assert detail.status_code == 200, detail.text
    assert cost.status_code == 200, cost.text
    assert plans.status_code == 200, plans.text
    assert detail.json()["archived_at"] is not None
    assert all(detail.json()["nodes"][0]["lineage"][key] == value for key, value in lineage.items())
    assert plans.json()[0]["plan_document"]["nodes"][0]["lineage"] == lineage
    assert detail.json()["cost"] == cost.json()
    assert Decimal(cost.json()["amount_usd"]) == Decimal("4.125000")
    usage = list(await session.scalars(select(UsageLog).where(UsageLog.org_id == flow.org_id, UsageLog.graph_address == address)))
    assert len(usage) == 1 and usage[0].cost_usd == Decimal("4.125000")

    after = await flow_meter.reconcile_flow_meter(session, org_id=flow.org_id, flow_id=flow.id, policy=meter.policy)
    assert after is not None and after.total_usd == before.total_usd and not after.has_pending
    assert client.get(f"/orchestration/flows/{flow.id}/cost").json() == cost.json()
