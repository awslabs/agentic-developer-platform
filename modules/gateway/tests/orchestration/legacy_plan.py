"""Persist pre-Product accepted state without using today's acceptance policy."""

from src.orchestration import compile as compiler
from src.orchestration.compile import CompileResult, compile_proposal, plan_hash, upsert_edges
from src.orchestration.models import DecisionKind
from src.orchestration.proposal import split_address
from src.orchestration.repository import OrchestrationRepository


async def persist_legacy_plan(session, proposal, approval):
    assert proposal.product_id is None
    proposal = compiler.prepare_execution_policy(proposal, decision=approval, decision_kind=DecisionKind.PLAN_ACCEPTED)
    repository = OrchestrationRepository(session)
    flow = await repository.create_flow(
        org_id=approval.org_id,
        slug=proposal.flow_slug,
        title=proposal.title,
        intent_ref=proposal.intent_ref,
        description=proposal.description,
        design_history=proposal.design_history.model_dump(mode="json") if proposal.design_history else None,
    )
    if await repository.get_accepted_plan(org_id=approval.org_id, flow_id=flow.id) is not None:
        return await compile_proposal(session, proposal, approval)
    node_ids = {}
    for proposed in proposal.nodes:
        _, epic_ref, wave_ref, node_ref = split_address(proposed.address)
        node = await repository.add_node(
            org_id=approval.org_id,
            flow_id=flow.id,
            epic_ref=epic_ref,
            wave_ref=wave_ref,
            node_ref=node_ref,
            kind=proposed.kind,
            title=proposed.title,
            issue_ref=proposed.issue_ref,
            lineage=proposed.lineage.model_dump(mode="json") if proposed.lineage else None,
            labels=proposed.labels,
        )
        node.repository_identity = proposed.repository_identity
        node.provider_repository_id = proposed.provider_repository_id
        node.repository_connection = proposed.repository_connection.model_dump(mode="json") if proposed.repository_connection else None
        node_ids[proposed.address] = node.id
    await upsert_edges(repository, proposal=proposal, org_id=approval.org_id, flow_id=flow.id, node_ids=node_ids)
    decision = await repository.append_decision(
        org_id=approval.org_id,
        flow_id=flow.id,
        kind=DecisionKind.PLAN_ACCEPTED.value,
        actor_id=approval.actor_id,
        actor_role=approval.actor_role,
        actor_kind=approval.actor_kind.value,
    )
    plan = await repository.record_accepted_plan(
        org_id=approval.org_id,
        flow_id=flow.id,
        plan_document=proposal.model_dump(mode="json"),
        plan_hash=plan_hash(proposal),
        accepted_by_decision_id=decision.id,
    )
    return CompileResult(flow_id=flow.id, plan_version=plan.version, decision_id=decision.id, plan_hash=plan.plan_hash, node_ids=node_ids)
