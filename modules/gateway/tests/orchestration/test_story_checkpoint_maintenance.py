"""Private claims survive real source adoption; unverified lineage fails closed."""

import copy
from types import SimpleNamespace

from sqlalchemy import select

from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision
from src.orchestration.run_reports import OrchestrationRunReport
from src.orchestration.story_checkpoints import CheckpointPayload, read_story_checkpoint, report_scope, revision
from tests.orchestration import test_repeated_maintenance_adoption as fixtures

engine = fixtures.engine
session = fixtures.session
governed_work_ownership = fixtures.governed_work_ownership
healthy_policy_reservations = fixtures.healthy_policy_reservations
policy_budget_initializers = fixtures.policy_budget_initializers


async def test_private_progress_survives_real_maintenance_adoption_and_rejects_corrupt_lineage(session, monkeypatch, governed_work_ownership):
    b = await fixtures.fixtures.arrange(session, monkeypatch)
    original = await report_scope(session, b.report)
    terminal = copy.deepcopy(b.report.terminal_receipt)
    original_plan = copy.deepcopy(b.f.plan.plan_document)
    payload = CheckpointPayload.model_validate(
        {
            "scope": original.model_dump(),
            "delta": {
                "version": 1,
                "progress_only": True,
                "source": {
                    "issue": original.issue,
                    "repository_id": original.repository_id,
                    "branch": original.branch,
                    "path": f".adp/tasks/{original.issue}.json",
                    "sha256": "a" * 64,
                    "confirmed_source_revision": "b" * 40,
                },
                "updates": {"TASK-1": {"base_task_hash": "c" * 64, "status": "done"}},
                "additions": {},
                "conflicts": [],
                "history": [],
            },
        }
    ).model_dump()
    # Fixture represents a checkpoint acknowledged before this historical run ended.
    saved = {**payload, "revision": revision(payload), "parent_revision": None}
    b.report.task_board_snapshot = {"story_checkpoint": saved}
    await session.commit()
    accepted = await fixtures.adopt(session, b)
    envelope = await fixtures.resume(session, b, monkeypatch)
    current = await session.get(OrchestrationRunReport, envelope["message_id"])
    scope = await report_scope(session, current)
    assigned = SimpleNamespace(
        invocation_id=scope.run_id,
        tenant_id=scope.tenant_id,
        node_id=scope.node_id,
        attempt=scope.attempt,
        claim_generation=scope.claim_generation,
        accepted_plan_version=scope.accepted_plan_version,
        repository_id=scope.repository_id,
        installation_id=scope.installation_id,
        repository=scope.repository,
        branch=scope.branch,
    )
    restored = await read_story_checkpoint(session, assigned)
    assert restored["state"] == "available" and restored["preserved_plan"]
    assert restored["checkpoint"] == saved
    assert scope.accepted_plan_version == accepted["plan_version"] == 2
    assert b.report.terminal_receipt == terminal and b.f.plan.plan_document == original_plan
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == scope.flow_id, OrchestrationAcceptedPlan.version == 2)
    )
    receipt = await session.get(OrchestrationDecision, plan.accepted_by_decision_id)
    original_actor_kind = receipt.actor_kind
    receipt.actor_kind = "system"
    # Corrupt the read fixture only; the append-only decision is never rewritten.
    with session.no_autoflush:
        refused = await read_story_checkpoint(session, assigned)
    receipt.actor_kind = original_actor_kind
    assert refused["state"] == "unavailable"
    assert refused["reason"] == "story_checkpoint_requires_reconciliation"
    assert "checkpoint" not in refused
