"""An ordinary amendment keeps the execution authority of nodes it left untouched (#7535)."""

import copy

import pytest
from sqlalchemy import select

from src.orchestration.amend import amend_plan
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationNode
from src.orchestration.plan_lineage import PlanLineageResolver, ancestor_plan, execution_plan_matches, preserved_execution_pairs
from tests.orchestration import test_amendment_graph_safety as graph_safety
from tests.orchestration import test_pending_amendments as fixtures
from tests.orchestration.legacy_plan import persist_legacy_plan

session = fixtures.session


async def _node(session, flow_id, node_ref):
    return await session.scalar(select(OrchestrationNode).where(OrchestrationNode.flow_id == flow_id, OrchestrationNode.node_ref == node_ref))


async def _current(session, flow_id):
    return await session.scalar(
        select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow_id, OrchestrationAcceptedPlan.superseded_at.is_(None))
    )


async def test_untouched_started_story_keeps_its_authority_across_an_ordinary_amendment(session):
    base = fixtures.base_proposal()
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    story = await _node(session, compiled.flow_id, "story-a")
    story.state, story.attempts = "running", 1
    await session.commit()
    # Adds story-b feeding the eval; story-a's definition and prerequisites are unchanged.
    accepted = await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
    await session.commit()
    assert accepted.plan_version == 2
    current = await _current(session, compiled.flow_id)
    bridged = await ancestor_plan(session, current, 1, node_id=story.id)
    assert bridged is not None and bridged.version == 1
    assert await execution_plan_matches(session, org_id=base.org_id, flow_id=compiled.flow_id, node_id=story.id, version=1, current_version=2)
    # The eval gained a prerequisite (story-b), so an execution admitted for it under v1 does not carry over.
    evaluation = await _node(session, compiled.flow_id, "eval")
    assert await ancestor_plan(session, current, 1, node_id=evaluation.id) is None
    assert not await execution_plan_matches(
        session, org_id=base.org_id, flow_id=compiled.flow_id, node_id=evaluation.id, version=1, current_version=2
    )
    # Without a node there is nothing to prove preservation for.
    assert await ancestor_plan(session, current, 1) is None


async def test_preservation_is_transitive_across_several_ordinary_amendments(session):
    base = fixtures.base_proposal()
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    story = await _node(session, compiled.flow_id, "story-a")
    story.state, story.attempts = "running", 1
    await session.commit()
    second = fixtures.amended_proposal(extra_gate=False)
    await amend_plan(session, flow_id=compiled.flow_id, proposal=second, actor=fixtures.amender())
    await session.commit()
    third = second.model_copy(deep=True)
    next(node for node in third.nodes if node.kind == "gate").title = "Deploy wave 2?"
    accepted = await amend_plan(session, flow_id=compiled.flow_id, proposal=third, actor=fixtures.amender())
    await session.commit()
    assert accepted.plan_version == 3
    current = await _current(session, compiled.flow_id)
    assert (await ancestor_plan(session, current, 1, node_id=story.id)).version == 1
    assert (await ancestor_plan(session, current, 2, node_id=story.id)).version == 2
    assert await execution_plan_matches(session, org_id=base.org_id, flow_id=compiled.flow_id, node_id=story.id, version=1, current_version=3)


async def test_preserved_pairs_bulk_load_and_reuse_without_hop_queries(session, monkeypatch):
    from tests.orchestration.test_list_flows import statement_recorder
    from tests.orchestration.test_progress_projection import execution

    base = fixtures.base_proposal()
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    story = await _node(session, compiled.flow_id, "story-a")
    evaluation = await _node(session, compiled.flow_id, "eval")
    story.state, story.attempts = "running", 1
    await session.commit()
    await execution(session, story, accepted_plan_version=1)
    await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
    await session.commit()
    evaluation.state, evaluation.attempts = "running", 1
    await execution(session, evaluation, accepted_plan_version=1)
    await session.commit()

    from src.orchestration import plan_lineage

    edge_scans = []
    original_edges = plan_lineage.edge_set

    def counted_edges(document):
        edge_scans.append(id(document))
        return original_edges(document)

    monkeypatch.setattr(plan_lineage, "edge_set", counted_edges)
    resolver = PlanLineageResolver()
    statements, stop = statement_recorder(session)
    try:
        preserved = await preserved_execution_pairs(session, org_id=base.org_id, flow_ids=[compiled.flow_id], resolver=resolver)
        first_count = len(statements)
        repeated = await preserved_execution_pairs(session, org_id=base.org_id, flow_ids=[compiled.flow_id], resolver=resolver)
    finally:
        stop()

    assert preserved == repeated == [(story.id, 1)]
    assert len(edge_scans) <= 2, "Index each plan once, instead of rescanning edges for every node"
    assert first_count == len(statements) == 3, statements
    # The current plan can be large and must not be transferred once per run.
    assert "orchestration_accepted_plans.plan_document" not in statements[0]
    assert "orchestration_executions.progress_note" not in statements[0]


