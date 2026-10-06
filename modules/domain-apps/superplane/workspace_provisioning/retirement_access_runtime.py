"""Execute one separately registered, approved temporary cleanup-access phase."""

import asyncio
import threading
from datetime import timedelta

from .artifacts import digest, read_artifact
from .process import AsyncBridgeStore
from .retirement_access_artifact import access_result, record_access_artifact
from .retirement_access_clients import access_delivery_session, build_access_clients
from .retirement_access_context import (
    RetirementAccessEffects,
    current_access_operation,
    load_access_context,
)
from .retirement_access_grants import establish_access_grants
from .retirement_access_plan import PHASE
from .runtime_config import LifecycleRefused
from .terraform import operation_directory


def check_access_call(call, operation):
    lease = operation.grant.lease
    if (
        call.operation_id,
        call.org_id,
        call.workspace_id,
        call.job_id,
        call.attempt_id,
        call.fence_token,
        call.provider,
        call.operation_kind,
        call.target,
    ) != (
        lease.operation_id,
        lease.org_id,
        lease.workspace_id,
        operation.job_id,
        lease.attempt_id,
        lease.fence_token,
        "superplane-lifecycle",
        PHASE,
        operation.request.parameters["retirement_access_recipe_sha256"],
    ):
        raise LifecycleRefused("provider call differs from its admitted cleanup phase")


async def run_retirement_access(operation, context):
    """No workspace bootstrap pointer change and no implicit final retirement.

    Worker selection must already validate the dedicated control registration.
    The independently admitted access allocation retains every new grant until
    final retirement proves their absence and settles it separately.
    """
    from harness_jobs.execution import CallOutcome, OperationExecutor
    from harness_jobs.execution_rpc import ExecutionRPCServer
    from superplane_bootstrap.registry import SqlRegistrationStore

    loop = asyncio.get_running_loop()
    registration_store = SqlRegistrationStore(
        AsyncBridgeStore(context.domain_connect, loop)
    )
    facts = await load_access_context(operation, context, registration_store)
    operation, lease = facts.operation, facts.operation.grant.lease
    effects = RetirementAccessEffects(
        operation, context, registration_store=registration_store, plan=facts.plan
    )
    revoked = threading.Event()
    result = {}

    def verify():
        if revoked.is_set():
            raise LifecycleRefused("cleanup access worker lost execution authority")
        pending = asyncio.run_coroutine_threadsafe(effects.authority(), loop)
        try:
            return pending.result(timeout=30)
        except BaseException:
            pending.cancel()
            raise

    async def hook(call):
        check_access_call(call, operation)
        await effects.authority()
        session = await access_delivery_session(
            operation,
            context,
            operation.request.parameters["aws_account_id"],
            operation.request.parameters["region"],
        )
        directory = operation_directory(
            context.state_root,
            lease.org_id,
            lease.workspace_id,
            lease.operation_id,
            create=True,
        ) / ("access-" + digest([lease.attempt_id, lease.fence_token])[:24])
        directory.mkdir(mode=0o700)
        clients = await asyncio.to_thread(
            build_access_clients, facts, session, directory, verify
        )
        try:
            from .retirement_managed_access import (
                ManagedRetirementAccessPlan,
                compile_managed_access_plan,
                managed_recipe_inputs,
            )

            if isinstance(facts.plan, ManagedRetirementAccessPlan):
                await clients.verify_target()
                actual = await asyncio.to_thread(
                    compile_managed_access_plan,
                    facts.inventory,
                    facts.config,
                    original_allocation_id=facts.plan.original_allocation_id,
                    bootstrap_artifact_id=facts.artifact["artifact_id"],
                    retirement_request_id=facts.plan.retirement_request_id,
                    kubernetes=clients.supervisor,
                    eks=clients.eks,
                    **managed_recipe_inputs(facts.inventory, facts.config),
                )
                if actual != facts.plan:
                    raise LifecycleRefused(
                        "live cleanup grant identities differ from the approved plan"
                    )
            identities = await establish_access_grants(
                facts.plan,
                effects,
                eks=clients.eks,
                kubernetes=clients.kubernetes,
                verify_target=clients.verify_target,
            )
            await clients.verify_target()
            result.update(await record_access_artifact(facts, effects, identities))
        finally:
            await asyncio.to_thread(clients.close)
        return (
            CallOutcome.SUCCEEDED,
            "temporary cleanup grants recorded; retirement and both allocations remain outstanding",
            result["retirement_access_artifact_id"],
        )

    async def authenticate(_token):
        return (await current_access_operation(operation, context)).grant

    server = ExecutionRPCServer(
        connect=context.connect, provider_call=hook, authenticate=authenticate
    )
    runtime = OperationExecutor(lease, connect=context.connect, provider_call=hook)

    async def heartbeat():
        active = runtime
        while True:
            await asyncio.sleep(10)
            try:
                await current_access_operation(operation, context)
                active = await active.renew(duration=timedelta(seconds=45))
            except BaseException:
                revoked.set()
                raise

    async with asyncio.TaskGroup() as tasks:
        renewal = tasks.create_task(heartbeat())
        try:
            await server.execute_step(operation.grant, runtime, PHASE)
        finally:
            revoked.set()
            renewal.cancel()
    if not result:
        # An already completed outer intent can only return its immutable result.
        # It must not recreate grants or invent a replacement access allocation.
        async with context.domain_connect() as connection:
            rows = await connection.fetch(
                "SELECT artifact_id FROM workspace_lifecycle_artifacts WHERE source_operation_id=$1 AND org_id=$2 AND workspace_id=$3",
                lease.operation_id,
                lease.org_id,
                lease.workspace_id,
            )
        if len(rows) != 1:
            raise LifecycleRefused(
                "completed cleanup phase has no unique immutable result"
            )
        row = await read_artifact(
            context.domain_connect,
            artifact_id=rows[0]["artifact_id"],
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
            require_fresh=False,
        )
        if (
            row["source_operation_id"] != lease.operation_id
            or row["source_payload_digest"] != operation.plan_digest
        ):
            raise LifecycleRefused("cleanup result belongs to a different admission")
        result.update(access_result(row, facts.plan))
    return result
