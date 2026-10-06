"""Resolve the separately admitted cleanup grant from current retirement facts."""

from harness_jobs.identity import OperationRefused
from harness_jobs.store import OperationStore

from .artifacts import read_artifact
from .authority import load_policy
from .retirement_access_artifact import validate_access_artifact
from .retirement_inventory import RetirementInventory
from .retirement_managed_access import (
    compile_managed_access_review,
    managed_recipe_inputs,
    require_managed_control_source,
)


async def resolve_managed_control(operation, inventory, context):
    """Rederive cleanup authority from the sealed paid/control chain, never a request plan.

    The current retirement grant is supplied by the trusted service. This read is
    safe before provider delivery; the runtime must recheck it under the lease
    immediately before the admitted delete.
    """
    lease, parameters = operation.grant.lease, operation.request.parameters
    if (
        operation.request.action != "teardown"
        or not isinstance(inventory, RetirementInventory)
        or inventory.preserve_cluster
        or (inventory.org_id, inventory.workspace_id)
        != (lease.org_id, lease.workspace_id)
        or not parameters.get("retirement_access_artifact_id")
        or not parameters.get("control_allocation_id")
        or not parameters.get("original_allocation_id")
        or not parameters.get("retirement_request_id")
    ):
        raise OperationRefused("managed cleanup has no current approved scope")
    artifact = await read_artifact(
        context.domain_connect,
        artifact_id=parameters["retirement_access_artifact_id"],
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        require_fresh=False,
    )
    async with context.connect() as connection:
        source = await OperationStore().get(
            connection, operation.grant.principal, artifact["source_operation_id"]
        )
        if source is None:
            raise OperationRefused("managed control producer is not in this scope")
        control = source.admitted_request()
        bootstrap_id = control.parameters.get("retirement_source_operation_id")
        bootstrap = (
            await OperationStore().get(
                connection, operation.grant.principal, bootstrap_id
            )
            if bootstrap_id
            else None
        )
        if bootstrap is None:
            raise OperationRefused("managed cleanup has no original bootstrap")
        paid_operation_id = bootstrap.admitted_request().parameters.get(
            "lifecycle_source_operation_id"
        )
        if not paid_operation_id:
            raise OperationRefused("managed cleanup has no original paid apply")
        config = load_policy(context, lease.org_id)["runtime"]
        plan = compile_managed_access_review(
            inventory,
            config,
            original_allocation_id=parameters["original_allocation_id"],
            bootstrap_artifact_id=control.parameters.get("lifecycle_artifact_id"),
            retirement_request_id=parameters["retirement_request_id"],
            **managed_recipe_inputs(inventory, config),
        )
        if (
            plan.allocation_id != parameters["control_allocation_id"]
            or control.parameters.get("retirement_access_plan_sha256") != plan.revision
        ):
            raise OperationRefused("managed control grant changed since admission")
        validate_access_artifact(artifact, plan)
        if (
            await require_managed_control_source(
                connection,
                plan=plan,
                access_artifact=artifact,
                paid_operation_id=paid_operation_id,
            )
            != source.operation_id
        ):
            raise OperationRefused("managed control producer changed")
    return plan, paid_operation_id
