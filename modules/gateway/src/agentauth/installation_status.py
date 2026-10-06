"""Read bounded, attributed runtime observations without inventing installed versions."""

from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import and_, select

from src.orchestration.deployment_runtime_contract import DeploymentReceipt
from src.orchestration.merge_controller import MERGE_KIND, MergeReceipt
from src.orchestration.models import OrchestrationAction, OrchestrationExecution, OrchestrationPullRequestBinding


def installed_record_capability() -> dict:
    return {"status": "unavailable", "reason": "installed_record_provider_unavailable"}


def unavailable_installation_status(installation_id: int) -> dict:
    return {
        "status": "unavailable",
        "installation_id": installation_id,
        "reason": "diagnostic_record_unavailable",
        "capabilities": {"installed_record": installed_record_capability()},
        "desired_release": installed_record_capability(),
        "last_verified_release": installed_record_capability(),
        "optional_modules": {"status": "unknown", "reason": "installed_record_provider_unavailable"},
    }


def project_runtime_observation(receipt: DeploymentReceipt, *, installation_id: int, now: datetime) -> dict:
    components = [
        {
            "component": component.component,
            "version": None,
            "observed_revision": component.actual_revision,
            "health": "stale" if now >= receipt.valid_until else "observed_healthy",
            "observed_at": component.observed_at.isoformat(),
            "evidence": "runtime_verification",
        }
        for component in receipt.components
    ]
    result = unavailable_installation_status(installation_id)
    result.pop("reason")
    result.update(
        {
            "status": "partial",
            "components": components,
            "observed_at": receipt.observed_at.isoformat(),
            "valid_until": receipt.valid_until.isoformat(),
            "coverage": "runtime_receipt_only",
        }
    )
    return result


async def read_installation_status(db, scope, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    bindings = OrchestrationPullRequestBinding
    executions = OrchestrationExecution
    actions = OrchestrationAction
    rows = (
        await db.execute(
            select(actions, executions, bindings)
            .join(executions, and_(actions.org_id == executions.org_id, actions.execution_id == executions.id))
            .join(
                bindings,
                and_(
                    bindings.org_id == executions.org_id,
                    bindings.flow_id == executions.flow_id,
                    bindings.node_id == executions.node_id,
                    bindings.attempt == executions.cycle,
                ),
            )
            .where(
                bindings.org_id == scope.tenant_id,
                bindings.installation_id == scope.installation_id,
                actions.kind == "deployment_verification",
                actions.status == "succeeded",
            )
            .order_by(actions.observed_at.desc(), actions.id.desc())
            .limit(50)
        )
    ).all()
    for action, execution, binding in rows:
        try:
            receipt = DeploymentReceipt.model_validate((action.detail or {})["deployment_receipt"])
        except (KeyError, TypeError, ValidationError):
            continue
        if (
            receipt.org_id != scope.tenant_id
            or receipt.execution_id != execution.id
            or receipt.cycle != execution.cycle
            or receipt.flow_id != binding.flow_id
            or receipt.node_id != binding.node_id
            or receipt.repo != binding.repo
            or binding.revision != 1
            or receipt.docs_only
        ):
            continue
        merge_action = await db.scalar(
            select(actions).where(
                actions.org_id == scope.tenant_id,
                actions.execution_id == execution.id,
                actions.operation_key == receipt.merge_operation_key,
                actions.kind == MERGE_KIND,
                actions.status == "succeeded",
            )
        )
        if merge_action is None:
            continue
        try:
            merge = MergeReceipt.model_validate((merge_action.detail or {})["merge_receipt"])
        except (KeyError, TypeError, ValidationError):
            continue
        if any(
            getattr(merge, field) != getattr(receipt, field)
            for field in ("org_id", "execution_id", "flow_id", "node_id", "cycle", "accepted_plan_version", "claim_id", "claim_generation", "repo")
        ):
            continue
        if (
            merge.operation_key != receipt.merge_operation_key
            or merge.merge_sha != receipt.source_revision
            or merge.reviewed_head_sha != binding.head_sha
            or merge.pr_number != binding.pr_number
            or merge.provider_repository_id != binding.provider_repository_id
            or merge.provider_pr_node_id != binding.provider_pr_node_id
        ):
            continue
        attributed = (
            await db.scalars(
                select(bindings.installation_id)
                .where(
                    bindings.org_id == scope.tenant_id,
                    bindings.flow_id == receipt.flow_id,
                    bindings.node_id == receipt.node_id,
                    bindings.attempt == receipt.cycle,
                    bindings.repo == receipt.repo,
                    bindings.head_sha == merge.reviewed_head_sha,
                )
                .limit(2)
            )
        ).all()
        if len(attributed) != 1 or attributed[0] != scope.installation_id:
            continue
        return project_runtime_observation(receipt, installation_id=scope.installation_id, now=now)
    return unavailable_installation_status(scope.installation_id)
