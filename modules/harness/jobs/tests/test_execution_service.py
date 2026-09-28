"""Real PostgreSQL and separate-worker-process evidence for execution authority."""

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from harness_jobs.execution import CallOutcome, read_audit
from harness_jobs.execution_rpc import (
    ExecutionClient,
    ExecutionGrant,
    ExecutionRPCError,
    ExecutionRPCServer,
    admitted_steps,
    step_key,
)
from harness_jobs.identity import OperationRefused, OperationState
from harness_jobs.leases import acquire
from harness_jobs.recovery import sweep_expired_leases
from harness_jobs.schema import apply, downgrade

from .conftest import cancellation_principal, requires_postgres
from .test_executor_recovery_boundaries import expire, prepared
from .test_facade_postgres import FixedResolver, facade

pytestmark = requires_postgres


async def rpc_prepared(connection, *, steps=None):
    from harness_jobs import OperationStore

    from .conftest import admit_paid
    from .test_admission_postgres import principal, request

    steps = (
        steps
        if steps is not None
        else [
            dict(
                step_id="create",
                provider="test",
                operation_kind="create",
                target="target",
            )
        ]
    )
    req = replace(
        request("rpc-plan"), parameters={"execution_steps": json.dumps(steps)}
    )
    admitted = await admit_paid(OperationStore(), connection, principal(), req)
    lease = await acquire(
        connection,
        operation_id=admitted.record.operation_id,
        holder="worker",
        attempt_id="attempt-1",
    )
    return admitted.record, lease


@pytest.mark.parametrize("ceiling", [1, 3, 7])
@pytest.mark.parametrize("recovery_default", [1, 5, 9])
async def test_restart_uses_durable_attempt_ceiling(
    connection, ceiling, recovery_default
):
    from harness_jobs import OperationStore

    from .conftest import admit_paid
    from .test_admission_postgres import principal, request

    admitted = await admit_paid(
        OperationStore(), connection, principal(), request("ceiling")
    )
    lease = await acquire(
        connection,
        operation_id=admitted.record.operation_id,
        holder="worker",
        attempt_id="attempt-1",
        max_attempts=ceiling,
    )
    for attempt in range(1, ceiling + 1):
        await expire(connection, lease.operation_id)
        report = await sweep_expired_leases(connection, max_attempts=recovery_default)
        assert report.results[0].action == (
            "failed" if attempt == ceiling else "retried"
        )
        if attempt < ceiling:
            lease = await acquire(
                connection,
                operation_id=lease.operation_id,
                holder="restart",
                attempt_id=f"attempt-{attempt + 1}",
                max_attempts=recovery_default,
            )
            assert lease.max_attempts == ceiling
    assert await connection.fetchval("SELECT state FROM harness_operations") == "failed"


async def test_v5_upgrade_preserves_attempts_and_establishes_default_policy(connection):
    _, lease = await prepared(connection)
    await downgrade(connection, target=5)
    await apply(connection)
    row = await connection.fetchrow(
        "SELECT attempts, max_attempts FROM harness_operation_leases"
    )
    assert row["attempts"] == lease.attempts and row["max_attempts"] == 5


@pytest.mark.parametrize(
    "method", ["execution_status", "cancel_operation", "report_execution"]
)
@pytest.mark.parametrize(
    "authority", ["foreign-org", "foreign-workspace", "no-permission"]
)
async def test_authenticated_facade_cannot_read_cancel_or_report_other_tenants(
    pool, method, authority
):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)
    principal = cancellation_principal(lease.holder)
    if authority == "foreign-org":
        principal = replace(principal, org_id="foreign")
    elif authority == "foreign-workspace":
        principal = replace(principal, workspace_id="foreign")
    else:
        principal = replace(principal, permissions=frozenset())
    service = facade(pool.acquire, FixedResolver(principal))
    kwargs = (
        dict(
            attempt_id=lease.attempt_id,
            fence_token=lease.fence_token,
            state=OperationState.SUCCEEDED,
        )
        if method == "report_execution"
        else {}
    )
    if authority == "no-permission":
        with pytest.raises(OperationRefused):
            await getattr(service, method)(lease.operation_id, **kwargs)
    else:
        foreign = await getattr(service, method)(lease.operation_id, **kwargs)
        absent = await getattr(service, method)("missing-operation", **kwargs)
        assert foreign == absent
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT state, cancel_requested_at FROM harness_operations"
        )
        assert row["state"] == "pending" and row["cancel_requested_at"] is None


async def test_facade_derives_cancel_actor_and_reports_only_current_executor(pool):
    async with pool.acquire() as connection:
        _, lease = await prepared(connection)
    user = facade(pool.acquire, FixedResolver(cancellation_principal("user:alice")))
    assert not await user.report_execution(
        lease.operation_id,
        attempt_id=lease.attempt_id,
        fence_token=lease.fence_token,
        state=OperationState.SUCCEEDED,
    )
    assert await user.cancel_operation(lease.operation_id, reason="stop")
    assert (await user.execution_status(lease.operation_id)).cancel_requested
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT cancel_requested_by FROM harness_operations"
            )
            == "user:alice"
        )
        events = await read_audit(
            connection,
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
        )
        assert any(
            e["event"] == "cancel.request" and e["actor"] == "user:alice"
            for e in events
        )
    worker = facade(pool.acquire, FixedResolver(cancellation_principal(lease.holder)))
    assert not await worker.report_execution(
        lease.operation_id,
        attempt_id=lease.attempt_id,
        fence_token=lease.fence_token,
        state=OperationState.CANCELLED,
    )


