"""Trusted retirement hook for the maintained allocation-aware execution RPC.

The worker submits only a step ID to ExecutionRPCServer. This hook re-resolves the
original current grant and bootstrap inventory; it never accepts worker-produced
ownership, approval, provider credentials, artifact paths or accounting results.
"""

import asyncio
from dataclasses import asdict

from harness_jobs.execution import CallOutcome
from harness_jobs.execution_plan import admitted_steps, step_key
from harness_jobs.identity import OperationRefused
from harness_jobs.leases import lock_lease
from harness_jobs.store import OperationStore

from .artifacts import digest
from .execution_contract import ExecutionStep
from .retirement_inventory import load_bootstrap_retirement_inventory
from .retirement_plan import (
    AWS,
    BLOCK_ADMISSION,
    DELETE_COMPONENT,
    DRAIN_WORKLOADS,
    GOVERNANCE,
    KUBERNETES,
    REGISTRY,
    REVOKE_GRANT,
    REVOKE_PREREQUISITE,
    UNREGISTER,
    VERIFY_RESOURCES,
    compose_retirement_plan,
)
from .retirement_terraform import ACTION, PROVIDER, ReviewedDestroy


def verify_retirement_inventory(operation, inventory, *, lease=None):
    current_lease = lease if lease is not None else operation.grant.lease
    request = operation.admitted_request() if lease is not None else operation.request
    if (inventory.workspace_id, inventory.org_id) != (
        current_lease.workspace_id,
        current_lease.org_id,
    ) or request.parameters.get("retirement_inventory_sha256") != digest(
        asdict(inventory)
    ):
        raise OperationRefused(
            "retirement ownership differs from the approved inventory"
        )


