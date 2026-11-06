"""A scope amendment must retain real repairs instead of redeploying their base."""

# ruff: noqa: F811
import copy
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration import operations_agent_recovery as recovery
from src.orchestration.continuation import digest
from src.orchestration.execution_policy import AcceptanceMode, stamp_policy
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationAction, OrchestrationDecision, OrchestrationExecution
from src.orchestration.operations_repair import repair_revisions
from src.orchestration.policy_admission import AdmissionInputs
from src.orchestration.shared_retry import CONTRACT, KIND, apply_retry_limit, effective_shared_retry
from tests.orchestration.test_operations_agent_recovery import accept, pending  # noqa: F401
from tests.orchestration.test_operations_continuation import engine, reporting, retained, session, session_factory, work  # noqa: F401


@pytest.mark.parametrize("invalid", [None, "receipt", "supplement", "unsettled", "pending", "owner", "current_cycle"])
async def test_same_plan_retry_increase_preserves_completed_native_repair(session, retained, work, monkeypatch, invalid):
    ctx = retained
    draft = ctx.policy.model_copy(deep=True)
    draft.policy_id = draft.policy_hash = draft.principal_id = None
    base = stamp_policy(draft, principal_id=ctx.policy.principal_id, org_id=ctx.node.org_id)
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == ctx.node.flow_id))
    plan.plan_document = {
        "execution_policy": base.model_dump(mode="json"),
        "execution_continuation": {"mode": "shared_worker_role", "contract_version": 1},
    }
    plan.plan_hash = digest(plan.plan_document)
    now = datetime.now(UTC)
    # Both supplements belong to the same accepted plan. Only the first existed
    # when the previous native worker recorded its merge.
    for limit, seconds in ((6, -2), (10, 0)):
        session.add(
            OrchestrationDecision(
                id=f"retry-{limit}",
                org_id=ctx.node.org_id,
                flow_id=ctx.node.flow_id,
                kind=KIND,
                actor_kind="service" if invalid == "supplement" and limit == 6 else "human",
                actor_id=base.principal_id,
                actor_role="platform_admin",
                created_at=now + timedelta(seconds=seconds),
                reason=json.dumps(
                    {
                        "contract": CONTRACT,
                        "flow_id": plan.flow_id,
                        "plan_version": plan.version,
                        "plan_hash": plan.plan_hash,
                        "original_policy_hash": base.policy_hash,
                        "principal_id": base.principal_id,
                        "max_attempts_per_node": limit,
                    }
                ),
            )
        )
    recorded = apply_retry_limit(base, 6, "retry-6")
    pins = {"source_revision": "a" * 40, "definition_revision": "b" * 40, "default_branch": "main"}
    session.add(
        OrchestrationAction(
            org_id=ctx.node.org_id,
            execution_id=ctx.row.id,
            kind="operations_repair_merge",
            status="succeeded",
            attempt=3,
            operation_key="operations-repair:77:" + "c" * 40,
            receipt_ref="github/aws-e/adp/pull/77",
            created_at=now - timedelta(seconds=1),
            detail={
                "pull_number": 77,
                "head": "c" * 40,
                "merge_sha": "d" * 40,
                "repository": "aws-e/adp",
                "default_branch": "main",
                "policy_hash": "f" * 64 if invalid == "receipt" else recorded.policy_hash,
                "workflows": ["deploy.yml"],
                "repair_paths": sorted(recorded.repair_paths),
                "before_source_revision": pins["source_revision"],
                "before_definition_revision": pins["definition_revision"],
                "claim_generation": ctx.row.claim_generation,
                "run_id": ctx.report.run_id,
            },
        )
    )
    assert not (ctx.row.handoff_receipt_ref or "").startswith("operations-agent-return:")
    if invalid == "unsettled":
        ctx.row.status = "runnable"
    if invalid == "pending":
        ctx.row.pending_action_key = "unsettled-action"
    await session.flush()
    current = await effective_shared_retry(session, plan, base)
    assert current.limits.max_attempts_per_node == 10
    if invalid == "owner":
        current.principal_id = "other"
    monkeypatch.setattr("src.orchestration.operations_repair.load_in_force_policy", AsyncMock(return_value=AdmissionInputs(current, 1)))
    result = await repair_revisions(
        session,
        node_id=ctx.node.id,
        org_id=ctx.node.org_id,
        plan_version=1,
        cycle=3 if invalid == "current_cycle" else 4,
        pins=pins,
        contract=work.contract,
        require_merged=True,
    )
    assert result == (None if invalid else {**pins, "source_revision": "d" * 40, "definition_revision": "d" * 40})
    assert ctx.node.attempts == 3
    assert current.limits.max_attempts_per_node == 10
    assert current.limits.max_spend_usd == base.limits.max_spend_usd
    assert current.limits.max_concurrent_actions == base.limits.max_concurrent_actions


