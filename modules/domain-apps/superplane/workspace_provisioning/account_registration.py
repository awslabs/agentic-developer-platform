"""Read the maintained created-account handoff under the current bootstrap grant.

The history view names the real original creation operation. It has no execution
or settlement method and confers no authority from that completed operation.
Every history/placement read instead checks the separately approved live bootstrap
operation and its immutable link to the original successful creation artifact.
"""

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

from .account_runtime import creation_proof
from .authority import current_operation
from .runtime_config import LifecycleRefused


async def created_account_registration(
    operation, context, request, authorization, row, management
):
    from account_provisioning.creation_runner import creation_key
    from account_provisioning.registration import load_created_account
    from harness_jobs.execution import read_call

    source = await creation_proof(operation, context, row, management, request)
    loop = asyncio.get_running_loop()

    async def history():
        current = await creation_proof(operation, context, row, management, request)
        if current != source:
            raise LifecycleRefused(
                "original account creation artifact changed during registration"
            )
        expected = json.loads(source["artifact_metadata_json"])["creation_call"]
        key = expected["idempotency_key"]
        async with context.connect() as connection:
            call = await read_call(connection, idempotency_key=key)
        if call is None or any(
            getattr(call, field) != value for field, value in expected.items()
        ):
            raise LifecycleRefused(
                "original creation evidence changed during registration"
            )
        await current_operation(operation, context)
        return (replace(call, idempotency_key=creation_key(view)),)

    def verify():
        future = asyncio.run_coroutine_threadsafe(
            current_operation(operation, context), loop
        )
        try:
            return future.result(timeout=20)
        except BaseException:
            future.cancel()
            raise

    class HistoryView:
        operation_id = source["source_operation_id"]
        org_id, workspace_id = source["org_id"], source["workspace_id"]

        async def provider_calls(self, *, provider, operation_kind):
            if (provider, operation_kind) != ("aws-organizations", "create-account"):
                raise LifecycleRefused(
                    "registration history is outside original creation"
                )
            return await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(history(), loop)
            )

    class Organizations:
        def list_parents(self, *, ChildId):
            if ChildId != source["account_id"]:
                raise LifecycleRefused(
                    "registration placement names another child account"
                )
            verify()
            result = management.client("organizations").list_parents(ChildId=ChildId)
            verify()
            return result

    class Credentials:
        async def management(self, *, operation_id):
            # This argument identifies the history being registered. It does not
            # resolve credentials or a grant for that completed operation.
            if operation_id != source["source_operation_id"]:
                raise LifecycleRefused(
                    "registration credentials name another creation source"
                )
            verify()
            return SimpleNamespace(organizations=Organizations())

    view = HistoryView()
    result = await asyncio.to_thread(
        lambda: asyncio.run(
            load_created_account(
                view, Credentials(), request, authorization=authorization
            )
        )
    )
    await current_operation(operation, context)
    return result