class RetirementRuntime:
    def __init__(
        self,
        *,
        connect,
        context,
        registration_store,
        removals,
        lifecycle,
        verify_inventory,
        artifact_for=None,
        terraform=None,
        domain_connect=None,
        control_access_for=None,
        control_verify=None,
    ):
        self.connect, self.context, self.registration_store = (
            connect,
            context,
            registration_store,
        )
        self.removals, self.lifecycle = removals, lifecycle
        (
            self.verify_inventory,
            self.artifact_for,
            self.terraform,
            self.domain_connect,
        ) = (
            verify_inventory,
            artifact_for,
            terraform,
            domain_connect,
        )
        self.control_access_for = control_access_for
        self.control_verify = control_verify

    async def __call__(self, call):
        operation, binding = await self.context(call)
        lease = operation.grant.lease
        if (
            operation.request.action != "teardown"
            or not getattr(operation, "job_id", None)
            or operation.job_id != getattr(call, "job_id", None)
            or any(
                getattr(lease, key) != getattr(call, key)
                for key in (
                    "operation_id",
                    "org_id",
                    "workspace_id",
                    "attempt_id",
                    "fence_token",
                )
            )
        ):
            raise OperationRefused("retirement call differs from its admitted attempt")
        parameters = operation.request.parameters
        if not parameters.get("allocation_id") or parameters.get(
            "allocation_id"
        ) != parameters.get("original_allocation_id"):
            raise OperationRefused("retirement must retain its original allocation")
        destroying = False

        async def authorize():
            current, current_binding = await self.context(call)
            if (
                current_binding != binding
                or current.request != operation.request
                or current.job_id != operation.job_id
                or current.plan_digest != operation.plan_digest
                or current.request_payload != operation.request_payload
                or any(
                    getattr(current.grant.lease, key) != getattr(lease, key)
                    for key in (
                        "operation_id",
                        "org_id",
                        "workspace_id",
                        "holder",
                        "attempt_id",
                        "fence_token",
                    )
                )
            ):
                raise OperationRefused("retirement authority changed")
            if destroying and (
                self.lifecycle.managed_fence is None
                or not await self.lifecycle.managed_fence(current, inventory)
            ):
                raise OperationRefused("managed cluster admission interlock changed")
            async with self.connect() as connection, connection.transaction():
                if not await lock_lease(connection, current.grant.lease):
                    raise OperationRefused("retirement lease expired")
                cancelled = await connection.fetchval(
                    "SELECT cancel_requested_at IS NOT NULL FROM harness_operations WHERE operation_id=$1",
                    lease.operation_id,
                )
                if cancelled is not False:
                    raise OperationRefused("retirement cancellation requested")

        await authorize()
        inventory = await asyncio.to_thread(
            load_bootstrap_retirement_inventory,
            registration_store=self.registration_store,
            binding=binding,
        )
        verify_retirement_inventory(operation, inventory)
        plan = compose_retirement_plan(inventory)
        if inventory.preserve_cluster and plan.cluster_rbac_remaining:
            raise OperationRefused(
                "adopted retirement requires independent exact-name cleanup authority for retained cluster RBAC"
            )
        artifact = (
            await self.artifact_for(operation, inventory) if self.artifact_for else None
        )
        access = None
        if self.control_access_for is not None:
            from .artifacts import read_artifact
            from .retirement_managed_access import (
                ManagedRetirementAccessPlan,
                require_managed_control_source,
            )

            if self.domain_connect is None or self.control_verify is None:
                raise OperationRefused("managed control provider is not composed")
            control_plan, paid_operation_id = await self.control_access_for(
                operation, inventory
            )
            if (
                not isinstance(control_plan, ManagedRetirementAccessPlan)
                or not parameters.get("retirement_access_artifact_id")
                or parameters.get("control_allocation_id") != control_plan.allocation_id
                or parameters.get("retirement_request_id")
                != control_plan.retirement_request_id
                or parameters["original_allocation_id"]
                != control_plan.original_allocation_id
            ):
                raise OperationRefused("managed control allocation or receipt changed")
            access_row = await read_artifact(
                self.domain_connect,
                artifact_id=parameters["retirement_access_artifact_id"],
                org_id=lease.org_id,
                workspace_id=lease.workspace_id,
                require_fresh=False,
            )
            async with self.connect() as connection:
                await require_managed_control_source(
                    connection,
                    plan=control_plan,
                    access_artifact=access_row,
                    paid_operation_id=paid_operation_id,
                )
            access = control_plan, access_row, paid_operation_id
        elif parameters.get("retirement_access_artifact_id"):
            raise OperationRefused("managed control provider is unavailable")
        plan = compose_retirement_plan(
            inventory,
            managed_destroy=artifact,
            managed_access=access[:2] if access is not None else None,
        )
        async with self.connect() as connection:
            record = await OperationStore().get(
                connection, operation.grant.principal, lease.operation_id
            )
        if record is None or record.plan_digest != operation.plan_digest:
            raise OperationRefused("retirement admission unavailable")
        steps = admitted_steps(record)
        if [(s.step_id, s.provider, s.operation_kind, s.target) for s in steps] != [
            (s.step_id, s.provider, s.operation_kind, s.target) for s in plan.steps
        ]:
            raise OperationRefused(
                "durable retirement plan differs from approved deletion set"
            )
        matched = [
            step for step in steps if step_key(record, step) == call.idempotency_key
        ]
        if len(matched) != 1 or (
            matched[0].provider,
            matched[0].operation_kind,
            matched[0].target,
        ) != (call.provider, call.operation_kind, call.target):
            raise OperationRefused("retirement provider call is not an admitted step")
        step = ExecutionStep(
            matched[0].step_id, call.provider, call.operation_kind, call.target
        )
        await authorize()
        action = (step.provider, step.operation_kind)
        if action == (GOVERNANCE, BLOCK_ADMISSION):
            return await self.lifecycle.status(
                operation, inventory, "Teardown", authorize
            )
        if action == (GOVERNANCE, DRAIN_WORKLOADS):
            return await self.lifecycle.drain(operation, inventory, authorize)
        if action == (REGISTRY, UNREGISTER):
            return await self.lifecycle.status(
                operation, inventory, "retired", authorize
            )
        if step.step_id == "revoke-control-entry":
            from harness_jobs.execution import CallStage, read_call
            from superplane_bootstrap.eks_grants import EksGrants

            from .retirement_adapters import OwnedResourceRemover
            from .retirement_managed_access import require_managed_control_source

            if (
                action != (AWS, REVOKE_GRANT)
                or access is None
                or not isinstance(self.removals, OwnedResourceRemover)
                or not isinstance(self.removals.eks, EksGrants)
            ):
                raise OperationRefused("managed control revoke has no pinned provider")
            destroying = True

            async def authorize_control():
                await authorize()
                async with self.connect() as connection:
                    await require_managed_control_source(
                        connection,
                        plan=access[0],
                        access_artifact=access[1],
                        paid_operation_id=access[2],
                    )
                await self.control_verify(operation, inventory, access[0], access[1])
                await authorize()

            await authorize_control()
            async with self.connect() as connection, connection.transaction():
                if not await lock_lease(connection, lease):
                    raise OperationRefused("managed revoke lease expired")
                outer = await read_call(
                    connection, idempotency_key=call.idempotency_key
                )
                if (
                    outer is None
                    or outer.stage is not CallStage.INTENDED
                    or (
                        outer.operation_id,
                        outer.org_id,
                        outer.workspace_id,
                        outer.job_id,
                        outer.attempt_id,
                        outer.fence_token,
                        outer.provider,
                        outer.operation_kind,
                        outer.target,
                    )
                    != (
                        lease.operation_id,
                        lease.org_id,
                        lease.workspace_id,
                        operation.job_id,
                        lease.attempt_id,
                        lease.fence_token,
                        step.provider,
                        step.operation_kind,
                        step.target,
                    )
                ):
                    raise OperationRefused(
                        "managed revoke has no original outer intent"
                    )
            await authorize_control()
            result = await asyncio.to_thread(
                self.removals.revoke_control_grant, access[0], access[1]
            )
            await authorize_control()
            return result
        if action in {
            (KUBERNETES, DELETE_COMPONENT),
            (KUBERNETES, REVOKE_GRANT),
            (AWS, REVOKE_GRANT),
            (AWS, REVOKE_PREREQUISITE),
        }:
            result = await asyncio.to_thread(self.removals.execute, step, inventory)
            await authorize()
            return result
        if (
            action == (PROVIDER, ACTION)
            and artifact is not None
            and self.terraform is not None
        ):
            if not await self.lifecycle.managed_destroy_ready(
                operation, inventory, authorize
            ):
                return (
                    CallOutcome.UNKNOWN,
                    "managed workload or storage ownership remains unverified",
                    None,
                )
            destroying = True
            return await self.terraform.execute(
                artifact, inventory, parameters, authorize
            )
        if action == (AWS, VERIFY_RESOURCES):
            # Must perform fresh provider enumeration and maintained allocation
            # finalization. A call RELEASE alone never releases the allocation.
            return await self.verify_inventory(operation, inventory, authorize)
        raise OperationRefused("retirement action has no configured provider adapter")


