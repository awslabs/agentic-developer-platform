"""A start-gate approval retains financial receipts through later amendments."""

import copy
import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from src.orchestration.amend import AmendmentContext, amend_plan
from src.orchestration.amendment_impact import assess_amendment
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationNode
from src.orchestration.plan_lineage import PolicyDisplayLineage
from src.orchestration.policy_admission import load_in_force_policy
from src.orchestration.proposal import LoopProposal
from src.orchestration.repository import OrchestrationRepository
from tests.orchestration import test_proposed_policy_activation as activation

session = activation.session
registrar = activation.registrar
access = activation.access
autonomy_default_unset = activation.autonomy_default_unset
provider_repository_identity = activation.provider_repository_identity


@pytest.fixture
async def accepted_budget(session, registrar, access):
    policy = activation.policy_for_these_fixtures()
    policy.limits = policy.limits.model_copy(update={"max_spend_usd": Decimal("2000")})
    work = activation.gateless_proposal()
    work.nodes = [node for node in work.nodes if node.address.endswith(("/story-a", "/eval-w1"))]
    work.edges = [edge for edge in work.edges if edge.from_address.endswith("/story-a")]
    result, _ = await activation.register_policy_bearing_draft(session, registrar, policy=policy, proposal=work)
    draft = await activation.in_force(session)
    gate = (await activation.nodes_by_ref(session))[activation.ACCEPTANCE_GATE_REF]
    answer = await activation.answer_acceptance_gate(session, access, node_id=gate.id, expected_plan_hash=draft.plan_hash)
    assert answer.status is activation.GateAnswerStatus.APPLIED
    base = await activation.in_force(session)
    assert base.version == 2 and base.accepted_by_decision_id == answer.decision_id
    stored_policy = base.plan_document["execution_policy"]
    receipt = await OrchestrationRepository(session).append_decision(
        org_id=base.org_id,
        flow_id=result.flow_id,
        kind="budget_increased",
        actor_id=stored_policy["principal_id"],
        actor_role="platform_admin",
        actor_kind="human",
        reason=json.dumps(
            {
                "contract": "shared-budget-increase/v1",
                "flow_id": base.flow_id,
                "plan_version": base.version,
                "plan_hash": base.plan_hash,
                "original_policy_hash": stored_policy["policy_hash"],
                "principal_id": stored_policy["principal_id"],
                "limits": {"max_spend_usd": "2000", "max_run_spend_usd": "100", "max_chain_spend_usd": "100"},
            }
        ),
    )
    await session.commit()
    actor = AmendmentContext(org_id=base.org_id, actor_id=stored_policy["principal_id"], actor_role="platform_admin")
    proposal = LoopProposal.model_validate(base.plan_document)
    proposal.execution_policy = proposal.execution_policy.model_copy(update={"policy_id": None, "policy_hash": None, "principal_id": None})
    next(node for node in proposal.nodes if node.kind == "story").title = "Clarified implementation outcome"
    return base, receipt, actor, proposal


async def test_previewed_budget_survives_real_gate_acceptance_and_amendment(session, accepted_budget):
    base, receipt, actor, proposal = accepted_budget
    before = await load_in_force_policy(session, org_id=base.org_id, flow_id=base.flow_id)
    assert before.policy._shared_run_spend_usd == 100
    original_plan, original_receipt = copy.deepcopy(base.plan_document), receipt.reason
    nodes = list(await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == base.flow_id)))
    history = {node.id: (node.state, node.attempts) for node in nodes}
    preview = await assess_amendment(session, org_id=base.org_id, flow_id=base.flow_id, proposal=proposal, actor_id=actor.actor_id)
    assert preview.acceptable, preview.blocking
    assert preview.receipts_dropped == []
    assert preview.receipts_preserved[0]["decision_id"] == receipt.id
    assert preview.receipts_preserved[0]["after"]["max_run_spend_usd"] == "100"
    accepted = await amend_plan(session, flow_id=base.flow_id, proposal=proposal, actor=actor)
    await session.commit()
    assert accepted.plan_version == 3
    assert accepted.receipts_preserved == preview.receipts_preserved
    for display in (None, PolicyDisplayLineage()):
        after = await load_in_force_policy(session, org_id=base.org_id, flow_id=base.flow_id, display_lineage=display)
        assert after.refusal is None
        assert after.policy._shared_budget_decision_id == receipt.id
        assert after.policy._shared_run_spend_usd == after.policy._shared_chain_spend_usd == 100
        assert after.policy.limits.max_spend_usd == 2000
        assert after.policy.limits == before.policy.limits
    assert base.plan_document == original_plan and receipt.reason == original_receipt
    for node in await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == base.flow_id)):
        assert (node.state, node.attempts) == history[node.id]
    assert len(list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "budget_increased")))) == 1


@pytest.mark.parametrize("change", ["rejected", "draft", "service", "anonymous", "foreign_org", "foreign_flow", "missing"])
async def test_unverified_predecessor_cannot_carry_budget(session, accepted_budget, change):
    base, receipt, actor, proposal = accepted_budget
    await amend_plan(session, flow_id=base.flow_id, proposal=proposal, actor=actor)
    original = await session.get(OrchestrationDecision, base.accepted_by_decision_id)
    if change == "missing":
        base.accepted_by_decision_id = None
    else:
        values = {column.name: getattr(original, column.name) for column in OrchestrationDecision.__table__.columns if column.name != "id"}
        values.update(
            {
                "rejected": {"kind": "gate_rejected"},
                "draft": {"kind": "plan_drafted"},
                "service": {"actor_kind": "service"},
                "anonymous": {"actor_id": ""},
                "foreign_org": {"org_id": "foreign-tenant"},
                "foreign_flow": {"flow_id": "foreign-flow"},
            }[change]
        )
        altered = OrchestrationDecision(**values)
        session.add(altered)
        await session.flush()
        base.accepted_by_decision_id = altered.id
    await session.commit()
    for display in (None, PolicyDisplayLineage()):
        current = await load_in_force_policy(session, org_id=base.org_id, flow_id=base.flow_id, display_lineage=display)
        assert current.policy._shared_budget_decision_id is None
        assert current.policy._shared_run_spend_usd is None
    assert (await session.get(OrchestrationAcceptedPlan, base.id)).version == 2
