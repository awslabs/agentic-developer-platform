"""Repair Product-less drafts without granting execution or altering their plan."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.orchestration import draft_revision as revision
from src.orchestration.models import DecisionKind, OrchestrationFlow
from src.orchestration.proposal import LoopProposal
from src.orchestration.registration import register_draft_proposal
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.repository_evaluation_contract import harness_digest
from src.products.models import Product
from tests.orchestration import test_draft_revision as rev
from tests.orchestration import test_registration as reg
from tests.orchestration.product_fixture import seed_product

session = reg.session
registrar = reg.registrar
access = reg.access
autonomy_default_unset = reg.autonomy_default_unset
provider_repository_identity = reg.provider_repository_identity
client = rev.client
draft = rev.draft
operator = rev.operator
records = rev.records


def binding(original, product_id=None):
    return revision.BindDraftProductRequest(
        product_id=product_id or f"product-{reg.ORG_A}",
        expected_plan_version=original.plan_version,
        expected_plan_hash=original.plan_hash,
    )


async def test_product_binding_preserves_security_contract_and_replays(session, registrar, operator):
    raw = json.loads((Path(__file__).parents[4] / "docs/l5-services/flow/proposal.authored.json").read_text())
    raw["org_id"] = raw["proposed_execution_policy"]["org_id"] = reg.ORG_A
    # This test creates a fresh draft to exercise Product binding. Use the
    # installed verifier for that test draft; the authored example deliberately
    # retains its historical pin and must not change whenever transport code
    # changes. Binding below must still preserve this complete contract.
    for node in raw["nodes"]:
        if node.get("evaluation"):
            node["evaluation"]["runner"]["harness_sha256"] = harness_digest()
    await reg.seed_org(session)
    await seed_product(session, org_id=reg.ORG_A, actor_id=operator.actor_id)
    original, _ = await register_draft_proposal(session, LoopProposal.model_validate(raw), registrar)
    await session.commit()
    before = (await records(session, original.flow_id))[0][0].plan_document
    repo = OrchestrationRepository(session)
    nodes = await repo.list_nodes(org_id=reg.ORG_A, flow_id=original.flow_id)
    snapshot = {n.id: (n.state, n.attempts) for n in nodes}

    result = await revision.bind_draft_product(session, original.flow_id, binding(original), operator)
    assert result["plan_version"] == 2 and result["plan_hash"] != original.plan_hash
    assert not result["execution_authorized"]
    await session.commit()
    replay = await revision.bind_draft_product(session, original.flow_id, binding(original), operator)
    assert replay["already_revised"] and replay["plan_version"] == 2
    plans, decisions = await records(session, original.flow_id)
    after = plans[-1].plan_document
    assert {k: v for k, v in after.items() if k != "product_id"} == {k: v for k, v in before.items() if k != "product_id"}
    assert sum(bool(n.get("evaluation")) for n in after["nodes"]) == 4
    assert after["execution_policy"] is None
    assert all(d.kind == DecisionKind.PLAN_DRAFTED.value for d in decisions)
    assert len(plans) == len(decisions) == 2
    assert (await session.get(OrchestrationFlow, original.flow_id)).product_id == binding(original).product_id
    assert {n.id: (n.state, n.attempts) for n in await repo.list_nodes(org_id=reg.ORG_A, flow_id=original.flow_id)} == snapshot

    # Product repair cannot become a backdoor for changing a machine contract.
    altered = LoopProposal.model_validate(raw).model_copy(update={"title": "Other edit", "product_id": binding(original).product_id})
    with pytest.raises(revision.ProposalRejectedError, match="evaluation acceptance"):
        await revision.preview_draft_revision(
            session,
            original.flow_id,
            revision.DraftRevisionRequest(
                proposal=altered,
                expected_plan_version=2,
                expected_plan_hash=result["plan_hash"],
            ),
            operator,
        )


@pytest.mark.parametrize("case", ["unknown", "archived", "foreign_tenant", "foreign_owner"])
async def test_inaccessible_product_writes_nothing(session, draft, operator, case):
    if case != "unknown":
        await seed_product(session, org_id=reg.ORG_A, actor_id=operator.actor_id)
        product = await session.get(Product, (reg.ORG_A, binding(draft[0]).product_id))
        if case == "archived":
            product.archived_at = datetime.now(UTC)
        elif case == "foreign_tenant":
            product.tenant_id = "other-tenant"
        else:
            product.owner_user_id = "other-owner"
            product.team_id = "other-team"
        await session.flush()
    with pytest.raises(revision.ProposalRejectedError, match="unknown or inaccessible"):
        await revision.bind_draft_product(session, draft[0].flow_id, binding(draft[0]), operator)
    assert len((await records(session, draft[0].flow_id))[0]) == 1
    assert (await session.get(OrchestrationFlow, draft[0].flow_id)).product_id is None


async def test_binding_rollback_includes_product_projection(session, draft, operator, monkeypatch):
    await seed_product(session, org_id=reg.ORG_A, actor_id=operator.actor_id)
    await session.commit()

    async def fail(*args, **kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(OrchestrationRepository, "record_accepted_plan", fail)
    with pytest.raises(RuntimeError, match="injected"):
        await revision.bind_draft_product(session, draft[0].flow_id, binding(draft[0]), operator)
    assert (await session.get(OrchestrationFlow, draft[0].flow_id)).product_id is None
    assert len((await records(session, draft[0].flow_id))[0]) == 1


async def test_readiness_changes_without_approving_and_respects_legacy(session, draft, operator):
    before = await revision.preview_product_binding(session, draft[0].flow_id, operator)
    assert before["required"] and before["can_bind_draft"] and not before["ready"]
    assert "Select a Product" in before["problem"]
    await seed_product(session, org_id=reg.ORG_A, actor_id=operator.actor_id)
    await revision.bind_draft_product(session, draft[0].flow_id, binding(draft[0]), operator)
    after = await revision.preview_product_binding(session, draft[0].flow_id, operator)
    assert after["ready"] and after["can_bind_draft"] and after["product_id"] == binding(draft[0]).product_id
    assert (await reg.nodes_by_ref(session))["accept"].state == "awaiting_gate"


async def test_productless_legacy_approval_remains_ready(session, draft, operator):
    await OrchestrationRepository(session).append_decision(
        org_id=reg.ORG_A,
        flow_id=draft[0].flow_id,
        kind=DecisionKind.GATE_APPROVED.value,
        actor_id=operator.actor_id,
        actor_role=operator.actor_role,
        actor_kind="human",
    )
    status = await revision.preview_product_binding(session, draft[0].flow_id, operator)
    assert status["ready"] and not status["required"] and not status["can_bind_draft"]
    with pytest.raises(revision.DraftRevisionConflictError, match="approved"):
        await revision.bind_draft_product(session, draft[0].flow_id, binding(draft[0]), operator)


@pytest.mark.parametrize("case,status", [("valid", 200), ("stale", 409), ("unknown", 422), ("extra", 422), ("service", 403), ("foreign_flow", 404)])
async def test_product_binding_http_contract(client, session, draft, operator, case, status):
    from src.auth.dependencies import get_current_user

    http, app = client
    principal = f"user-{reg.HUMAN_USER_ID}"  # Created by the HTTP client fixture.
    await seed_product(session, org_id=reg.ORG_A, actor_id=operator.actor_id, user_id=principal)
    body = binding(draft[0]).model_dump(mode="json")
    if case == "stale":
        body["expected_plan_hash"] = "0" * 64
    elif case == "unknown":
        body["product_id"] = "unknown"
    elif case == "extra":
        body["proposal"] = {}
    elif case == "service":
        app.dependency_overrides[get_current_user] = lambda: reg.token_context(reg.ORG_A, user_id=operator.actor_id, account_type="service")
    elif case == "foreign_flow":
        (await session.get(OrchestrationFlow, draft[0].flow_id)).org_id = "other"
        await session.flush()
    response = await http.post(f"/orchestration/flows/{draft[0].flow_id}/draft/product", json=body)
    assert response.status_code == status, response.text
    if case == "valid":
        assert response.json()["plan_version"] == 2 and not response.json()["execution_authorized"]
        assert (await session.get(OrchestrationFlow, draft[0].flow_id)).product_id == body["product_id"]


async def test_gate_preview_exposes_product_blocker_without_evaluation(client, session, draft, operator, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src.orchestration.controls import get_access_control as controls_access
    from src.orchestration.controls import router as controls_router

    http, app = client
    app.include_router(controls_router)
    app.dependency_overrides[controls_access] = lambda: reg.AccessControl(session)
    monkeypatch.setattr(
        "src.agentauth.human_control.authorize_human_session",
        AsyncMock(
            return_value=SimpleNamespace(tenant_id=reg.ORG_A, user_id=operator.actor_id),
        ),
    )
    monkeypatch.setattr(
        "src.orchestration.gate_execution.prepare_gate_execution",
        AsyncMock(
            return_value={"required": False, "ready": True, "problems": [], "runs": [], "grants": ["private"]},
        ),
    )
    gate = (await reg.nodes_by_ref(session))["accept"]
    result = await http.get(f"/orchestration/gates/{gate.id}/execution-preview")
    assert result.status_code == 200, result.text
    preview = result.json()
    assert not preview["required"] and not preview["ready"]
    assert preview["product"]["can_bind_draft"] and preview["product"]["product_id"] is None
    assert "Select a Product" in preview["problems"][0]
    assert "grants" not in preview


async def test_teamless_product_draft_uses_current_admin_authority_without_starting(session, draft, operator, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    await seed_product(session, org_id=reg.ORG_A, actor_id=operator.actor_id)
    product = await session.get(Product, (reg.ORG_A, binding(draft[0]).product_id))
    product.team_id = None
    await session.flush()
    authority = AsyncMock(return_value=SimpleNamespace(is_admin=True))
    monkeypatch.setattr("src.shared.identity.product_authority.stored_product_actor", authority)

    result = await revision.bind_draft_product(session, draft[0].flow_id, binding(draft[0]), operator)
    assert not result["execution_authorized"]
    preview = await revision.preview_product_binding(session, draft[0].flow_id, operator)
    assert preview["ready"] and preview["can_bind_draft"]
    authority.assert_awaited_with(session, user_id=operator.actor_id, tenant_id=reg.ORG_A)
    assert (await reg.nodes_by_ref(session))["accept"].state == "awaiting_gate"

    # Revocation takes effect on the next read; neither ownership nor an old
    # approval/display role can keep the Product accessible.
    authority.return_value = SimpleNamespace(is_admin=False)
    preview = await revision.preview_product_binding(session, draft[0].flow_id, operator)
    assert not preview["ready"] and preview["problem"] == "Product is unknown or inaccessible"
    assert (await reg.nodes_by_ref(session))["accept"].state == "awaiting_gate"


@pytest.mark.parametrize("case", ["unknown", "archived", "foreign_tenant", "revoked_admin", "service"])
async def test_product_admin_exception_retains_scope_and_revocation(session, draft, operator, monkeypatch, case):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src.orchestration.compile import require_accessible_product
    from src.orchestration.state import ActorKind

    await seed_product(session, org_id=reg.ORG_A, actor_id=operator.actor_id)
    product = await session.get(Product, (reg.ORG_A, binding(draft[0]).product_id))
    product.team_id = None
    product_id = product.id
    if case == "unknown":
        product_id = "unknown"
    elif case == "archived":
        product.archived_at = datetime.now(UTC)
    elif case == "foreign_tenant":
        product.tenant_id = "other-tenant"
    await session.flush()
    authority = AsyncMock(return_value=SimpleNamespace(is_admin=case != "revoked_admin"))
    monkeypatch.setattr("src.shared.identity.product_authority.stored_product_actor", authority)
    decision = replace(operator, actor_role="platform_admin", actor_kind=ActorKind.SERVICE if case == "service" else ActorKind.HUMAN)
    with pytest.raises(revision.ProposalRejectedError, match="unknown or inaccessible"):
        await require_accessible_product(session, product_id=product_id, decision=decision)
    if case == "service":
        authority.assert_not_awaited()


async def test_product_admin_outage_does_not_become_a_permanent_access_denial(session, draft, operator, monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from src.orchestration.compile import require_accessible_product

    await seed_product(session, org_id=reg.ORG_A, actor_id=operator.actor_id)
    product = await session.get(Product, (reg.ORG_A, binding(draft[0]).product_id))
    product.team_id = None
    await session.flush()
    monkeypatch.setattr(
        "src.shared.identity.product_authority.stored_product_actor",
        AsyncMock(side_effect=HTTPException(503, "Authority unavailable")),
    )
    with pytest.raises(HTTPException) as error:
        await require_accessible_product(session, product_id=product.id, decision=operator)
    assert error.value.status_code == 503


async def test_product_team_access_does_not_require_an_identity_provider_lookup(session, draft, operator, monkeypatch):
    from unittest.mock import AsyncMock

    from src.orchestration.compile import require_accessible_product

    await seed_product(session, org_id=reg.ORG_A, actor_id=operator.actor_id)
    authority = AsyncMock(side_effect=AssertionError("Team access must remain independent of administrator lookup"))
    monkeypatch.setattr("src.shared.identity.product_authority.stored_product_actor", authority)
    await require_accessible_product(session, product_id=binding(draft[0]).product_id, decision=operator)
    authority.assert_not_awaited()
