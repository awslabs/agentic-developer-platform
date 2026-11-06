"""HTTP regressions for acceptance and migrated binding protections."""

import json

import pytest
from sqlalchemy import select

from src.orchestration.amendment_impact import assess_amendment
from src.orchestration.compile import ApprovalContext, compile_proposal
from src.orchestration.models import (
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from src.orchestration.proposal import ProposedEdge, ProposedNode
from src.products.models import Product
from src.shared.models.organization import User
from tests.orchestration.product_fixture import seed_product_membership

from . import test_flow_create as flow_fixtures
from .legacy_plan import persist_legacy_plan
from .test_flow_create import ORG_A, ROUTE, USER_ID, address, client_for, routable_proposal, seed_org

session = flow_fixtures.session
app_with_router = flow_fixtures.app_with_router

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def products(session, monkeypatch):
    from types import SimpleNamespace

    from src.admin.installations.resolver import OwnerState
    from src.orchestration import repository_binding
    from src.shared.models.vault import ChannelTenantMap

    await seed_org(session, ORG_A, installation_ids=["12345"])
    session.add(
        ChannelTenantMap(provider="github", provider_scope_id="111", org_id=ORG_A, installation_id="12345", install_metadata={"account_id": "111"})
    )

    async def owner(installation_id, *, db):
        assert installation_id == 12345
        return SimpleNamespace(tenant_id=ORG_A, github_account_id="111"), OwnerState.RESOLVED

    async def read(_tenant, installation_id, name, repository_id, issue):
        expected = {"example/repo-a": 1001, "example/repo-b": 1002, "example/repo-c": 1003}.get(name)
        if installation_id != 12345 or expected != repository_id:
            return None
        return {"id": expected, "full_name": name}, {"account": {"id": 111}}, {"id": issue + 10000, "node_id": f"I_{issue}"}

    monkeypatch.setattr(repository_binding, "resolve_installation_owner", owner)
    monkeypatch.setattr(repository_binding, "_provider_read", read)
    await session.merge(User(id=USER_ID, org_id=ORG_A, team_id="team", email="owner@example.test"))
    for product_id in ("product-a", "product-b"):
        await seed_product_membership(session, org_id=ORG_A, user_id=USER_ID)
        session.add(
            Product(
                team_id=f"product-team-{ORG_A}",
                id=product_id,
                tenant_id=ORG_A,
                name=product_id,
                purpose="Test",
                intended_users=["team"],
                owner_user_id=USER_ID,
                starting_point="running_product",
                created_by=USER_ID,
            )
        )
    await session.commit()


def repository_proposal(**overrides):
    proposal = routable_proposal(**overrides)
    proposal.nodes[0].repository_identity = "example/repo-a"
    proposal.nodes[0].provider_repository_id = 1001
    return proposal


def approval():
    return ApprovalContext(org_id=ORG_A, actor_id=USER_ID, actor_role="org_admin")


async def snapshot(session):
    session.expire_all()
    return {
        model.__name__: [
            {column.name: getattr(row, column.name) for column in model.__table__.columns}
            for row in (await session.scalars(select(model).order_by(model.id))).all()
        ]
        for model in (
            OrchestrationFlow,
            OrchestrationNode,
            OrchestrationEdge,
            OrchestrationAcceptedPlan,
            OrchestrationDecision,
            OrchestrationExecution,
            OrchestrationWorkClaim,
            OrchestrationPullRequestBinding,
        )
    }


async def test_create_cannot_retarget_reserved_repository(session, app_with_router, products):
    client = client_for(app_with_router, permitted=True)
    proposal = repository_proposal(product_id="product-a")
    accepted = client.post(ROUTE, json=proposal.model_dump(mode="json"))
    assert accepted.status_code == 201, accepted.text
    node = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.node_ref == "story-a"))
    session.add(
        OrchestrationExecution(
            org_id=ORG_A,
            flow_id=accepted.json()["flow_id"],
            node_id=node.id,
            phase="queued",
            status="queued",
            claim_id="reserved",
            claim_generation=1,
        )
    )
    await session.commit()
    before = await snapshot(session)
    retried = client.post(ROUTE, json=proposal.model_dump(mode="json"))
    assert retried.status_code == 200, retried.text
    assert await snapshot(session) == before
    proposal.nodes[0].repository_identity = "example/repo-b"
    proposal.nodes[0].provider_repository_id = 1002
    response = client.post(ROUTE, json=proposal.model_dump(mode="json"))
    assert response.status_code == 422, response.text
    assert await snapshot(session) == before


