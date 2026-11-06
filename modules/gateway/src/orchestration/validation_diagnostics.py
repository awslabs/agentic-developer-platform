"""Current engine check results for the flow UI; never an authorization input."""

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from .models import OrchestrationAction, OrchestrationExecution, OrchestrationNode


class ValidationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    status: Literal["passed", "failed", "waiting"]
    label: str
    detail: str
    technical_detail: str | None = None
    next_action: str | None = None
    observed_at: str | None = None


_DETAILS = {
    "deployment_workflow_inputs_invalid": (
        "Workflow inputs",
        "The workflow input definitions are invalid.",
        "Use scalar defaults in the accepted workflow's input definitions.",
    ),
    "delivery_target_lease_changed": (
        "Deployment target",
        "This attempt no longer owns the deployment target reservation.",
        "Refresh the current deployment state before retrying.",
    ),
    "delivery_workflow_failed": (
        "Deployment workflow",
        "The deployment workflow ended without success.",
        "Inspect the failed workflow steps and retry after fixing the cause.",
    ),
    "delivery_required_checks_missing": (
        "Verification evidence",
        "The workflow did not report every required check.",
        "Run the missing checks and publish their evidence.",
    ),
    "delivery_checks_failed": (
        "Verification checks",
        "A workflow verification check failed or was skipped.",
        "Fix the failed check and rerun the accepted workflow.",
    ),
    "delivery_cleanup_failed": ("Cleanup", "Required cleanup has not succeeded.", "Complete cleanup and publish the result."),
    "delivery_evidence_identity_mismatch": (
        "Evidence identity",
        "The evidence does not match this contract, source, target or workflow attempt.",
        "Publish evidence from the accepted workflow attempt.",
    ),
    "deployment_release_artifact_missing_or_ambiguous": (
        "Workflow evidence",
        "The expected evidence artifact is missing or has multiple matches.",
        "Publish one evidence artifact for this workflow attempt.",
    ),
    "operations_runtime_manifest_unavailable": (
        "Deployment configuration",
        "The deployment configuration does not match the accepted release.",
        "Restore the accepted target and workflow configuration.",
    ),
    "operations_release_ancestry_missing_or_ambiguous": (
        "Release source",
        "No source revision contains all required predecessor changes.",
        "Finish the predecessor merges and verify their commit ancestry.",
    ),
    "operations_release_target_unavailable": (
        "Deployment target",
        "The accepted target is unavailable or outside this flow's permitted scope.",
        "Restore the target connection or update the flow configuration.",
    ),
    "operations_release_verification_timed_out": (
        "Release evidence",
        "Checking the release evidence timed out.",
        "The engine will retry; inspect provider access if this repeats.",
    ),
    "deployment_connection_not_permitted": (
        "Target access",
        "This environment connection is outside the accepted scope.",
        "Use a connection permitted by this flow.",
    ),
    "operations_runtime_target_not_authorized": (
        "Target access",
        "The current execution cannot access the deployment target.",
        "Restore access through the accepted environment connection.",
    ),
    "delivery_target_busy": ("Deployment target", "Another deployment is using this target.", "Wait for that deployment to finish, then retry."),
    "dispatch_verified": ("Workflow dispatch", "The accepted workflow dispatch was recorded.", None),
    "delivery_evidence_invalid": (
        "Evidence format",
        "The workflow evidence does not match the required format.",
        "Publish a valid evidence envelope from the accepted workflow.",
    ),
    "delivery_dispatch_reconciliation_required": (
        "Dispatch confirmation",
        "A previous dispatch has an unconfirmed outcome.",
        "Observe the correlated workflow run before attempting another dispatch.",
    ),
    "runtime_verified": ("Deployment verification", "The workflow, target, release and required evidence were verified.", None),
}


def safe_code(value):
    # Only stable codes, never arbitrary exception text, provider payloads or IDs.
    code = value.split(":", 1)[0] if isinstance(value, str) else None
    return code if code is not None and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code) else "validation_unavailable"


def check_result(code, *, status="failed", observed_at=None):
    code = safe_code(code)
    label, detail, action = _DETAILS.get(
        code,
        (
            code.replace("_", " ").capitalize(),
            "The engine could not verify this requirement.",
            "Inspect the evidence for this check and retry once the requirement is met.",
        ),
    )
    return ValidationCheck(code=code, status=status, label=label, detail=detail, next_action=action, observed_at=observed_at)


