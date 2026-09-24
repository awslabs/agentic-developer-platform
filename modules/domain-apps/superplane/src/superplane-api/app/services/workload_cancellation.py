"""Cancel an original workload operation through shared fencing and accounting.

Cancellation grants no provider execution authority. Only a never-acquired,
withdrawn operation with released accounting can relinquish model quota here;
every other workload retains its quota until the owned-inventory finalizer runs.
"""

from harness_jobs.identity import decode_payload, payload_digest
from harness_jobs.recovery import request_cancellation, settle_unheld_cancellation
from sqlalchemy import select

from app.adapters.harness_operation_facade import _translate
from app.adapters.operation_authority_source import GrantBackedAuthority
from app.database import async_session_factory
from app.models.controller_deployment import ControllerDeploymentOperation
from app.models.workspace import Workspace
from app.services import deployment_operations
from app.services.provisioning import ProvisioningRefused, ProvisioningUnavailable


async def cancel(
    request, db, org_id, workspace_id, deployment_id, operation_id, *, kind
):
    owner = deployment_operations.composition(request)
    if getattr(owner, "ledger", None) is None:
        raise ProvisioningUnavailable("workload cancellation accounting is unavailable")
    principal = await GrantBackedAuthority(async_session_factory).resolve(
        org_id=str(org_id),
        workspace_id=str(workspace_id),
        permission="workspace:provision",
    )
    if principal is None:
        raise ProvisioningRefused("workload cancellation authority refused")
    # Use the same workspace -> deployment lock order as admission and teardown.
    # No provider access is needed, including when the installed profile is gone.
    workspace = await db.scalar(
        select(Workspace)
        .where(Workspace.id == workspace_id, Workspace.org_id == org_id)
        .with_for_update()
    )
    if workspace is None:
        raise ProvisioningRefused("workload workspace is unavailable")
    intent = await deployment_operations.intent_for(
        db, org_id, workspace_id, deployment_id, for_update=True, workload_kind=kind
    )
    original = deployment_operations.stored_preview(intent)
    # Cancelling the bound original operation cannot retarget it. Permit this
    # even after suspension or target/profile removal; no provider call is made.
    registration = await db.scalar(
        select(ControllerDeploymentOperation).where(
            ControllerDeploymentOperation.deployment_id == str(deployment_id),
            ControllerDeploymentOperation.org_id == str(org_id),
            ControllerDeploymentOperation.workspace_id == str(workspace_id),
            ControllerDeploymentOperation.operation_id == operation_id,
            ControllerDeploymentOperation.action == "provision",
        )
    )
    if (
        registration is None
        or registration.allocation_id != original.request.parameters["allocation_id"]
        or registration.request_id != original.request.idempotency_key
    ):
        raise ProvisioningRefused("original workload operation is unavailable")
    async with owner.operation_connect() as connection:
        row = await connection.fetchrow(
            "SELECT request_payload,plan_digest FROM harness_operations WHERE operation_id=$1 "
            "AND org_id=$2 AND workspace_id=$3",
            operation_id,
            str(org_id),
            str(workspace_id),
        )
        if (
            row is None
            or row["plan_digest"] != registration.plan_digest
            or row["plan_digest"] != payload_digest(original.request)
            or decode_payload(row["request_payload"]) != original.request
        ):
            raise ProvisioningRefused("original workload operation binding changed")
        try:
            await request_cancellation(
                connection, operation_id=operation_id, principal=principal
            )
            # This call commits its fence/withdrawal before touching the budget ledger.
            # Never wrap it in a domain-owned shared-store transaction.
            await settle_unheld_cancellation(
                connection,
                owner.ledger,
                operation_id=operation_id,
                principal=principal,
                reason="authenticated workload cancellation",
            )
        except Exception as error:
            raise _translate(error) from error
        unused = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM harness_operations o "
            "JOIN harness_operation_leases l USING(operation_id) "
            "JOIN harness_approval_consumption a USING(operation_id) "
            "JOIN operation_budget_reservations b ON b.reservation_id=a.reservation_id "
            "AND b.org_id=o.org_id AND b.workspace_id=o.workspace_id "
            "AND b.job_id=o.job_id AND b.attempt_id=o.attempt_id "
            "WHERE o.operation_id=$1 AND o.org_id=$2 AND o.workspace_id=$3 "
            "AND o.plan_digest=$4 AND o.state='cancelled' AND o.cancel_requested_at IS NOT NULL "
            "AND l.org_id=o.org_id AND l.workspace_id=o.workspace_id "
            "AND a.org_id=o.org_id AND a.workspace_id=o.workspace_id AND a.plan_digest=o.plan_digest "
            "AND NOT o.cleanup_required AND l.closed_at IS NOT NULL AND l.fence_token=0 "
            "AND l.holder IS NULL AND a.reservation_state='released' AND b.state='released' "
            "AND NOT EXISTS(SELECT 1 FROM harness_dispatch_outbox WHERE operation_id=o.operation_id) "
            "AND NOT EXISTS(SELECT 1 FROM harness_provider_call_intent WHERE operation_id=o.operation_id) "
            "AND NOT EXISTS(SELECT 1 FROM harness_allocation_resource WHERE org_id=$2 "
            "AND workspace_id=$3 AND allocation_id=$5))",
            operation_id,
            str(org_id),
            str(workspace_id),
            registration.plan_digest,
            registration.allocation_id,
        )
        state = await connection.fetchrow(
            "SELECT state,cancel_requested_at IS NOT NULL AS requested FROM harness_operations "
            "WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
            operation_id,
            str(org_id),
            str(workspace_id),
        )
    if unused and intent.status not in {"Deleting", "Deleted"}:
        intent.status = "CancelledBeforeDispatch"
        intent.actual_replicas = 0
    await db.commit()
    return {
        "deployment_id": str(intent.id),
        "workspace_id": str(workspace_id),
        "operation_id": operation_id,
        "operation_state": state["state"],
        "cancellation_requested": state["requested"],
        "status": intent.status,
        "cleanup_status": "not-required"
        if unused
        else ("confirmed" if intent.status == "Deleted" else "unconfirmed"),
        "observed_cost_micros": None,
    }
