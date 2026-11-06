"""A login-subject spelling change preserves receipts, never other authority."""

import copy
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationNode
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User

from . import test_amendment_review_routes as routes
from .test_amendment_review_routes import (
    HUMAN_USER_ID,
    client_for,
    count_rows,
    draft_route,
    exact_accept_body,
    insert_after_body,
    policied_flow_with_draft,
    proposals_route,
)
from .test_pending_amendments import ORG_A, ORG_B, address

session = routes.session
app_with_router = routes.app_with_router


async def owner(session, **changes):
    values = dict(id=HUMAN_USER_ID, org_id=ORG_A, cognito_sub="cognito-sub-owner", team_id="team", email="owner@example.test")
    values.update(changes)
    user = User(**values)
    session.add(user)
    await session.commit()
    return user


async def test_placement_preview_accept_preserves_canonical_owner_budget_and_history(session, app_with_router):
    from src.orchestration.flow_meter import meter_target
    from src.orchestration.policy_admission import load_in_force_policy
    from src.orchestration.repository import OrchestrationRepository

    await owner(session)
    flow_id, _ = await policied_flow_with_draft(session, spend="500.00")
    base = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow_id))
    data = {
        "contract": "shared-budget-increase/v1",
        "flow_id": flow_id,
        "plan_version": base.version,
        "plan_hash": base.plan_hash,
        "original_policy_hash": base.plan_document["execution_policy"]["policy_hash"],
        "principal_id": "cognito-sub-owner",
        "limits": {"max_spend_usd": "500.00", "max_run_spend_usd": "100.00", "max_chain_spend_usd": "500.00"},
    }
    receipt = await OrchestrationRepository(session).append_decision(
        org_id=ORG_A,
        flow_id=flow_id,
        kind="budget_increased",
        actor_id=HUMAN_USER_ID,
        actor_role="platform_admin",
        actor_kind="human",
        reason=json.dumps(data),
    )
    story = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.flow_id == flow_id, OrchestrationNode.node_ref == "story-a"))
    story.state, story.attempts = "passed", 1
    await session.commit()
    base_document, receipt_reason = copy.deepcopy(base.plan_document), receipt.reason
    before_nodes = {
        node.id: (node.state, node.attempts) for node in await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == flow_id))
    }
    before = await load_in_force_policy(session, org_id=ORG_A, flow_id=flow_id)
    meter_before = meter_target(org_id=ORG_A, flow_id=flow_id, policy=before.policy).key()
    client = client_for(app_with_router)
    request = insert_after_body(address("story-a"))
    request["placement"]["work"][0]["kind"] = "gate"

    response = client.post(proposals_route(flow_id), json=request)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["impact"]["receipts_dropped"] == []
    assert created["impact"]["receipts_preserved"] == [
        {"kind": "budget", "decision_id": receipt.id, "before": data["limits"], "after": data["limits"], "reason": "authority_unchanged"}
    ]
    draft = client.get(draft_route(flow_id, created["draft_id"])).json()
    policy = draft["proposal_document"]["execution_policy"]
    assert all(policy.get(field) is None for field in ("policy_id", "policy_hash", "principal_id"))
    preview = client.post(draft_route(flow_id, created["draft_id"], "preview")).json()
    assert preview["acceptable"], preview["blocking"]
    assert preview["effective_limits_before"] == preview["effective_limits_after"]

    response = client.post(draft_route(flow_id, created["draft_id"], "accept"), json=exact_accept_body(client, flow_id, created["draft_id"]))
    assert response.status_code == 200, response.text
    assert response.json()["receipts_preserved"] == preview["receipts_preserved"]
    assert response.json()["receipts_dropped"] == []
    after = await load_in_force_policy(session, org_id=ORG_A, flow_id=flow_id)
    assert after.refusal is None
    assert after.policy.principal_id == HUMAN_USER_ID
    assert after.policy.limits.max_spend_usd == after.policy._shared_chain_spend_usd == 500
    assert after.policy._shared_run_spend_usd == 100
    assert after.policy.limits.max_concurrent_actions == 2
    assert after.policy.limits.max_attempts_per_node == 3
    assert after.policy._shared_budget_decision_id == receipt.id
    assert meter_target(org_id=ORG_A, flow_id=flow_id, policy=after.policy).key() == meter_before
    assert base.plan_document == base_document and receipt.reason == receipt_reason
    assert len(list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "budget_increased")))) == 1
    for node in await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == flow_id)):
        if node.id in before_nodes:
            assert (node.state, node.attempts) == before_nodes[node.id]
    # A later loss of authoritative alias proof cannot keep carrying this receipt.
    person = await session.get(User, HUMAN_USER_ID)
    person.cognito_sub = "replacement-login-subject"
    await session.commit()
    revoked = await load_in_force_policy(session, org_id=ORG_A, flow_id=flow_id)
    assert revoked.policy._shared_budget_decision_id is None
    assert meter_target(org_id=ORG_A, flow_id=flow_id, policy=revoked.policy).key() == meter_before


async def test_existing_draft_preview_preserves_budget_for_canonical_owner(session, app_with_router):
    await owner(session)
    flow_id, draft = await policied_flow_with_draft(session, spend="500.00", receipt_spend="500.00")
    client = client_for(app_with_router)
    response = client.post(draft_route(flow_id, draft.draft_id, "preview"))
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["receipts_dropped"] == []
    assert [receipt["kind"] for receipt in preview["receipts_preserved"]] == ["budget"]