async def test_new_execution_requires_product_even_without_repository_fields(session, app_with_router, products, monkeypatch):
    monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", "example/repo-a")
    client = client_for(app_with_router, permitted=True)
    before = await snapshot(session)
    response = client.post(ROUTE, json=routable_proposal().model_dump(mode="json"))
    assert response.status_code == 422, response.text
    assert "Product" in response.text
    assert await snapshot(session) == before


@pytest.mark.parametrize("retarget", ["product", "repository", "both", "unmatched_claim"])
@pytest.mark.parametrize("ingress", ["create", "amend"])
async def test_migrated_claim_freezes_binding(session, app_with_router, products, retarget, ingress):
    proposal = repository_proposal()
    accepted = await persist_legacy_plan(session, proposal, approval())
    session.add(
        OrchestrationWorkClaim(
            org_id=ORG_A,
            provider_repository_id=1001,
            issue_number=9999 if retarget == "unmatched_claim" else 4320,
            owner_kind="engine_flow",
            owner_ref=accepted.flow_id,
        )
    )
    await session.commit()
    before = await snapshot(session)
    if retarget != "repository":
        proposal.product_id = "product-a"
    if retarget in {"repository", "both"}:
        proposal.nodes[0].repository_identity = "example/repo-b"
        proposal.nodes[0].provider_repository_id = 1002
    preview = await assess_amendment(session, org_id=ORG_A, flow_id=accepted.flow_id, proposal=proposal, actor_id=USER_ID)
    expected = "amendment_definition_change" if retarget == "repository" else "product_retarget_started"
    assert expected in {item.rule for item in preview.blocking}
    client = client_for(app_with_router, permitted=True)
    route = ROUTE if ingress == "create" else f"{ROUTE}/{accepted.flow_id}/amendments"
    response = client.post(route, json=proposal.model_dump(mode="json"))
    assert response.status_code == 422, response.text
    assert await snapshot(session) == before


@pytest.mark.parametrize("ingress", ["create", "amend"])
async def test_claim_freezes_legacy_story_without_repository_id(session, app_with_router, products, ingress):
    proposal = routable_proposal()
    accepted = await persist_legacy_plan(session, proposal, approval())
    session.add(
        OrchestrationWorkClaim(
            org_id=ORG_A,
            provider_repository_id=1001,
            issue_number=4320,
            owner_kind="engine_flow",
            owner_ref=accepted.flow_id,
        )
    )
    await session.commit()
    before = await snapshot(session)
    proposal.nodes[0].repository_identity = "example/repo-b"
    proposal.nodes[0].provider_repository_id = 1002
    preview = await assess_amendment(session, org_id=ORG_A, flow_id=accepted.flow_id, proposal=proposal, actor_id=USER_ID)
    client = client_for(app_with_router, permitted=True)
    route = ROUTE if ingress == "create" else f"{ROUTE}/{accepted.flow_id}/amendments"
    response = client.post(route, json=proposal.model_dump(mode="json"))
    assert response.status_code == 422, response.text
    assert await snapshot(session) == before
    assert "amendment_definition_change" in {item.rule for item in preview.blocking}


@pytest.mark.parametrize("unrelated", ["tenant", "flow", "issue", "known_repository"])
async def test_unrelated_claim_does_not_freeze_story_binding(session, app_with_router, products, unrelated):
    proposal = repository_proposal() if unrelated == "known_repository" else routable_proposal()
    accepted = await persist_legacy_plan(session, proposal, approval())
    session.add(
        OrchestrationWorkClaim(
            org_id="another-tenant" if unrelated == "tenant" else ORG_A,
            provider_repository_id=1002,
            issue_number=9999 if unrelated == "issue" else 4320,
            owner_kind="engine_flow",
            owner_ref="another-flow" if unrelated == "flow" else accepted.flow_id,
        )
    )
    await session.commit()
    proposal.nodes[0].repository_identity = "example/repo-c"
    proposal.nodes[0].provider_repository_id = 1003
    response = client_for(app_with_router, permitted=True).post(f"{ROUTE}/{accepted.flow_id}/amendments", json=proposal.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    node = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.node_ref == "story-a"))
    assert node.provider_repository_id == 1003