async def test_changed_definition_of_an_unstarted_node_breaks_its_own_lineage_only(session):
    base = graph_safety.evaluation_plan()
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    story = await _node(session, compiled.flow_id, "story-a")
    story.state, story.attempts = "running", 1
    await session.commit()
    proposal = base.model_copy(deep=True)
    evaluation_node = next(node for node in proposal.nodes if node.kind == "eval")
    evaluation_node.criteria.append("new-requirement")
    evaluation_node.evaluation["criteria"].append({"criterion_id": "new-requirement", "kind": "api", "required": True})
    await amend_plan(session, flow_id=compiled.flow_id, proposal=proposal, actor=fixtures.amender())
    await session.commit()
    current = await _current(session, compiled.flow_id)
    evaluation = await _node(session, compiled.flow_id, "eval")
    assert await ancestor_plan(session, current, 1, node_id=evaluation.id) is None
    assert (await ancestor_plan(session, current, 1, node_id=story.id)).version == 1


async def test_the_approval_recorded_at_dispatch_keeps_authorizing_a_preserved_node(session):
    from src.orchestration.plan_lineage import approval_authorizes_node

    base = fixtures.base_proposal()
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    story = await _node(session, compiled.flow_id, "story-a")
    story.state, story.attempts = "running", 1
    await session.commit()
    original = await _current(session, compiled.flow_id)
    root = original.accepted_by_decision_id
    assert root
    assert await approval_authorizes_node(session, org_id=base.org_id, flow_id=compiled.flow_id, node_id=story.id, decision_id=root)
    await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
    await session.commit()
    # The newer approval supersedes the plan, not the running story's assignment.
    assert await approval_authorizes_node(session, org_id=base.org_id, flow_id=compiled.flow_id, node_id=story.id, decision_id=root)
    evaluation = await _node(session, compiled.flow_id, "eval")
    assert not await approval_authorizes_node(session, org_id=base.org_id, flow_id=compiled.flow_id, node_id=evaluation.id, decision_id=root)
    assert not await approval_authorizes_node(session, org_id=base.org_id, flow_id=compiled.flow_id, node_id=story.id, decision_id="unknown")


async def test_a_gate_approval_recorded_at_dispatch_keeps_authorizing_a_preserved_node(session):
    from src.orchestration.models import OrchestrationDecision
    from src.orchestration.plan_lineage import approval_authorizes_node

    base = fixtures.base_proposal()
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    story = await _node(session, compiled.flow_id, "story-a")
    story.state, story.attempts = "running", 1
    gate = OrchestrationDecision(
        org_id=base.org_id,
        flow_id=compiled.flow_id,
        node_id=(await _node(session, compiled.flow_id, "deploy-gate")).id,
        kind="gate_approved",
        actor_id="human",
        actor_role="owner",
        actor_kind="human",
        reason="approved",
    )
    session.add(gate)
    await session.commit()
    # A gate approval roots dispatches but accepts no plan of its own.
    assert await approval_authorizes_node(session, org_id=base.org_id, flow_id=compiled.flow_id, node_id=story.id, decision_id=gate.id)
    await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
    await session.commit()
    assert await approval_authorizes_node(session, org_id=base.org_id, flow_id=compiled.flow_id, node_id=story.id, decision_id=gate.id)
    evaluation = await _node(session, compiled.flow_id, "eval")
    assert not await approval_authorizes_node(session, org_id=base.org_id, flow_id=compiled.flow_id, node_id=evaluation.id, decision_id=gate.id)


@pytest.mark.parametrize("corruption", ["duplicate_node", "changed_edge", "foreign_receipt", "irrelevant_invalid_address"])
async def test_display_indexes_preserve_validation_and_do_not_outlive_request(session, corruption):
    from src.orchestration.models import OrchestrationDecision
    from tests.orchestration.test_progress_projection import execution

    base = fixtures.base_proposal()
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    story = await _node(session, compiled.flow_id, "story-a")
    story.state, story.attempts = "running", 1
    await execution(session, story, accepted_plan_version=1)
    await session.commit()
    await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
    await session.commit()
    first = await preserved_execution_pairs(session, org_id=base.org_id, flow_ids=[compiled.flow_id])
    assert first == [(story.id, 1)]
    current = await _current(session, compiled.flow_id)
    document = copy.deepcopy(current.plan_document)
    item = next(n for n in document["nodes"] if n["address"].endswith("/story-a"))
    if corruption == "duplicate_node":
        document["nodes"].append(copy.deepcopy(item))
    elif corruption == "changed_edge":
        document["edges"].append({"from_address": "different/prerequisite", "to_address": item["address"]})
    elif corruption == "foreign_receipt":
        receipt = await session.get(OrchestrationDecision, current.accepted_by_decision_id)
        foreign = OrchestrationDecision(
            org_id="foreign-tenant",
            flow_id=receipt.flow_id,
            kind=receipt.kind,
            actor_id=receipt.actor_id,
            actor_role=receipt.actor_role,
            actor_kind=receipt.actor_kind,
            reason=receipt.reason,
        )
        session.add(foreign)
        await session.flush()
        current.accepted_by_decision_id = foreign.id
    else:
        document["nodes"].append({"address": ["not", "an", "address"]})
    current.plan_document = document
    await session.commit()
    authoritative = await ancestor_plan(session, current, 1, node_id=story.id)
    displayed = await preserved_execution_pairs(session, org_id=base.org_id, flow_ids=[compiled.flow_id])
    expected = [(story.id, 1)] if corruption == "irrelevant_invalid_address" else []
    assert (authoritative is not None) == bool(expected)
    assert displayed == expected


