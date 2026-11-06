"""Recoverable operations work on the node's existing execution and action ledger."""

import json

from sqlalchemy import select

from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
)

STAGES = {
    "diagnose": "Diagnose the deployment failure",
    "repair": "Make a scoped repair",
    "test": "Run the required checks",
    "publish": "Publish and merge the tested repair",
    "deploy": "Observe the repaired deployment",
    "verify": "Verify runtime and smoke results",
}
STATES = {"in_progress", "done", "blocked"}
EVIDENCE_KINDS = {
    "operations_repair_pr",
    "operations_repair_merge",
    "correlated_workflow_observation",
    "deployment_workflow",
    "deployment_verification",
}


def _refuse(reason):
    from src.agentauth.github_operations import OperationRefusedError

    raise OperationRefusedError(reason)


async def _current_execution(session, assignment, *, lock=False):
    query = select(OrchestrationExecution).where(
        OrchestrationExecution.org_id == assignment.tenant_id,
        OrchestrationExecution.node_id == assignment.node_id,
        OrchestrationExecution.cycle == assignment.attempt,
        OrchestrationExecution.accepted_plan_version == assignment.accepted_plan_version,
    )
    if lock:
        query = query.with_for_update()
    execution = await session.scalar(query)
    node = await session.get(OrchestrationNode, assignment.node_id)
    if (
        execution is None
        or node is None
        or node.org_id != assignment.tenant_id
        or node.attempts != assignment.attempt
        or execution.status in {"concluded", "superseded"}
        or execution.claim_generation != assignment.claim_generation
    ):
        _refuse("operations task ownership changed")
    return node, execution


async def seed_operations_tasks(session, execution, *, repair_cycle=1):
    existing = set(
        (
            await session.scalars(
                select(OrchestrationAction.operation_key).where(
                    OrchestrationAction.org_id == execution.org_id,
                    OrchestrationAction.execution_id == execution.id,
                    OrchestrationAction.kind == "operations_task",
                )
            )
        ).all()
    )
    for stage, title in STAGES.items():
        key = f"operations-task:{repair_cycle}:{stage}"
        if key not in existing:
            session.add(
                OrchestrationAction(
                    org_id=execution.org_id,
                    execution_id=execution.id,
                    operation_key=key,
                    kind="operations_task",
                    status="succeeded",
                    attempt=1,
                    detail={"state": "open", "title": title, "diagnosis_code": None, "repair_cycle": repair_cycle},
                )
            )
    await session.flush()


async def record_operations_task(session, assignment, *, stage, state, diagnosis_code=None, repair_cycle=1):
    if stage not in STAGES or state not in STATES or type(repair_cycle) is not int or not 1 <= repair_cycle <= 1000:
        _refuse("unknown operations task transition")
    if diagnosis_code is not None and (
        stage != "diagnose"
        or not isinstance(diagnosis_code, str)
        or not 1 <= len(diagnosis_code) <= 64
        or not all(char.isascii() and (char.isalnum() or char in "_-.") for char in diagnosis_code)
    ):
        _refuse("invalid diagnosis code")
    _, execution = await _current_execution(session, assignment, lock=True)
    await seed_operations_tasks(session, execution, repair_cycle=repair_cycle)
    key = f"operations-task:{repair_cycle}:{stage}"
    previous = await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == assignment.tenant_id,
            OrchestrationAction.execution_id == execution.id,
            OrchestrationAction.operation_key == key,
        )
    )
    detail = {"state": state, "title": STAGES[stage], "diagnosis_code": diagnosis_code, "repair_cycle": repair_cycle}
    if previous is not None:
        if diagnosis_code is None and stage == "diagnose":
            detail["diagnosis_code"] = (previous.detail or {}).get("diagnosis_code")
        if previous.detail == detail:
            return previous.id
        if (previous.detail or {}).get("state") == "done":
            _refuse("operations task transition is stale")
        previous.detail = detail
        await session.flush()
        return previous.id
    action = OrchestrationAction(
        org_id=assignment.tenant_id,
        execution_id=execution.id,
        operation_key=key,
        kind="operations_task",
        status="succeeded",
        attempt=1,
        detail=detail,
    )
    session.add(action)
    await session.flush()
    return action.id


