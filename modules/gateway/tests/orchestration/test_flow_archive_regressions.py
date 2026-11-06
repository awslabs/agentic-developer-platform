"""Archived history cannot be mistaken for free cost or a reusable slug."""

from datetime import UTC

import pytest
from sqlalchemy import func, select

from src.orchestration.compile import ApprovalContext, plan_hash
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationFlow, OrchestrationNode
from src.orchestration.registration import DraftFlowConflictError, _refuse_if_flow_is_live, register_draft_proposal, transform_for_registration
from src.orchestration.repository import OrchestrationRepository
from src.shared.models.usage import UsageLog
from tests.orchestration import test_flow_controls as control_fixtures
from tests.orchestration import test_list_flows as list_fixtures
from tests.orchestration import test_read_api as read_fixtures

session = read_fixtures.session
app_with_router = read_fixtures.app_with_router
controls = control_fixtures.controls


async def test_archived_flow_without_usage_does_not_report_zero_cost(session, app_with_router, controls):
    flow = await read_fixtures.seed_flow(session)
    node = await read_fixtures.seed_node(session, flow, node_ref="unmeasured", state="passed")
    await session.commit()

    response = await controls[0].post(f"/orchestration/flows/{flow.id}/archive", json={"reason": "Delivery is complete"})
    assert response.status_code == 200, response.text
    client = read_fixtures.client_for(app_with_router)
    detail = client.get(f"/orchestration/flows/{flow.id}")
    cost = client.get(f"/orchestration/flows/{flow.id}/cost")

    assert detail.status_code == 200, detail.text
    assert cost.status_code == 200, cost.text
    assert detail.json()["archived_at"] is not None
    assert detail.json()["nodes"][0]["node_ref"] == node.node_ref
    assert detail.json()["cost"] == cost.json()
    assert cost.json()["status"] == "unknown"
    assert cost.json()["amount_usd"] is None
    assert cost.json()["partial"] is True
    assert cost.json()["nodes"][0]["status"] == "unknown"
    assert cost.json()["nodes"][0]["amount_usd"] is None
    assert (await session.scalar(select(func.count()).select_from(UsageLog).where(UsageLog.graph_address == read_fixtures.address_of(node)))) == 0


async def test_archived_slug_refuses_registration_and_keeps_history(session, controls):
    slug = "reserved-history"
    archived = await read_fixtures.seed_flow(session, slug=slug)
    await session.commit()
    response = await controls[0].post(f"/orchestration/flows/{archived.id}/archive", json={"reason": "The draft was retired"})
    assert response.status_code == 200, response.text
    archived_at = archived.archived_at
    repo = OrchestrationRepository(session)
    proposal = list_fixtures.proposal_for(slug)

    with pytest.raises(DraftFlowConflictError, match="archived.*slug remains reserved"):
        await _refuse_if_flow_is_live(session, proposal=proposal, org_id=archived.org_id, document_hash=plan_hash(proposal))

    with pytest.raises(DraftFlowConflictError, match="archived.*slug remains reserved"):
        await register_draft_proposal(session, proposal, ApprovalContext(org_id=archived.org_id, actor_id="registrar", actor_role="member"))

    reused = await repo.create_flow(org_id=archived.org_id, slug=slug, title="Different delivery")
    await session.commit()
    assert reused.id == archived.id
    assert reused.title == archived.title
    assert reused.archived_at.replace(tzinfo=reused.archived_at.tzinfo or UTC) == archived_at
    assert (await session.scalar(select(func.count()).select_from(OrchestrationFlow).where(OrchestrationFlow.org_id == archived.org_id))) == 1
    assert (await session.scalar(select(func.count()).select_from(OrchestrationNode).where(OrchestrationNode.flow_id == archived.id))) == 0
    assert (
        await session.scalar(select(func.count()).select_from(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == archived.id))
    ) == 0
    decisions = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == archived.id)))
    assert [(decision.kind, decision.reason) for decision in decisions] == [("flow_archived", "The draft was retired")]

    other_tenant = await repo.create_flow(org_id="another-tenant", slug=slug, title="Independent delivery")
    assert other_tenant.id != archived.id


async def test_archived_existing_plan_cannot_replay_registration(session, controls):
    archived = await read_fixtures.seed_flow(session, slug="retired-plan")
    proposal = list_fixtures.proposal_for(archived.slug)
    transformed, _ = transform_for_registration(proposal)
    repo = OrchestrationRepository(session)
    await repo.record_accepted_plan(
        org_id=archived.org_id,
        flow_id=archived.id,
        plan_document=transformed.model_dump(mode="json"),
        plan_hash=plan_hash(transformed),
    )
    await session.commit()
    response = await controls[0].post(f"/orchestration/flows/{archived.id}/archive", json={"reason": "The draft was retired"})
    assert response.status_code == 200, response.text

    with pytest.raises(DraftFlowConflictError, match="archived.*slug remains reserved"):
        await register_draft_proposal(session, proposal, ApprovalContext(org_id=archived.org_id, actor_id="registrar", actor_role="member"))

    assert (
        await session.scalar(select(func.count()).select_from(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == archived.id))
    ) == 1
    decisions = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == archived.id)))
    assert [decision.kind for decision in decisions] == ["flow_archived"]