@pytest.mark.parametrize("state", ["passed", "superseded", "failed", "halted", "rejected_at_gate", "awaiting_gate"])
async def test_engine_owned_display_skips_unused_history_without_changing_progress(session, state):
    from src.orchestration.delivery_progress import node_progress
    from src.orchestration.display_state import ENGINE_TO_DISPLAY
    from src.orchestration.repository import OrchestrationRepository
    from src.orchestration.state import NodeState
    from tests.orchestration.test_list_flows import statement_recorder
    from tests.orchestration.test_progress_projection import execution

    base = fixtures.base_proposal()
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    story = await _node(session, compiled.flow_id, "story-a")
    story.state, story.attempts = "running", 1
    row = await execution(session, story, accepted_plan_version=1, status="blocked", block_code="human_input_required")
    await session.commit()
    await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
    story.state = state
    await session.commit()
    current = await _current(session, compiled.flow_id)
    assert await ancestor_plan(session, current, 1, node_id=story.id) is not None
    kwargs = dict(node=story, binding=None, dispatch={}, result={}, execution=row, policy_enabled=True, plan_version=2)
    assert node_progress(**kwargs, preserved_execution=True) == node_progress(**kwargs, preserved_execution=False)

    statements, stop = statement_recorder(session)
    try:
        shown = await OrchestrationRepository(session).node_display_states(org_id=base.org_id, flow_id=compiled.flow_id)
    finally:
        stop()
    expected = ENGINE_TO_DISPLAY[NodeState(state)]
    assert shown[story.id] == (expected.value if expected is not None else None)
    assert not any("orchestration_accepted_plans.plan_document" in sql for sql in statements), "Irrelevant history must not be transferred"


@pytest.mark.parametrize("phase", ["deployment_pending", "awaiting_runtime_verification", "evaluation_pending"])
async def test_passed_story_preserves_amended_delivery_progress_until_execution_settles(session, phase):
    from src.orchestration.delivery_progress import node_progress
    from tests.orchestration.test_progress_projection import execution

    base = fixtures.base_proposal()
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    story = await _node(session, compiled.flow_id, "story-a")
    story.state, story.attempts = "running", 1
    row = await execution(session, story, accepted_plan_version=1, status="blocked", block_code="human_input_required")
    await session.commit()
    await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
    story.state, row.phase = "passed", phase
    await session.commit()

    pairs = await preserved_execution_pairs(session, org_id=base.org_id, flow_ids=[compiled.flow_id])
    assert pairs == [(story.id, 1)]
    await session.refresh(row)
    kwargs = dict(node=story, binding=None, dispatch={}, result={}, execution=row, policy_enabled=True, plan_version=2)
    progress = node_progress(**kwargs, preserved_execution=(story.id, 1) in pairs)
    assert progress.stage == phase and progress.blocker == "human_input_required"
    assert node_progress(**kwargs, preserved_execution=False).blocker == "execution_stale"

    row.status = "concluded"
    await session.commit()
    assert await preserved_execution_pairs(session, org_id=base.org_id, flow_ids=[compiled.flow_id]) == []
    assert node_progress(**kwargs, preserved_execution=False).stage == "complete"


async def test_completed_history_cannot_exhaust_live_display_lineage_bound(session):
    from src.orchestration.models import OrchestrationExecution
    from src.orchestration.repository import OrchestrationRepository
    from tests.orchestration.test_progress_projection import execution

    base = fixtures.base_proposal()
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    story = await _node(session, compiled.flow_id, "story-a")
    story.state, story.attempts = "running", 1
    await execution(session, story, accepted_plan_version=1, status="blocked", block_code="human_input_required")
    await session.commit()
    await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
    for index in range(1001):
        node_id = f"settled-{index}"
        session.add(
            OrchestrationNode(
                id=node_id,
                org_id=base.org_id,
                flow_id=compiled.flow_id,
                epic_ref=story.epic_ref,
                wave_ref=story.wave_ref,
                node_ref=node_id,
                title=node_id,
                kind="story",
                state="passed",
                attempts=1,
            )
        )
        session.add(
            OrchestrationExecution(
                org_id=base.org_id,
                flow_id=compiled.flow_id,
                node_id=node_id,
                cycle=1,
                phase="awaiting_review",
                status="concluded",
                accepted_plan_version=1,
                claim_id="claim",
                claim_generation=1,
            )
        )
    await session.commit()
    shown = await OrchestrationRepository(session).node_display_states(org_id=base.org_id, flow_id=compiled.flow_id)
    assert shown[story.id] == "stalled"
    assert sum(value == "complete" for value in shown.values()) == 1001