@pytest.mark.parametrize("invalid", [None, "owner", "removed_path", "credential", "workflow", "unaccepted", "unsettled", "receipt"])
@pytest.mark.parametrize("retain_maintenance", [False, True])
async def test_amended_scope_preserves_verified_repair_and_maintenance(session, pending, work, monkeypatch, invalid, retain_maintenance):
    ctx = pending.ctx
    pins = {"source_revision": "a" * 40, "definition_revision": "b" * 40, "default_branch": "main"}
    session.add(
        OrchestrationAction(
            org_id=ctx.node.org_id,
            execution_id=ctx.row.id,
            kind="operations_repair_merge",
            status="succeeded",
            attempt=3,
            operation_key="operations-repair:77:" + "c" * 40,
            receipt_ref="github/aws-e/adp/pull/77",
            detail={
                "pull_number": 77,
                "head": "c" * 40,
                "merge_sha": "d" * 40,
                "repository": "aws-e/adp",
                "default_branch": "main",
                "policy_hash": ctx.policy.policy_hash,
                "workflows": ["deploy.yml"],
                "repair_paths": sorted(ctx.policy.repair_paths),
                "before_source_revision": pins["source_revision"],
                "before_definition_revision": pins["definition_revision"],
                "claim_generation": ctx.row.claim_generation,
                "run_id": ctx.report.run_id,
            },
        )
    )
    monkeypatch.setattr(
        recovery,
        "maintenance_source",
        AsyncMock(
            return_value={
                "before": {**pins, "source_revision": "d" * 40, "definition_revision": "d" * 40},
                "pins": {**pins, "source_revision": "e" * 40, "definition_revision": "e" * 40},
                "deployment_run_id": 123,
                "workflow": ".github/workflows/deploy.yml",
            }
            if retain_maintenance
            else None
        ),
    )
    await session.commit()
    await accept(session, pending)
    original = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == ctx.node.flow_id))
    address = f"{pending.flow.slug}/{ctx.node.epic_ref}/{ctx.node.wave_ref}/{ctx.node.node_ref}"
    document = {
        "nodes": [
            {
                "address": address,
                "kind": "story",
                "title": "Repair",
                "issue_ref": "4196",
                "required_inputs": [{"source": item.source} for item in work.contract.node.required_inputs],
            }
        ],
        "edges": [],
        "execution_policy": ctx.policy.model_dump(mode="json"),
    }
    # Complete the synthetic original fixture before exercising the real lineage reader.
    original.plan_document = document
    original.superseded_at = datetime.now(UTC)
    draft = ctx.policy.model_copy(deep=True)
    draft.repair_paths = [*draft.repair_paths, "tests/e2e/runtime.py"]
    # Replacing a downstream evaluation changes its acceptance address, not
    # the authority under which this completed repair was merged.
    draft.evaluation_acceptance = {f"{pending.flow.slug}/epic/wave/replacement-eval": AcceptanceMode.MACHINE}
    draft.limits = draft.limits.model_copy(update={"max_attempts_per_node": 5})
    if invalid == "removed_path":
        draft.repair_paths = ["tests/e2e/runtime.py"]
    if invalid == "credential":
        draft.environment_connection_ids = ["other"]
    draft.policy_id = draft.policy_hash = draft.principal_id = None
    policy = stamp_policy(draft, principal_id="other" if invalid == "owner" else ctx.policy.principal_id, org_id=ctx.node.org_id)
    changed = copy.deepcopy(document)
    changed["execution_policy"] = policy.model_dump(mode="json")
    if invalid == "workflow":
        changed["nodes"][0]["required_inputs"][0]["source"] = "deployment_workflow:other.yml"
    decision = OrchestrationDecision(
        org_id=ctx.node.org_id,
        flow_id=ctx.node.flow_id,
        kind="plan_amended",
        actor_kind="human",
        actor_id="human",
        actor_role="platform_admin",
        reason="Expand scoped repair",
    )
    session.add(decision)
    await session.flush()
    session.add(
        OrchestrationAcceptedPlan(
            org_id=ctx.node.org_id,
            flow_id=ctx.node.flow_id,
            version=2,
            plan_hash="f" * 64,
            plan_document=changed,
            accepted_by_decision_id=None if invalid == "unaccepted" else decision.id,
        )
    )
    work.contract.plan_version = 2
    if invalid == "unsettled":
        ctx.row.status = "runnable"
    if invalid == "receipt":
        ctx.row.handoff_receipt_ref = "operations-agent-return:missing"
    monkeypatch.setattr("src.orchestration.operations_repair.load_in_force_policy", AsyncMock(return_value=AdmissionInputs(policy, 2)))
    await session.commit()
    result = await repair_revisions(
        session, node_id=ctx.node.id, org_id=ctx.node.org_id, plan_version=2, cycle=4, pins=pins, contract=work.contract, require_merged=True
    )
    expected = ("e" if retain_maintenance else "d") * 40
    assert result == (None if invalid else {**pins, "source_revision": expected, "definition_revision": expected})
    assert ctx.node.attempts == 3
    if invalid:
        return
    successor = OrchestrationExecution(
        org_id=ctx.node.org_id,
        flow_id=ctx.node.flow_id,
        node_id=ctx.node.id,
        cycle=4,
        accepted_plan_version=2,
        claim_id=ctx.row.claim_id,
        claim_generation=5,
        phase="delivering",
        status="runnable",
    )
    session.add(successor)
    await session.flush()
    session.add(
        OrchestrationAction(
            org_id=ctx.node.org_id,
            execution_id=successor.id,
            kind="operations_repair_merge",
            status="succeeded",
            attempt=4,
            operation_key="operations-repair:78:" + "8" * 40,
            receipt_ref="github/aws-e/adp/pull/78",
            detail={
                "pull_number": 78,
                "head": "8" * 40,
                "merge_sha": "9" * 40,
                "repository": "aws-e/adp",
                "default_branch": "main",
                "policy_hash": policy.policy_hash,
                "workflows": ["deploy.yml"],
                "repair_paths": sorted(policy.repair_paths),
                "before_source_revision": expected,
                "before_definition_revision": expected,
                "claim_generation": 5,
                "run_id": "next-worker",
            },
        )
    )
    await session.flush()
    assert await repair_revisions(
        session, node_id=ctx.node.id, org_id=ctx.node.org_id, plan_version=2, cycle=4, pins=pins, contract=work.contract, require_merged=True
    ) == {
        **pins,
        "source_revision": "9" * 40,
        "definition_revision": "9" * 40,
    }
