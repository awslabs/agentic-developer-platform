"""Fresh gate approval validates Product access before granting any authority."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from src.orchestration.adapters.github_commands import CommandVerb, EngineCommand
from src.orchestration.adapters.github_comments import GateAnswerStatus, InputPath, apply_gate_answer_for_context
from src.orchestration.compile import ProposalRejectedError
from src.orchestration.engine_commands import _apply_command
from src.orchestration.models import DecisionKind, OrchestrationDecision, OrchestrationNode
from src.orchestration.registration import ACCEPTANCE_GATE_REF, register_draft_proposal
from src.products.models import Product
from src.shared.models.base import utcnow
from src.shared.models.organization import TeamMembership, User

from . import test_proposed_policy_activation as policy_fixtures
from . import test_registration as registration
from .product_fixture import seed_product
from .test_binding_acceptance_repairs import snapshot

session = registration.session
registrar = registration.registrar
access = registration.access
controls_app = policy_fixtures.controls_app


async def draft(session, registrar, product_case):
    await registration.seed_org(session)
    user_id = await registration.seed_principal(session, org_id=registration.ORG_A, role=registration.AdminRole.ORG_ADMIN.value)
    product_id = None if product_case == "missing" else "draft-product"
    if product_case not in {"missing", "unknown"}:
        await seed_product(
            session,
            org_id=registration.ORG_A if product_case != "other_tenant" else registration.ORG_B,
            actor_id=registration.HUMAN_USER_ID,
            user_id=user_id,
            product_id=product_id,
        )
        if product_case == "archived":
            (await session.get(Product, (registration.ORG_A, product_id))).archived_at = utcnow()
        if product_case == "other_owner":
            (await session.get(Product, (registration.ORG_A, product_id))).owner_user_id = "another-owner"
    result, _ = await register_draft_proposal(session, registration.gateless_proposal(product_id=product_id), registrar)
    gate = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.node_ref == ACCEPTANCE_GATE_REF))
    if product_case == "archived_after_draft":
        (await session.get(Product, (registration.ORG_A, product_id))).archived_at = utcnow()
    if product_case == "lost_team_access":
        product = await session.get(Product, (registration.ORG_A, product_id))
        # Revocation must take effect even for the owner with a stale team pointer.
        (await session.get(User, user_id)).team_id = product.team_id
        await session.execute(
            delete(TeamMembership).where(
                TeamMembership.user_id == user_id,
                TeamMembership.org_id == registration.ORG_A,
                TeamMembership.team_id == product.team_id,
            )
        )
    await session.commit()
    return result, gate.id


async def github_answer(session, access, gate_id, expected_hash, *, approve=True):
    return await apply_gate_answer_for_context(
        session,
        context=registration.token_context(registration.ORG_A, user_id=registration.HUMAN_USER_ID, account_type="human"),
        node_id=gate_id,
        approve=approve,
        reason="Reviewed draft",
        access=access,
        input_path=InputPath.GITHUB_COMMENT,
        expected_plan_hash=expected_hash,
    )


@pytest.mark.parametrize("product_case", ["unknown", "other_tenant", "archived"])
async def test_invalid_product_is_rejected_before_draft_registration(session, registrar, product_case):
    before = await snapshot(session)
    with pytest.raises(ProposalRejectedError, match="active Project belonging to this Product"):
        await draft(session, registrar, product_case)
    assert await snapshot(session) == before


@pytest.mark.parametrize("product_case", ["missing", "lost_team_access", "archived_after_draft"])
@pytest.mark.parametrize("ingress", ["dashboard", "github"])
@pytest.mark.parametrize("bound", [False, True])
async def test_invalid_product_cannot_approve_fresh_draft(session, registrar, access, controls_app, product_case, ingress, bound):
    result, gate_id = await draft(session, registrar, product_case)
    before = await snapshot(session)
    expected_hash = result.plan_hash if bound else None
    if ingress == "dashboard":
        async with AsyncClient(transport=ASGITransport(app=controls_app), base_url="http://test") as client:
            response = await client.post(f"/orchestration/gates/{gate_id}/approve", json={"expected_plan_hash": expected_hash})
        assert response.status_code == 422, response.text
        assert "Product" in response.text
    else:
        outcome = await github_answer(session, access, gate_id, expected_hash)
        assert outcome.status is GateAnswerStatus.REFUSED_PRODUCT
        assert "Product" in outcome.message
    assert await snapshot(session) == before


@pytest.mark.parametrize("product_case", ["accessible", "other_owner"])
async def test_accessible_product_approval_and_bound_retry(session, registrar, access, product_case):
    result, gate_id = await draft(session, registrar, product_case)
    outcome = await github_answer(session, access, gate_id, result.plan_hash)
    assert outcome.status is GateAnswerStatus.APPLIED
    await session.commit()
    (await session.get(Product, (registration.ORG_A, "draft-product"))).name = "Renamed Product"
    await session.commit()
    before = await snapshot(session)
    replay = await github_answer(session, access, gate_id, result.plan_hash)
    assert replay.status is GateAnswerStatus.IDEMPOTENT_REPLAY
    assert replay.decision_id == outcome.decision_id
    assert await snapshot(session) == before


async def test_github_command_reports_product_refusal_without_retrying(session, registrar, access):
    result, _ = await draft(session, registrar, "missing")
    before = await snapshot(session)
    applied, message = await _apply_command(
        session,
        command=EngineCommand(verb=CommandVerb.ACCEPT),
        org_id=registration.ORG_A,
        flow_id=result.flow_id,
        context=registration.token_context(registration.ORG_A, user_id=registration.HUMAN_USER_ID, account_type="human"),
        access=access,
    )
    assert applied is False
    assert "Product" in message
    assert await snapshot(session) == before


@pytest.mark.parametrize("legacy_approved", [False, True])
async def test_productless_rejection_and_legacy_approval_remain_valid(session, registrar, access, legacy_approved):
    result, gate_id = await draft(session, registrar, "missing")
    if legacy_approved:
        session.add(
            OrchestrationDecision(
                org_id=registration.ORG_A,
                flow_id=result.flow_id,
                kind=DecisionKind.PLAN_ACCEPTED.value,
                actor_id=registration.HUMAN_USER_ID,
                actor_kind="human",
                actor_role="org_admin",
            )
        )
        await session.commit()
    outcome = await github_answer(session, access, gate_id, result.plan_hash, approve=legacy_approved)
    assert outcome.status is GateAnswerStatus.APPLIED