async def test_separate_worker_process_has_only_rpc_and_provider_intent_commits_first(
    pool,
):
    async with pool.acquire() as connection:
        record, lease = await rpc_prepared(connection)
    effects = []

    async def provider(call):
        async with pool.acquire() as observer:
            assert (
                await observer.fetchval(
                    "SELECT stage FROM harness_provider_call_intent "
                    "WHERE idempotency_key=$1",
                    call.idempotency_key,
                )
                == "intended"
            )
        effects.append(call.idempotency_key)
        return CallOutcome.SUCCEEDED, None, "resource-1"

    async def authenticate(token):
        if token != "scoped-test-token":
            raise OperationRefused("invalid")
        return ExecutionGrant(cancellation_principal(lease.holder), lease)

    server = ExecutionRPCServer(
        connect=pool.acquire, provider_call=provider, authenticate=authenticate
    )
    worker_code = """
import asyncio,json,sys,os
from harness_jobs.execution_rpc import ExecutionClient,ExecutionRPCError
async def main():
    client=ExecutionClient(sys.argv[1],"scoped-test-token")
    assert not hasattr(client,"_connect") and not hasattr(client,"_provider_call")
    forbidden=("DATABASE_URL","HARNESS_JOBS_TEST_POSTGRES_URL","AWS_SECRET_ACCESS_KEY")
    assert not any(k in os.environ for k in forbidden)
    denied=0
    attacks=[("_connect",{}),("_provider_call",{}),
             ("settle",{"state":"succeeded","operation_id":"foreign"})]
    for method,args in attacks:
        try: await client.request(method,**args)
        except PermissionError: denied+=1
    assert denied==3
    call=await client.request("execute_step",step_id="create")
    assert call[1]=="settle"
    print(json.dumps({"worker_pid":os.getpid(),"denied":denied}))
asyncio.run(main())
"""
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="execution-rpc-") as temp:
        socket = str(Path(temp) / "service.sock")
        async with server.serve(socket):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                worker_code,
                socket,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                    "PYTHONFAULTHANDLER": "1",
                    # setup-python requires its loader path to select the matching
                    # libpython. Keep runtime paths while excluding credentials.
                    **{
                        key: os.environ[key]
                        for key in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH")
                        if key in os.environ
                    },
                },
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            output, error = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode == 0, error.decode()
            assert json.loads(output)["worker_pid"] != os.getpid()
    assert effects == [step_key(record, admitted_steps(record)[0])]
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval("SELECT state FROM harness_operations")
            == "succeeded"
        )


@pytest.mark.parametrize(
    "failure",
    [
        "invalid-token",
        "revoked-token",
        "foreign-grant",
        "stale-grant",
        "arbitrary-method",
    ],
)
async def test_rpc_refuses_invalid_or_changed_authority_without_provider_effect(
    pool, failure
):
    async with pool.acquire() as connection:
        record, lease = await rpc_prepared(connection)
        if failure == "stale-grant":
            await expire(connection, lease.operation_id)
    effects = []
    revoked = False

    async def authenticate(token):
        if token != "scoped" or revoked:
            raise OperationRefused("credential refused")
        identity = cancellation_principal(lease.holder)
        if failure == "foreign-grant":
            identity = replace(identity, org_id="foreign")
        return ExecutionGrant(identity, lease)

    async def provider(call):
        effects.append(call)
        return CallOutcome.SUCCEEDED, None, None

    server = ExecutionRPCServer(
        connect=pool.acquire, provider_call=provider, authenticate=authenticate
    )
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="execution-rpc-") as temp:
        socket = str(Path(temp) / "service.sock")
        async with server.serve(socket):
            client = ExecutionClient(
                socket, "bad" if failure == "invalid-token" else "scoped"
            )
            if failure == "revoked-token":
                assert (await client.request("status"))[
                    "operation_id"
                ] == lease.operation_id
                revoked = True
            if failure == "arbitrary-method":
                # Bypass the client allowlist: the trusted receiver must reject it too.
                reader, writer = await asyncio.open_unix_connection(socket)
                writer.write(
                    json.dumps(
                        {"token": "scoped", "method": "_connect", "arguments": {}}
                    ).encode()
                    + b"\n"
                )
                await writer.drain()
                assert json.loads(await reader.readline()) == {
                    "ok": False,
                    "error": "refused",
                }
                writer.close()
                await writer.wait_closed()
            else:
                with pytest.raises(ExecutionRPCError):
                    await client.request(
                        "execute_step",
                        step_id="create",
                    )
    assert effects == []