async def test_identical_repository_bound_legacy_retry_preserves_acceptance(session, app_with_router, products):
    proposal = repository_proposal()
    accepted = await persist_legacy_plan(session, proposal, approval())
    assert accepted.plan_hash == "04d84d0844259379c20e6dcfaebe5e9bdad2c043a88c9469c50982b2ac1f1f4b"
    await session.commit()
    before = await snapshot(session)
    original_bytes = json.dumps(before["OrchestrationAcceptedPlan"][0]["plan_document"], sort_keys=True).encode()
    client = client_for(app_with_router, permitted=True)
    response = client.post(ROUTE, json=proposal.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    assert response.json()["decision_id"] == accepted.decision_id
    assert response.json()["plan_hash"] == accepted.plan_hash
    after = await snapshot(session)
    assert after == before
    assert json.dumps(after["OrchestrationAcceptedPlan"][0]["plan_document"], sort_keys=True).encode() == original_bytes


async def test_draft_remains_incomplete_but_promotion_requires_product(session, app_with_router, products):
    proposal = routable_proposal()
    drafted = await compile_proposal(session, proposal, approval(), decision_kind=DecisionKind.PLAN_DRAFTED)
    await session.commit()
    before = await snapshot(session)
    client = client_for(app_with_router, permitted=True)
    proposal.title = "Accepted delivery loop"
    response = client.post(ROUTE, json=proposal.model_dump(mode="json"))
    assert response.status_code == 422, response.text
    assert await snapshot(session) == before
    proposal.product_id = "product-a"
    response = client.post(ROUTE, json=proposal.model_dump(mode="json"))
    assert response.status_code == 201, response.text
    assert response.json()["flow_id"] == drafted.flow_id


async def test_passed_preflight_gate_does_not_freeze_product(session, app_with_router, products):
    proposal = repository_proposal(product_id="product-a")
    proposal.nodes.append(ProposedNode(address=address("story-b"), kind="story", title="Second story", issue_ref="4322"))
    proposal.edges = [
        ProposedEdge(from_address=address("gate"), to_address=address("story-b")),
        ProposedEdge(from_address=address("story-b"), to_address=address("eval")),
        ProposedEdge(from_address=address("gate"), to_address=address("story-a")),
        ProposedEdge(from_address=address("story-a"), to_address=address("eval")),
    ]
    client = client_for(app_with_router, permitted=True)
    accepted = client.post(ROUTE, json=proposal.model_dump(mode="json"))
    assert accepted.status_code == 201, accepted.text
    gate = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.kind == "gate"))
    gate.state = "passed"
    await session.commit()
    proposal.product_id = "product-b"
    preview = await assess_amendment(session, org_id=ORG_A, flow_id=accepted.json()["flow_id"], proposal=proposal, actor_id=USER_ID)
    assert "product_retarget_started" not in {item.rule for item in preview.blocking}
    response = client.post(f"{ROUTE}/{accepted.json()['flow_id']}/amendments", json=proposal.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    assert (await session.get(OrchestrationFlow, accepted.json()["flow_id"])).product_id == "product-b"

    before = await snapshot(session)
    next(node for node in proposal.nodes if node.kind == "gate").issue_ref = "4323"
    response = client.post(f"{ROUTE}/{accepted.json()['flow_id']}/amendments", json=proposal.model_dump(mode="json"))
    assert response.status_code == 422, response.text
    assert await snapshot(session) == before


async def test_creation_projection_mismatch_refuses_without_mutation(session, app_with_router, products):
    proposal = repository_proposal(product_id="product-a")
    client = client_for(app_with_router, permitted=True)
    accepted = client.post(ROUTE, json=proposal.model_dump(mode="json"))
    assert accepted.status_code == 201, accepted.text
    node = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.node_ref == "story-a"))
    node.provider_repository_id = 1002
    await session.commit()
    before = await snapshot(session)
    proposal.title = "Updated title"
    response = client.post(ROUTE, json=proposal.model_dump(mode="json"))
    assert response.status_code == 422, response.text
    assert "projection mismatch" in response.text
    assert await snapshot(session) == before


