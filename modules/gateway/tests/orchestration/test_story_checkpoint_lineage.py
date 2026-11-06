from src.orchestration.amend import amend_plan
from src.orchestration.story_checkpoints import CheckpointScope, preserved_scope
from tests.orchestration import test_pending_amendments as fixtures
from tests.orchestration.legacy_plan import persist_legacy_plan
from tests.orchestration.test_plan_lineage_preserving import _node

session = fixtures.session


async def test_checkpoint_plan_bridge_uses_real_preservation_receipts(session):
    base = fixtures.base_proposal()
    compiled = await persist_legacy_plan(session, base, fixtures.approval())
    story = await _node(session, compiled.flow_id, "story-a")
    story.state, story.attempts = "running", 1
    await session.commit()
    await amend_plan(session, flow_id=compiled.flow_id, proposal=fixtures.amended_proposal(extra_gate=False), actor=fixtures.amender())
    await session.commit()
    scope = CheckpointScope(
        tenant_id=base.org_id,
        flow_id=compiled.flow_id,
        node_id=story.id,
        run_id="old-run",
        attempt=1,
        claim_generation=1,
        accepted_plan_version=1,
        issue=12,
        repository_id=7,
        installation_id=1,
        repository="example/service",
        branch="agent/issue-12",
    )
    newer = scope.model_copy(update={"run_id": "new-run", "attempt": 2, "accepted_plan_version": 2})
    assert await preserved_scope(session, scope, newer)
    assert not await preserved_scope(session, scope, newer.model_copy(update={"flow_id": "foreign"}))
    # The same amendment changed this evaluation's prerequisites: no bridge.
    evaluation = await _node(session, compiled.flow_id, "eval")
    assert not await preserved_scope(
        session, scope.model_copy(update={"node_id": evaluation.id}), newer.model_copy(update={"node_id": evaluation.id})
    )
