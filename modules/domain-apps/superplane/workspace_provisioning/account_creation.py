"""Governed account producer composed with the original admitted shared RPC step.

Not activated by run_lifecycle. The maintained producer's logical generation-zero
key aliases exactly one real RPC key; provider, target, admission and observations
are never replaced. An uncertain call cannot create a second generation here.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

from .artifacts import record_artifact
from .authority import current_operation
from .runtime_config import LifecycleRefused


class CreationExecution:
    """Private logical-key adapter; the shared journal remains authoritative."""

    def __init__(self, operation, context, request, runtime, server, publish):
        self.operation, self.context, self.request = operation, context, request
        self.runtime, self.server, self.publish = runtime, server, publish
        lease = operation.grant.lease
        self.operation_id = lease.operation_id
        self.org_id, self.workspace_id = lease.org_id, lease.workspace_id

    async def binding(self):
        from account_provisioning.creation_runner import creation_target
        from harness_jobs.execution_plan import admitted_steps, step_key
        from harness_jobs.leases import lock_lease
        from harness_jobs.store import OperationStore

        await current_operation(self.operation, self.context)
        async with self.context.connect() as connection, connection.transaction():
            if not await lock_lease(connection, self.operation.grant.lease):
                raise LifecycleRefused("account producer lease is no longer live")
            record = await OperationStore().get(
                connection, self.operation.grant.principal, self.operation_id
            )
            if (
                record is None
                or record.plan_digest != self.operation.plan_digest
                or record.request_payload != self.operation.request_payload
                or (record.org_id, record.workspace_id, record.job_id)
                != (self.org_id, self.workspace_id, self.operation.job_id)
            ):
                raise LifecycleRefused("account producer original admission changed")
            steps = admitted_steps(record)
            if len(steps) != 1 or (
                steps[0].step_id,
                steps[0].provider,
                steps[0].operation_kind,
                steps[0].target,
            ) != (
                "create-account",
                "aws-organizations",
                "create-account",
                creation_target(self.request),
            ):
                raise LifecycleRefused(
                    "account producer differs from its admitted descriptor"
                )
            return steps[0], step_key(record, steps[0])

    async def original(self):
        from harness_jobs.execution import read_call

        step, actual_key = await self.binding()
        async with self.context.connect() as connection:
            keys = await connection.fetch(
                "SELECT idempotency_key FROM harness_provider_call_intent WHERE operation_id=$1",
                self.operation_id,
            )
            if keys and [row["idempotency_key"] for row in keys] != [actual_key]:
                raise LifecycleRefused("account phase has an unapproved provider call")
            call = await read_call(connection, idempotency_key=actual_key)
        if call is not None and (
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
            self.operation_id,
            self.org_id,
            self.workspace_id,
            self.operation.job_id,
            self.operation.grant.lease.attempt_id,
            self.operation.grant.lease.fence_token,
            step.provider,
            step.operation_kind,
            step.target,
        ):
            raise LifecycleRefused("account producer call provenance changed")
        await current_operation(self.operation, self.context)
        return call

    def logical(self, call):
        from account_provisioning.creation_runner import creation_key

        return replace(call, idempotency_key=creation_key(self))

    async def provider_calls(self, *, provider, operation_kind):
        if (provider, operation_kind) != ("aws-organizations", "create-account"):
            raise LifecycleRefused(
                "account producer history query is outside its phase"
            )
        call = await self.original()
        return () if call is None else (self.logical(call),)

    async def execute_provider(
        self, *, idempotency_key, provider, operation_kind, target
    ):
        from account_provisioning.creation_runner import creation_key

        step, _ = await self.binding()
        if (idempotency_key, provider, operation_kind, target) != (
            creation_key(self),
            step.provider,
            step.operation_kind,
            step.target,
        ):
            raise LifecycleRefused(
                "account producer cannot add a key or retry generation"
            )
        if await self.original() is not None:
            raise LifecycleRefused(
                "recorded account creation requires observation, never redispatch"
            )
        call, disposition = await self.server.execute_step(
            self.operation.grant, self.runtime, step.step_id
        )
        # RPC may have settled the operation. Its returned durable record is the
        # only result here; querying current execution authority after settlement
        # would incorrectly refuse an already successful producer.
        return self.logical(call), disposition

    async def reconcile(self, *, idempotency_key, outcome, detail, provider_ref):
        from account_provisioning.creation_runner import creation_key, _decode_reference
        from harness_jobs.execution import CallOutcome, CallStage

        call = await self.original()
        if (
            idempotency_key != creation_key(self)
            or call is None
            or call.stage is not CallStage.INTENDED
            or outcome not in (CallOutcome.SUCCEEDED, CallOutcome.FAILED)
        ):
            raise LifecycleRefused(
                "account observation cannot replace settled or uncertain evidence"
            )
        if (
            _decode_reference(call.provider_ref)[2] is None
            or _decode_reference(call.provider_ref)[2]
            != _decode_reference(provider_ref)[2]
        ):
            raise LifecycleRefused("account observation names another creation request")
        if outcome is CallOutcome.SUCCEEDED:
            await self.publish(call, provider_ref)
        await current_operation(self.operation, self.context)
        return await self.runtime.observe(
            idempotency_key=call.idempotency_key,
            outcome=outcome,
            detail=detail,
            provider_ref=provider_ref,
        )


class ProducerThread:
    """Run maintained synchronous SDK ports off the lease-renewal event loop."""

    def __init__(self, operation, context, session, loop):
        self.operation, self.context, self.session, self.loop = (
            operation,
            context,
            session,
            loop,
        )

    async def bridge(self, coroutine):
        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        )

    def verify(self):
        future = asyncio.run_coroutine_threadsafe(
            current_operation(self.operation, self.context), self.loop
        )
        try:
            return future.result(timeout=20)
        except BaseException:
            future.cancel()
            raise

    def credentials(self):
        thread = self

        class Organizations:
            def __getattr__(self, name):
                if name not in {
                    "create_account",
                    "describe_create_account_status",
                    "list_roots",
                    "list_organizational_units_for_parent",
                    "list_parents",
                }:
                    raise LifecycleRefused(
                        "account producer SDK method is outside its recipe"
                    )
                method = getattr(thread.session.client("organizations"), name)

                def call(**arguments):
                    from account_provisioning.ports import ProviderUnavailable

                    thread.verify()
                    try:
                        result = method(**arguments)
                    except Exception as error:
                        # Never infer authoritative absence from an SDK exception.
                        # Lost answers remain uncertain in the maintained hook.
                        raise ProviderUnavailable(
                            "Organizations response was unavailable"
                        ) from error
                    if name != "create_account":
                        thread.verify()
                    return result

                return call

        class Credentials:
            async def management(self, *, operation_id):
                if operation_id != thread.operation.grant.lease.operation_id:
                    raise LifecycleRefused(
                        "management credentials require the original operation"
                    )
                thread.verify()
                return SimpleNamespace(organizations=Organizations())

            async def child_account(self, **kwargs):
                raise LifecycleRefused(
                    "account creation cannot obtain child credentials"
                )

        return Credentials()

    def executor(self, adapter):
        thread = self

        class Executor:
            operation_id = adapter.operation_id
            org_id, workspace_id = adapter.org_id, adapter.workspace_id

            async def provider_calls(self, **kwargs):
                return await thread.bridge(adapter.provider_calls(**kwargs))

            async def execute_provider(self, **kwargs):
                return await thread.bridge(adapter.execute_provider(**kwargs))

            async def reconcile(self, **kwargs):
                return await thread.bridge(adapter.reconcile(**kwargs))

        return Executor()


async def publish_created_account(
    operation, context, request, session, call, reference
):
    """Publish immutable child identity while the original creation grant is live."""
    from account_provisioning.creation_runner import _decode_reference
    from .account_runtime import read_sdk

    account, failure, request_id = _decode_reference(reference)
    if (
        not account
        or account == request.management_account_id
        or failure is not None
        or not request_id
    ):
        raise LifecycleRefused(
            "creation result does not identify a distinct child account"
        )
    observed = await read_sdk(
        operation,
        context,
        session.client("organizations").describe_create_account_status,
        CreateAccountRequestId=request_id,
    )
    status = observed.get("CreateAccountStatus", {})
    if (
        status.get("Id"),
        status.get("State"),
        status.get("AccountId"),
        status.get("AccountName"),
    ) != (request_id, "SUCCEEDED", account, "adp-" + request.workspace_id):
        raise LifecycleRefused(
            "account handoff differs from the original creation request"
        )
    response = await read_sdk(
        operation,
        context,
        session.client("organizations").list_parents,
        ChildId=account,
    )
    parents = response.get("Parents", [])
    if (
        response.get("NextToken")
        or len(parents) != 1
        or parents[0].get("Type") != "ROOT"
        or not parents[0].get("Id")
    ):
        raise LifecycleRefused(
            "new child account is not at its observed organization root"
        )
    return await record_artifact(
        operation,
        context,
        account_id=account,
        target={
            "account_id": account,
            "aws_region": request.region,
            "organizational_unit_id": request.organizational_unit_id,
        },
        metadata={
            "next_phase": "bootstrap-account",
            "creation_request_id": request_id,
            "creation_source_parent_id": parents[0]["Id"],
            "creation_call": {
                "idempotency_key": call.idempotency_key,
                "provider": call.provider,
                "operation_kind": call.operation_kind,
                "target": call.target,
                "provider_ref": reference,
            },
        },
    )


async def run_account_creation(operation, context):
    """One original creation admission, with maintained readback of its request ID.

    This private entry point is deliberately separate from public mode activation.
    Expired/lost authority requires a separately authenticated recovery composer.
    """
    from account_provisioning.creation_runner import (
        creation_hook,
        reconcile_creation,
        _settled_outcome,
    )
    from account_provisioning.registration import (
        create_from_durable_history,
        creation_history,
    )
    from harness_jobs.execution import CallOutcome, OperationExecutor
    from harness_jobs.execution_rpc import ExecutionRPCServer
    from .account_runtime import creation_preflight
    from .runtime import delivery_session, validate_phase

    config, request, authorization, row, step = await validate_phase(operation, context)
    if (
        request.mode.value != "new-account-managed"
        or row is not None
        or step.step_id != "create-account"
    ):
        raise LifecycleRefused(
            "account producer requires its initial creation admission"
        )
    # The maintained hook uses AWS's documented default; a config that claims a
    # different bootstrap role must fail before any authority is delivered.
    if (
        config.get("new_account", {}).get("child_access_role_name")
        != "OrganizationAccountAccessRole"
    ):
        raise LifecycleRefused(
            "maintained creation requires OrganizationAccountAccessRole"
        )
    session = await delivery_session(
        operation, context, request.management_account_id, request.region
    )
    await creation_preflight(
        operation, context, config, request, authorization, session
    )
    loop, result = asyncio.get_running_loop(), {}
    thread = ProducerThread(operation, context, session, loop)

    async def publish(call, reference):
        result.update(
            await publish_created_account(
                operation, context, request, session, call, reference
            )
        )

    async def hook(call):
        original = await adapter.original()
        if original != call:
            raise LifecycleRefused(
                "creation hook did not receive the original shared intent"
            )

        def invoke():
            return asyncio.run(
                creation_hook(thread.credentials(), request, outcomes=CallOutcome)(call)
            )

        outcome, detail, reference = await asyncio.to_thread(invoke)
        if outcome is CallOutcome.SUCCEEDED:
            try:
                await publish(call, reference)
            except Exception:
                # Even a handoff read/write failure must not discard the only
                # returned AWS request handle. The original intent stays open.
                return (
                    CallOutcome.UNKNOWN,
                    "account handoff requires observation",
                    reference,
                )
        return outcome, detail, reference

    async def authenticate(_token):
        return (await current_operation(operation, context)).grant

    runtime = OperationExecutor(
        operation.grant.lease, connect=context.connect, provider_call=hook
    )
    server = ExecutionRPCServer(
        connect=context.connect, provider_call=hook, authenticate=authenticate
    )
    adapter = CreationExecution(operation, context, request, runtime, server, publish)
    executor = thread.executor(adapter)

    def produce():
        return asyncio.run(
            create_from_durable_history(
                executor, thread.credentials(), request, authorization=authorization
            )
        )

    async def heartbeat():
        active = runtime
        while True:
            await asyncio.sleep(10)
            await current_operation(operation, context)
            active = await active.renew(duration=timedelta(seconds=45))

    async with asyncio.TaskGroup() as tasks:
        renewal = tasks.create_task(heartbeat())
        try:
            existing = await adapter.provider_calls(
                provider="aws-organizations", operation_kind="create-account"
            )
            if existing:
                # A live-grant resume may only observe the original request; it
                # cannot spend a previously failed or uncertain call on a retry.
                outcome = _settled_outcome(existing[0])
            else:
                outcome = await asyncio.to_thread(produce)
            # A bounded observation period cannot become an implicit creation retry.
            for _ in range(30):
                if outcome.succeeded or not outcome.create_account_request_id:
                    break

                async def observe():
                    (recorded,) = await creation_history(executor, request)
                    return await reconcile_creation(
                        executor,
                        thread.credentials(),
                        recorded=recorded,
                        store=executor,
                        outcomes=CallOutcome,
                    )

                outcome = await asyncio.to_thread(lambda: asyncio.run(observe()))
                if outcome.succeeded or outcome.failure is not None:
                    break
                await asyncio.sleep(2)
            if not outcome.succeeded:
                raise LifecycleRefused(
                    "account creation remains retained for original-request observation"
                )
            if not result:
                result.update(
                    await reload_created_account(
                        operation, context, await adapter.original()
                    )
                )
            # The initial synchronous-success path already settled through RPC;
            # a later readback needs RPC's normal successful-plan settlement.
            async with context.connect() as connection:
                state = await connection.fetchval(
                    "SELECT state FROM harness_operations WHERE operation_id=$1",
                    operation.grant.lease.operation_id,
                )
            if state != "succeeded":
                await server.execute_step(operation.grant, runtime, step.step_id)
        finally:
            renewal.cancel()
    return result


async def reload_created_account(operation, context, call):
    """Resume the artifact-written/call-observed/operation-not-settled window."""
    from account_provisioning.creation_runner import _decode_reference
    from harness_jobs.execution import CallOutcome
    from .artifacts import proposal, read_artifact

    await current_operation(operation, context)
    if call is None or call.outcome is not CallOutcome.SUCCEEDED:
        raise LifecycleRefused("created account has no original successful call")
    lease = operation.grant.lease
    async with context.domain_connect() as connection:
        rows = await connection.fetch(
            "SELECT artifact_id FROM workspace_lifecycle_artifacts "
            "WHERE source_operation_id=$1 AND org_id=$2 AND workspace_id=$3",
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
        )
    if len(rows) != 1:
        raise LifecycleRefused("created account has no unique immutable handoff")
    row = await read_artifact(
        context.domain_connect,
        artifact_id=rows[0]["artifact_id"],
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        require_fresh=False,
    )
    metadata = json.loads(row["artifact_metadata_json"])
    expected = {
        key: getattr(call, key)
        for key in (
            "idempotency_key",
            "provider",
            "operation_kind",
            "target",
            "provider_ref",
        )
    }
    if (
        metadata.get("creation_call") != expected
        or metadata.get("next_phase") != "bootstrap-account"
        or _decode_reference(call.provider_ref)
        != (row["account_id"], None, metadata.get("creation_request_id"))
        or (
            row["source_operation_id"],
            row["source_job_id"],
            row["producer_attempt_id"],
            row["producer_fence_token"],
            row["source_payload_digest"],
            row["source_request_payload"],
        )
        != (
            lease.operation_id,
            operation.job_id,
            lease.attempt_id,
            lease.fence_token,
            operation.plan_digest,
            operation.request_payload,
        )
    ):
        raise LifecycleRefused(
            "recorded creation handoff differs from its original successful call"
        )
    await current_operation(operation, context)
    return proposal(row)
