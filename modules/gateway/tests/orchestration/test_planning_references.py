import pytest
from sqlalchemy import select

from src.orchestration import repository_binding
from src.orchestration.compile import ProposalRejectedError, compile_proposal, plan_hash
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationFlow, OrchestrationNode
from src.orchestration.repository import OrchestrationRepository
from tests.orchestration import test_compile as fixtures
from tests.orchestration.product_fixture import seed_product

session = fixtures.session
approval = fixtures.approval


def _proposal():
    reference = dict(provider="github", repository_id=3003, repository="example/planning", installation_id=303, issue_number=42)
    return fixtures.valid_proposal(
        product_id="product-planning", origin_ref=reference, tracker_refs=[dict(scope="epic", address=f"{fixtures.FLOW}/epic-1", issue=reference)]
    )


def test_planning_reference_changes_hash_without_rehashing_old_documents():
    legacy = fixtures.valid_proposal(product_id=None)
    assert plan_hash(legacy) == "445deca4c2b5f2db7954752501c3e5779d5fa2c36b29554fc2e705c2a7f5c587"
    assert "origin_ref" not in legacy.model_dump(mode="json")
    assert "tracker_refs" not in legacy.model_dump(mode="json")
    assert plan_hash(_proposal()) != plan_hash(fixtures.valid_proposal(product_id="product-planning"))


@pytest.mark.asyncio
async def test_verified_planning_reference_projects_without_moving_story(session, approval, monkeypatch):
    await seed_product(session, org_id=fixtures.ORG_A, actor_id=approval.actor_id, product_id="product-planning")
    calls = []

    async def verified(_session, **kwargs):
        calls.append(kwargs)
        return repository_binding.VerifiedRepository(
            "github", kwargs["repository_id"], kwargs["name"], kwargs["installation_id"], kwargs["name"], kwargs["issue"], kwargs["operation"]
        )

    monkeypatch.setattr(repository_binding, "resolve_story_repository", verified)
    result = await compile_proposal(session, _proposal(), approval)
    flow = await session.get(OrchestrationFlow, result.flow_id)
    plan = await OrchestrationRepository(session).get_accepted_plan(org_id=fixtures.ORG_A, flow_id=result.flow_id)
    assert flow.product_id == "product-planning"
    assert flow.origin_ref == plan.plan_document["origin_ref"]
    assert flow.tracker_refs == plan.plan_document["tracker_refs"]
    assert len(calls) == 2
    assert all(call["name"] == "example/planning" and call["installation_id"] == 303 for call in calls)
    assert all("permitted_names" not in call for call in calls)
    stories = (await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == flow.id))).all()
    assert all(node.repository_identity is None for node in stories)


@pytest.mark.asyncio
async def test_wrong_planning_repository_refuses_before_mutation(session, approval, monkeypatch):
    await seed_product(session, org_id=fixtures.ORG_A, actor_id=approval.actor_id, product_id="product-planning")

    async def wrong(_session, **kwargs):
        return repository_binding.VerifiedRepository("github", 9999, kwargs["name"], 303, kwargs["name"], 42, "accept")

    monkeypatch.setattr(repository_binding, "resolve_story_repository", wrong)
    with pytest.raises(ProposalRejectedError):
        await compile_proposal(session, _proposal(), approval)
    assert (await session.scalars(select(OrchestrationAcceptedPlan))).all() == []
    assert (await session.scalars(select(OrchestrationFlow))).all() == []