async def record_repair_pr(session, assignment, status):
    node, execution = await _current_execution(session, assignment, lock=True)
    pull, head, base = status.get("pull_number"), status.get("head"), status.get("base")
    if (
        type(pull) is not int
        or pull < 1
        or not all(isinstance(sha, str) and len(sha) == 40 and all(char in "0123456789abcdef" for char in sha) for sha in (head, base))
    ):
        _refuse("repair PR identity is unavailable")
    from .maintenance_repair_coverage import assignment_adoption, covered_publication

    adoption = await assignment_adoption(session, assignment, node)
    coverage = covered_publication(
        adoption,
        repository=assignment.repository,
        receipt_ref=f"github/{assignment.repository}/pull/{pull}",
        pull_number=pull,
        head=head,
    )
    if coverage is not None:
        # Reading an already adopted PR is not a new publication. Existing
        # observations remain in the append-only history and are classified on
        # readback; never rewrite them or invent a current repair merge.
        return {"publication_recorded": False, "covered_by_adoption": coverage, "next_action": coverage["next_action"]}
    key = f"operations-repair-pr:{pull}:{head}:{base}"
    previous = await session.scalar(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == assignment.tenant_id,
            OrchestrationAction.execution_id == execution.id,
            OrchestrationAction.operation_key == key,
        )
    )
    if previous is None:
        previous = OrchestrationAction(
            org_id=assignment.tenant_id,
            execution_id=execution.id,
            operation_key=key,
            kind="operations_repair_pr",
            status="succeeded",
            attempt=1,
            receipt_ref=f"github/{assignment.repository}/pull/{pull}",
        )
        session.add(previous)
    task_cycles = [
        int(action.operation_key.split(":")[1])
        for action in (
            await session.scalars(
                select(OrchestrationAction).where(
                    OrchestrationAction.org_id == assignment.tenant_id,
                    OrchestrationAction.execution_id == execution.id,
                    OrchestrationAction.kind == "operations_task",
                )
            )
        ).all()
    ]
    repair_cycle = (previous.detail or {}).get("repair_cycle") if previous is not None and previous.detail else max(task_cycles, default=1)
    previous.detail = {
        "pull_number": pull,
        "head": head,
        "base": base,
        "checks_verified": status.get("checks_verified") is True,
        "repair_cycle": repair_cycle,
    }
    await session.flush()
    return {"publication_recorded": True, "publication_id": previous.id}


async def _task_context(session, assignment, node):
    """Display the immutable task assigned to this attempt, never issue content.

    Current effect authorization remains in the operation route. This metadata
    neither grants permissions nor replaces repair-derived deployment pins.
    """
    from .compile import address_of
    from .proposal import ProposedNode

    flow = await session.scalar(
        select(OrchestrationFlow).where(
            OrchestrationFlow.id == node.flow_id,
            OrchestrationFlow.org_id == assignment.tenant_id,
        )
    )
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.flow_id == node.flow_id,
            OrchestrationAcceptedPlan.org_id == assignment.tenant_id,
            OrchestrationAcceptedPlan.version == assignment.accepted_plan_version,
        )
    )
    if flow is None or plan is None:
        return {"available": False, "reason": "accepted_task_unavailable"}
    matches = [
        item for item in (plan.plan_document or {}).get("nodes", []) if isinstance(item, dict) and item.get("address") == address_of(flow.slug, node)
    ]
    try:
        if len(matches) != 1:
            raise ValueError("ambiguous task")
        task = ProposedNode.model_validate(matches[0])
    except ValueError:
        return {"available": False, "reason": "accepted_task_unavailable"}
    context = {
        "available": True,
        "plan_version": plan.version,
        "plan_hash": plan.plan_hash,
        "criteria": task.criteria,
        "required_inputs": [item.model_dump() for item in task.required_inputs],
        "evidence_expectations": task.evidence_expectations,
    }
    if len(json.dumps(context, ensure_ascii=False).encode()) > 16384:
        return {"available": False, "reason": "accepted_task_exceeds_context_bound"}
    return context