@pytest.mark.parametrize("ingress", ["create", "amend"])
@pytest.mark.parametrize("state,attempts", [("pending", 0), ("failed", 5), ("passed", 1)])
async def test_legacy_missing_repository_projection_is_restored_without_resetting_work(session, app_with_router, products, ingress, state, attempts):
    proposal = repository_proposal()
    accepted = await persist_legacy_plan(session, proposal, approval())
    node = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.node_ref == "story-a"))
    node.repository_identity = node.provider_repository_id = None
    node.state, node.attempts = state, attempts
    node_id = node.id
    await session.commit()
    before = await snapshot(session)
    proposal.title = "Continue the accepted legacy work"
    impact = await assess_amendment(session, org_id=ORG_A, flow_id=accepted.flow_id, proposal=proposal, actor_id=USER_ID)
    assert not impact.blocking
    assert await snapshot(session) == before  # Preview never repairs persisted state.
    client = client_for(app_with_router, permitted=True)
    route = ROUTE if ingress == "create" else f"{ROUTE}/{accepted.flow_id}/amendments"
    response = client.post(route, json=proposal.model_dump(mode="json"))
    assert response.status_code in (200, 201), response.text
    after = await snapshot(session)
    restored = next(row for row in after["OrchestrationNode"] if row["id"] == node_id)
    assert (restored["repository_identity"], restored["provider_repository_id"]) == ("example/repo-a", 1001)
    assert (restored["state"], restored["attempts"]) == (state, attempts)
    assert len(after["OrchestrationNode"]) == len(before["OrchestrationNode"])
    assert after["OrchestrationExecution"] == before["OrchestrationExecution"]
    previous = next(row for row in after["OrchestrationAcceptedPlan"] if row["version"] == 1)
    assert previous["plan_document"] == before["OrchestrationAcceptedPlan"][0]["plan_document"]


@pytest.mark.parametrize("ingress", ["create", "amend"])
@pytest.mark.parametrize("case", ["modern_missing", "legacy_partial", "legacy_conflicting", "legacy_retarget"])
async def test_repository_projection_recovery_does_not_allow_identity_changes(session, app_with_router, products, ingress, case):
    proposal = repository_proposal(product_id="product-a") if case == "modern_missing" else repository_proposal()
    accepted = (
        await compile_proposal(session, proposal, approval())
        if case == "modern_missing"
        else await persist_legacy_plan(session, proposal, approval())
    )
    node = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.node_ref == "story-a"))
    node.repository_identity = None
    node.provider_repository_id = 1002 if case == "legacy_conflicting" else (1001 if case == "legacy_partial" else None)
    node.state, node.attempts = "failed", 5
    await session.commit()
    before = await snapshot(session)
    proposal.title = "A real amendment"
    if case == "legacy_retarget":
        proposal.nodes[0].repository_identity = "example/repo-b"
        proposal.nodes[0].provider_repository_id = 1002
    impact = await assess_amendment(session, org_id=ORG_A, flow_id=accepted.flow_id, proposal=proposal, actor_id=USER_ID)
    expected = "amendment_definition_change" if case == "legacy_retarget" else "accepted_identity_projection_mismatch"
    assert expected in {item.rule for item in impact.blocking}
    client = client_for(app_with_router, permitted=True)
    route = ROUTE if ingress == "create" else f"{ROUTE}/{accepted.flow_id}/amendments"
    response = client.post(route, json=proposal.model_dump(mode="json"))
    assert response.status_code == 422, response.text
    assert await snapshot(session) == before


async def test_gate_accepted_legacy_history_remains_amendable(session, app_with_router, products):
    proposal = repository_proposal()
    accepted = await compile_proposal(session, proposal, approval(), decision_kind=DecisionKind.PLAN_DRAFTED)
    session.add(
        OrchestrationDecision(
            org_id=ORG_A,
            flow_id=accepted.flow_id,
            kind=DecisionKind.GATE_APPROVED.value,
            actor_id=USER_ID,
            actor_kind="human",
            actor_role="org_admin",
        )
    )
    await session.commit()
    proposal.title = "Updated legacy flow"
    client = client_for(app_with_router, permitted=True)
    response = client.post(f"{ROUTE}/{accepted.flow_id}/amendments", json=proposal.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    assert response.json()["plan_version"] == 2
    assert (await session.get(OrchestrationFlow, accepted.flow_id)).product_id is None
