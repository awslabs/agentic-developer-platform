"""Retain settled diagnoses across preserved plans without rewriting history."""

import re

from sqlalchemy import func, select

from .models import OrchestrationAction, OrchestrationExecution

MAX_DIAGNOSIS_HISTORY = 2000


def _completed_code(action):
    key = re.fullmatch(r"operations-task:([1-9][0-9]{0,3}):diagnose", action.operation_key)
    if key is None or not 1 <= int(key[1]) <= 1000:
        return None
    detail = action.detail or {}
    code = detail.get("diagnosis_code")
    # Only the authenticated operations-task ledger is consulted. Seeded tasks
    # also have status=succeeded; that status alone is not a completed diagnosis.
    return code if detail.get("state") == "done" and isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code) else None


def _input_required(code):
    # These diagnoses explicitly request external input. A source change or a
    # different spelling of the input cannot supply it. Unknown codes retain the
    # ordinary repeated-diagnosis rule; no prose or task-title inference is used.
    return code == "devops_human_evidence_required" or re.fullmatch(r"missing_protected_input\.[A-Za-z0-9_][A-Za-z0-9_.-]*", code) is not None


async def retry_diagnosis_blocker(session, *, node, contract, runtime_unverified, failure_category):
    from .operations_evidence import assignment_version_preserved

    rows = (
        await session.execute(
            select(OrchestrationExecution, OrchestrationAction)
            .join(OrchestrationAction, OrchestrationAction.execution_id == OrchestrationExecution.id)
            .where(
                OrchestrationExecution.org_id == node.org_id,
                OrchestrationExecution.flow_id == node.flow_id,
                OrchestrationExecution.node_id == node.id,
                OrchestrationExecution.cycle <= node.attempts,
                OrchestrationExecution.accepted_plan_version <= contract.plan_version,
                OrchestrationExecution.status == "concluded",
                OrchestrationExecution.phase == "concluded",
                OrchestrationAction.org_id == node.org_id,
                OrchestrationAction.kind == "operations_task",
                OrchestrationAction.status == "succeeded",
                OrchestrationAction.operation_key.like("operations-task:%:diagnose"),
            )
            .order_by(
                OrchestrationExecution.cycle.desc(),
                func.coalesce(OrchestrationAction.updated_at, OrchestrationAction.created_at).desc(),
                OrchestrationAction.id.desc(),
            )
            .limit(MAX_DIAGNOSIS_HISTORY + 1)
        )
    ).all()
    if len(rows) > MAX_DIAGNOSIS_HISTORY:
        return "recovery_evidence_unavailable"
    preserved, diagnoses = {}, []
    for execution, action in rows:
        if _completed_code(action) is None:
            continue
        version = execution.accepted_plan_version
        if version not in preserved:
            preserved[version] = await assignment_version_preserved(session, node, fenced=version, in_force=contract.plan_version)
        if preserved[version]:
            diagnoses.append((execution, action))
    latest = next((row for row in diagnoses if row[0].cycle == node.attempts), None)
    # An attempt that only opened more tasks has not superseded a completed
    # input hold. A later completed diagnosis can supersede it explicitly.
    if latest is None and diagnoses and _input_required(_completed_code(diagnoses[0][1])):
        return "operations_input_required"
    if latest is None:
        return None
    execution, action = latest
    code = _completed_code(action)
    # Only this attempt's latest completed diagnosis can request input. A later
    # completed diagnosis supersedes it; unfinished work cannot erase it.
    if _input_required(code):
        return "operations_input_required"
    previous = next((row for row in diagnoses if row[0].cycle < node.attempts), None)
    if previous is None or _completed_code(previous[1]) != code or (not runtime_unverified and failure_category == "transport"):
        return None

    release = contract.node.operational_release_binding
    if release is not None and release.maintenance_adoption_id is not None:
        from .maintenance_adoption_resolution import verified_adoption_checkpoints
        from .review_cycle import CycleBlockedError

        try:
            checkpoints = await verified_adoption_checkpoints(session, node, contract)
        except CycleBlockedError:
            return "recovery_evidence_unavailable"
        # A verified changed-source checkpoint permits a fresh observation of a
        # source-sensitive failure only when accepted after the last failed run.
        # Once the new source was observed and failed identically, that allowance
        # is consumed. A numeric plan bump alone is not repair evidence.
        if any(execution.cycle <= data["checkpoint_cycle"] and data["before_pins"] != data["pins"] for data in checkpoints):
            return None

    repaired = await session.scalar(
        select(OrchestrationAction)
        .where(
            OrchestrationAction.org_id == node.org_id,
            OrchestrationAction.execution_id == execution.id,
            OrchestrationAction.kind == "operations_repair_merge",
            OrchestrationAction.status == "succeeded",
            OrchestrationAction.created_at > (action.updated_at or action.created_at),
        )
        .limit(1)
    )
    if repaired is not None:
        from .dispatch_pass import attempt_run_id
        from .operations_repair import repair_revisions
        from .run_reports import OrchestrationRunReport

        report = await session.get(OrchestrationRunReport, attempt_run_id(node.id, node.attempts))
        pins = ((report.dispatch_metadata or {}).get("orchestration") or {}).get("deployment_revisions") if report is not None else None
        resolved = await repair_revisions(
            session,
            node_id=node.id,
            org_id=node.org_id,
            plan_version=execution.accepted_plan_version,
            cycle=execution.cycle,
            pins=pins,
            contract=contract,
        )
        if resolved is not None and resolved != pins:
            return None
    return "deterministic_failure_unchanged"