async def _recovery_note(session, assignment, node):
    """Read guidance from this run's recorded Resume, without granting authority."""
    from .run_reports import OrchestrationRunReport

    report = await session.get(OrchestrationRunReport, assignment.invocation_id)
    if report is None or (report.org_id, report.flow_id, report.node_id, report.attempt) != (
        assignment.tenant_id,
        node.flow_id,
        node.id,
        assignment.attempt,
    ):
        return None
    recovery = (report.dispatch_metadata.get("orchestration") or {}).get("operations_recovery") or {}
    decision_id = recovery.get("resume_decision_id")
    if not decision_id:
        return None
    decision = await session.scalar(
        select(OrchestrationDecision).where(
            OrchestrationDecision.id == decision_id,
            OrchestrationDecision.org_id == assignment.tenant_id,
            OrchestrationDecision.flow_id == node.flow_id,
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.kind == "node_stalled",
            OrchestrationDecision.from_state == "failed",
            OrchestrationDecision.to_state == "ready",
            OrchestrationDecision.actor_kind == "human",
            OrchestrationDecision.rejection_reason.is_(None),
        )
    )
    if decision is None or not decision.actor_id or not decision.reason:
        return None
    return {"decision_id": decision.id, "text": decision.reason[:2000], "truncated": len(decision.reason) > 2000}