@pytest.mark.parametrize("change", ["other-human", "foreign-tenant", "revoked", "shadow", "bot", "missing", "unverifiable"])
async def test_canonical_alias_needs_live_same_human_in_same_tenant(session, monkeypatch, change):
    from src.orchestration.policy_identity import same_human_principal
    from src.shared.identity import workspaces

    user = await owner(session, **({"org_id": ORG_B} if change == "foreign-tenant" else {}))
    current = HUMAN_USER_ID
    if change == "other-human":
        session.add(User(id="other-admin", org_id=ORG_A, cognito_sub="other-login", team_id="team", email=user.email))
        current = "other-admin"
    elif change == "revoked":
        session.add(TenantMembership(user_id=user.id, tenant_id=ORG_A, revoked_at=datetime.now(UTC)))
    elif change == "shadow":
        user.is_shadow = True
    elif change == "bot":
        user.user_kind = "bot"
    elif change == "missing":
        user.cognito_sub = None
    elif change == "unverifiable":
        monkeypatch.setattr(workspaces, "workspace_user", AsyncMock(side_effect=SQLAlchemyError("unavailable")))
    await session.commit()
    assert not await same_human_principal(session, org_id=ORG_A, recorded_id="cognito-sub-owner", current_id=current)


async def test_canonical_alias_does_not_preserve_other_policy_changes(session):
    from src.orchestration.compile import ApprovalContext, DecisionKind, prepare_execution_policy
    from src.orchestration.execution_policy import Action
    from src.orchestration.plan_lineage import resolved_receipt_authority_change

    from .test_amendment_review_routes import permitted_policy
    from .test_pending_amendments import base_proposal

    await owner(session)
    raw = base_proposal(execution_policy=permitted_policy())
    original = prepare_execution_policy(
        raw, decision=ApprovalContext(org_id=ORG_A, actor_id="cognito-sub-owner", actor_role="org_admin"), decision_kind=DecisionKind.PLAN_AMENDED
    )
    changed = prepare_execution_policy(
        raw, decision=ApprovalContext(org_id=ORG_A, actor_id=HUMAN_USER_ID, actor_role="org_admin"), decision_kind=DecisionKind.PLAN_AMENDED
    )
    assert await resolved_receipt_authority_change(session, original.model_dump(mode="json"), changed.model_dump(mode="json")) is None
    raw = raw.model_copy(deep=True)
    raw.execution_policy.allowed_actions.append(Action.REVIEW)
    changed = prepare_execution_policy(
        raw, decision=ApprovalContext(org_id=ORG_A, actor_id=HUMAN_USER_ID, actor_role="org_admin"), decision_kind=DecisionKind.PLAN_AMENDED
    )
    assert (
        await resolved_receipt_authority_change(session, original.model_dump(mode="json"), changed.model_dump(mode="json"))
        == "policy_authority_changed"
    )


@pytest.mark.parametrize("values", [{"is_shadow": True}, {"user_kind": "bot"}])
async def test_alias_rechecks_account_classification_after_prior_read(session, values):
    from src.orchestration.policy_identity import same_human_principal

    person = await owner(session)
    await session.execute(update(User).where(User.id == person.id).values(**values).execution_options(synchronize_session=False))
    assert person.user_kind == "human" and not person.is_shadow
    assert not await same_human_principal(session, org_id=ORG_A, recorded_id="cognito-sub-owner", current_id=person.id)


@pytest.mark.parametrize("changed", ["membership", "login-subject", "identity-link"])
async def test_org_placement_alias_rechecks_cached_fallback_authority(session, changed):
    from src.orchestration.policy_identity import same_human_principal
    from src.shared.models.vault import UserIdentity

    person = await owner(session, cognito_sub=None)
    login = User(id="login-row", org_id=ORG_B, cognito_sub="original-login", team_id="team", email=person.email)
    membership = TenantMembership(user_id=login.id, tenant_id=ORG_A)
    link = UserIdentity(
        user_id=person.id, org_id=ORG_A, team_id="team", provider="cognito", provider_user_id="original-login", verification_method="org_placement"
    )
    session.add_all([login, membership, link, TenantMembership(user_id=person.id, tenant_id=ORG_A)])
    await session.commit()
    assert await same_human_principal(session, org_id=ORG_A, recorded_id=login.id, current_id=person.id)
    if changed == "membership":
        statement = update(TenantMembership).where(TenantMembership.id == membership.id).values(revoked_at=datetime.now(UTC))
    elif changed == "login-subject":
        statement = update(User).where(User.id == login.id).values(cognito_sub="replacement-login")
    else:
        statement = update(UserIdentity).where(UserIdentity.id == link.id).values(verification_method="manual")
    await session.execute(statement.execution_options(synchronize_session=False))
    assert membership.revoked_at is None and login.cognito_sub == "original-login" and link.verification_method == "org_placement"
    assert not await same_human_principal(session, org_id=ORG_A, recorded_id=login.id, current_id=person.id)


@pytest.mark.parametrize("field", ["policy_id", "policy_hash", "principal_id"])
async def test_client_stamp_is_still_refused(session, field):
    from src.orchestration.amend import AmendmentContext, amend_plan
    from src.orchestration.compile import PolicyNotAcceptableError

    from .test_amendment_review_routes import permitted_policy
    from .test_pending_amendments import amended_proposal

    flow_id, _ = await policied_flow_with_draft(session)
    policy = permitted_policy().model_copy(update={field: "forged"})
    with pytest.raises(PolicyNotAcceptableError, match="server-stamped"):
        await amend_plan(
            session,
            flow_id,
            amended_proposal(extra_gate=False, execution_policy=policy),
            AmendmentContext(org_id=ORG_A, actor_id=HUMAN_USER_ID, actor_role="org_admin"),
        )
    assert await count_rows(session, OrchestrationAcceptedPlan) == 1