class RetirementRecoveryObserver:
    """Observe an original deletion under a new, authenticated recovery claim."""

    def __init__(self, *, connect, principal, resolve):
        from harness_jobs.identity import ResolvedPrincipal

        if (
            not isinstance(principal, ResolvedPrincipal)
            or "workspace:recover" not in principal.permissions
        ):
            raise OperationRefused("retirement recovery requires current scope")
        self.connect, self.principal, self.resolve = connect, principal, resolve

    async def __call__(self, lease, key, provider, operation_kind, target):
        from harness_jobs.execution import CallStage, read_call
        from harness_jobs.recovery_grant import RecoveryGrant, lock_recovery_grant

        from .retirement_adapters import OwnedResourceRemover

        grant = RecoveryGrant(self.principal, lease)
        async with self.connect() as connection, connection.transaction():
            if not await lock_recovery_grant(connection, grant):
                raise OperationRefused("retirement recovery claim changed")
            call = await read_call(connection, idempotency_key=key)
            record = await OperationStore().get(
                connection, self.principal, lease.operation_id
            )
            if (
                call.stage is not CallStage.INTENDED
                or call.operation_id != lease.operation_id
                or (call.org_id, call.workspace_id)
                != (lease.org_id, lease.workspace_id)
                or (call.provider, call.operation_kind, call.target)
                != (provider, operation_kind, target)
                or record is None
                or record.action != "teardown"
                or record.job_id != call.job_id
                or call.fence_token >= lease.fence_token
                or not record.admitted_request().parameters.get("allocation_id")
                or record.admitted_request().parameters["allocation_id"]
                != record.admitted_request().parameters.get("original_allocation_id")
            ):
                raise OperationRefused("retirement recovery call changed")
            matched = [
                step
                for step in admitted_steps(record)
                if step_key(record, step) == key
                and (step.provider, step.operation_kind, step.target)
                == (provider, operation_kind, target)
            ]
            if len(matched) != 1:
                raise OperationRefused("retirement recovery step was not approved")
        inventory, removals, artifact = await self.resolve(grant, record)
        if not isinstance(removals, OwnedResourceRemover):
            raise OperationRefused("retirement recovery has no scoped provider reads")
        verify_retirement_inventory(record, inventory, lease=grant.lease)
        if artifact is not None:
            if not isinstance(artifact, ReviewedDestroy):
                raise OperationRefused("retirement recovery has no reviewed destroy")
            await asyncio.to_thread(
                artifact.read, inventory, record.admitted_request().parameters
            )
        if [
            (step.step_id, step.provider, step.operation_kind, step.target)
            for step in compose_retirement_plan(
                inventory, managed_destroy=artifact
            ).steps
        ] != [
            (step.step_id, step.provider, step.operation_kind, step.target)
            for step in admitted_steps(record)
        ]:
            raise OperationRefused("retirement recovery inventory changed")
        step = matched[0]
        if (step.provider, step.operation_kind) in {
            (KUBERNETES, DELETE_COMPONENT),
            (KUBERNETES, REVOKE_GRANT),
            (AWS, REVOKE_GRANT),
            (AWS, REVOKE_PREREQUISITE),
        }:
            admitted = ExecutionStep(
                step.step_id, step.provider, step.operation_kind, step.target
            )
            result = await asyncio.to_thread(removals.observe, admitted, inventory)
        else:
            result = CallOutcome.UNKNOWN, "no authoritative absence observation", None
        async with self.connect() as connection, connection.transaction():
            if not await lock_recovery_grant(connection, grant):
                raise OperationRefused("retirement recovery claim expired")
        return result