async def read_operations_tasks(session, assignment):
    node, current = await _current_execution(session, assignment)
    from .maintenance_repair_coverage import assignment_adoption, covered_publication

    adoption = await assignment_adoption(session, assignment, node)
    # This is recovery context, not effect authorization. A plan amendment must
    # not hide an earlier repair that the dispatch gate still requires merged.
    # Keep its original version visible; current authority and repair_revisions
    # independently verify whether any historical receipt can advance pins.
    executions = (
        await session.scalars(
            select(OrchestrationExecution)
            .where(
                OrchestrationExecution.org_id == assignment.tenant_id,
                OrchestrationExecution.flow_id == node.flow_id,
                OrchestrationExecution.node_id == node.id,
                OrchestrationExecution.accepted_plan_version <= assignment.accepted_plan_version,
                OrchestrationExecution.cycle <= current.cycle,
            )
            .order_by(OrchestrationExecution.cycle)
        )
    ).all()
    cycles = []
    for execution in executions:
        actions = (
            await session.scalars(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.org_id == assignment.tenant_id,
                    OrchestrationAction.execution_id == execution.id,
                )
                .order_by(OrchestrationAction.created_at, OrchestrationAction.id)
            )
        ).all()
        progress = {action.operation_key: action.detail for action in actions if action.kind == "operations_task"}
        repair_numbers = sorted({1, *(detail.get("repair_cycle", 1) for detail in progress.values())})
        pr_cycles = {
            (action.detail.get("pull_number"), action.detail.get("head")): action.detail.get("repair_cycle", 1)
            for action in actions
            if action.kind == "operations_repair_pr" and isinstance(action.detail, dict)
        }
        revision_cycles = {
            action.detail.get("merge_sha"): pr_cycles.get((action.detail.get("pull_number"), action.detail.get("head")), 1)
            for action in actions
            if action.kind == "operations_repair_merge" and isinstance(action.detail, dict)
        }
        context_cycles = {
            action.id: revision_cycles.get((action.detail or {}).get("correlated_workflow_receipt", {}).get("source_revision"), 1)
            for action in actions
            if action.kind == "correlated_workflow_observation"
        }
        evidence = []
        for action in actions:
            if action.kind not in EVIDENCE_KINDS or action.status != "succeeded":
                continue
            detail = action.detail or {}
            record = {"id": action.id, "kind": action.kind, "receipt_ref": action.receipt_ref}
            if action.kind in {"operations_repair_pr", "operations_repair_merge"}:
                record.update({key: detail.get(key) for key in ("pull_number", "head", "base", "merge_sha", "checks_verified") if key in detail})
                if action.kind == "operations_repair_pr":
                    coverage = covered_publication(
                        adoption,
                        repository=detail.get("repository", assignment.repository),
                        receipt_ref=action.receipt_ref,
                        pull_number=detail.get("pull_number"),
                        head=detail.get("head"),
                    )
                    if coverage is not None:
                        record["covered_by_adoption"] = coverage
            elif action.kind == "correlated_workflow_observation":
                receipt = detail.get("correlated_workflow_receipt") or {}
                record.update({key: receipt.get(key) for key in ("run_id", "run_attempt", "source_revision")})
            elif action.kind == "deployment_verification":
                receipt = detail.get("deployment_receipt") or {}
                record.update({"source_revision": receipt.get("source_revision"), "workflow_operation_keys": receipt.get("workflow_operation_keys")})
            repair_cycle = detail.get("repair_cycle")
            if action.kind == "operations_repair_merge":
                repair_cycle = pr_cycles.get((detail.get("pull_number"), detail.get("head")), 1)
            elif action.kind == "correlated_workflow_observation":
                repair_cycle = revision_cycles.get(record.get("source_revision"), 1)
            elif action.kind == "deployment_verification":
                repair_cycle = revision_cycles.get(record.get("source_revision"), 1)
            elif action.kind == "deployment_workflow":
                repair_cycle = context_cycles.get(detail.get("context_action_id"), 1)
            record["repair_cycle"] = repair_cycle or 1
            evidence.append(record)
        cycles.append(
            {
                "cycle": execution.cycle,
                "execution_id": execution.id,
                "accepted_plan_version": execution.accepted_plan_version,
                "tasks": [
                    {
                        "id": stage,
                        "title": title,
                        "state": (progress.get(f"operations-task:1:{stage}") or {}).get("state", "open"),
                        "diagnosis_code": (progress.get(f"operations-task:1:{stage}") or {}).get("diagnosis_code"),
                    }
                    for stage, title in STAGES.items()
                ],
                "evidence": evidence,
                "repair_cycles": [
                    {
                        "repair_cycle": number,
                        "tasks": [
                            {
                                "id": stage,
                                "title": title,
                                "state": (progress.get(f"operations-task:{number}:{stage}") or {}).get("state", "open"),
                                "diagnosis_code": (progress.get(f"operations-task:{number}:{stage}") or {}).get("diagnosis_code"),
                            }
                            for stage, title in STAGES.items()
                        ],
                        "evidence": [item for item in evidence if item["repair_cycle"] == number],
                    }
                    for number in repair_numbers
                ],
            }
        )
    return {
        "node_id": node.id,
        "title": node.title,
        "current_cycle": current.cycle,
        "task_context": await _task_context(session, assignment, node),
        "recovery_note": await _recovery_note(session, assignment, node),
        **(
            {
                "maintenance_adoption": {
                    "adoption_id": adoption["after_binding"]["maintenance_adoption_id"],
                    "checkpoint_cycle": adoption["checkpoint_cycle"],
                    "pins": adoption["pins"],
                    "next_action": (
                        "Continue unfinished tasks using this checkpoint as the release baseline, retaining later verified repair merges. "
                        "Earlier task states are historical; "
                        "covered_by_adoption identifies repairs already included in the checkpoint. "
                        "Current runtime verification and new repair merges are still required."
                    ),
                }
            }
            if adoption is not None
            else {}
        ),
        "cycles": cycles,
    }