async def record_runtime_validation(factory, *, binding, issued, code, passed=False, context_action_id=None, stage="runtime", invocation_id=None):
    """One latest observation per execution, fenced to its current attempt."""
    if stage not in {"runtime", "dispatch"}:
        raise ValueError("unsupported validation stage")
    async with factory() as session:
        node = await session.get(OrchestrationNode, issued.node_id)
        if node is None or node.org_id != binding.org_id:
            return
        execution = await session.scalar(
            select(OrchestrationExecution)
            .where(
                OrchestrationExecution.org_id == binding.org_id,
                OrchestrationExecution.node_id == node.id,
                OrchestrationExecution.cycle == node.attempts,
                OrchestrationExecution.accepted_plan_version == issued.plan_version,
            )
            .with_for_update()
        )
        if execution is None or execution.status in {"concluded", "superseded"}:
            return
        if context_action_id is None:
            from .operations_continuation_authority import assignment_matches
            from .run_reports import OrchestrationRunReport

            report = await session.get(OrchestrationRunReport, invocation_id) if invocation_id else None
            if report is None or not await assignment_matches(session, node=node, execution=execution, report=report):
                return
        if context_action_id is not None:
            context = await session.get(OrchestrationAction, context_action_id)
            receipt = (context.detail or {}).get("correlated_workflow_receipt", {}) if context is not None else {}
            if (
                context is None
                or context.org_id != binding.org_id
                or context.execution_id != execution.id
                or receipt.get("claim_id") != execution.claim_id
                or receipt.get("claim_generation") != execution.claim_generation
            ):
                return
        now = datetime.now(UTC)
        result = check_result(code, status="passed" if passed else "failed", observed_at=now.isoformat())
        row = await session.scalar(
            select(OrchestrationAction).where(
                OrchestrationAction.org_id == binding.org_id,
                OrchestrationAction.execution_id == execution.id,
                OrchestrationAction.operation_key == f"validation:{stage}",
            )
        )
        if row is None:
            row = OrchestrationAction(
                org_id=binding.org_id,
                execution_id=execution.id,
                operation_key=f"validation:{stage}",
                kind="engine_validation",
                status="succeeded",
                attempt=1,
            )
            session.add(row)
        # Succeeded means the observation was recorded, not that the check passed.
        row.detail = {"check": result.model_dump()}
        row.observed_at = now
        await session.commit()


async def current_validation_checks(session, *, org_id, executions):
    if not executions:
        return {}
    rows = await session.scalars(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == org_id,
            OrchestrationAction.execution_id.in_([row.id for row in executions.values()]),
            OrchestrationAction.operation_key.in_(["validation:runtime", "validation:dispatch"]),
            OrchestrationAction.kind == "engine_validation",
        )
    )
    result = {}
    for row in rows:
        value = (row.detail or {}).get("check", {})
        if not isinstance(value, dict) or value.get("status") not in {"passed", "failed"}:
            continue
        # Recreate display text from the code; stored text cannot inject secrets.
        result.setdefault(row.execution_id, []).extend(
            [check_result(value.get("code"), status=value["status"], observed_at=row.observed_at.isoformat() if row.observed_at else None)]
        )
    for execution_id, checks in result.items():
        verified = next((check for check in checks if check.code == "runtime_verified" and check.status == "passed"), None)
        if verified is not None:
            # A later verified runtime settles an earlier uncertain dispatch.
            result[execution_id] = [
                check
                for check in checks
                if check.status == "passed" or not check.observed_at or not verified.observed_at or check.observed_at > verified.observed_at
            ]
    return result


def with_validation_checks(progress, *, checks, configuration_problem=None, required_input_names=()):
    if progress is None:
        return None
    results = [*progress.validations, *checks]
    if configuration_problem:
        results.append(
            ValidationCheck(
                code="required_inputs_unavailable",
                status="waiting",
                label="Required inputs",
                detail="Waiting for " + ", ".join(name.replace("_", " ") for name in required_input_names) + "."
                if required_input_names
                else configuration_problem,
                technical_detail=configuration_problem if required_input_names else None,
                next_action="The engine will recheck when the inputs become available.",
            )
        )
    if progress.blocker and safe_code(progress.detail) != "validation_unavailable":
        underlying = check_result(progress.detail, observed_at=progress.observed_at)
        results.append(underlying)
    for code in progress.blockers:
        if code in {item.code for item in results}:
            continue
        result = check_result(code, status="waiting", observed_at=progress.observed_at)
        if code == progress.blocker:
            result.detail, result.next_action = progress.detail, progress.next_action
        results.append(result)
    return progress.model_copy(update={"validations": list({check.code: check for check in results}.values())})
