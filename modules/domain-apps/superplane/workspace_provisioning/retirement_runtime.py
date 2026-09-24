"""Trusted retirement hook for the maintained allocation-aware execution RPC.

The worker submits only a step ID to ExecutionRPCServer. This hook re-resolves the
original current grant and bootstrap inventory; it never accepts worker-produced
ownership, approval, provider credentials, artifact paths or accounting results.
"""

import asyncio
from dataclasses import asdict

from harness_jobs.execution_plan import admitted_steps, step_key
from harness_jobs.execution import CallOutcome
from harness_jobs.identity import OperationRefused
from harness_jobs.leases import lock_lease
from harness_jobs.store import OperationStore

from .execution_contract import ExecutionStep
from .artifacts import digest
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
from .retirement_terraform import ACTION, PROVIDER


def verify_retirement_inventory(operation, inventory):
    lease = operation.grant.lease
    if (inventory.workspace_id, inventory.org_id) != (
        lease.workspace_id,
        lease.org_id,
    ) or operation.request.parameters.get("retirement_inventory_sha256") != digest(
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
    ):
        self.connect, self.context, self.registration_store = (
            connect,
            context,
            registration_store,
        )
        self.removals, self.lifecycle = removals, lifecycle
        self.verify_inventory, self.artifact_for, self.terraform = (
            verify_inventory,
            artifact_for,
            terraform,
        )

    async def __call__(self, call):
        operation, binding = await self.context(call)
        lease = operation.grant.lease
        if operation.request.action != "teardown" or any(
            getattr(lease, key) != getattr(call, key)
            for key in (
                "operation_id",
                "org_id",
                "workspace_id",
                "attempt_id",
                "fence_token",
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
            async with self.connect() as connection:
                async with connection.transaction():
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
        if artifact is not None:
            plan = compose_retirement_plan(inventory, managed_destroy=artifact)
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
