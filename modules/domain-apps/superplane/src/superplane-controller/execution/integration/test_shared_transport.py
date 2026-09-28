"""Go worker against the real shared RPC service and PostgreSQL, on offline CI.

The provider hook is a test double; admission, intent, fencing, cancellation,
budget dispositions and cross-process transport use the maintained implementation.
This is not SkyPilot/EKS or deployment acceptance.
"""

import asyncio
import json
import os
import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import pytest
from harness_jobs.execution import CallOutcome
from harness_jobs.execution_rpc import ExecutionGrant, ExecutionRPCServer
from harness_jobs.identity import OperationRefused
from harness_jobs.recovery import request_cancellation

from tests.conftest import cancellation_principal, requires_postgres
from tests.test_execution_service import rpc_prepared
from tests.test_executor_recovery_boundaries import expire

pytestmark = requires_postgres


@pytest.fixture(scope="session")
def worker_binary():
    binary = Path(os.environ["SUPERPLANE_TEST_RPC_BINARY"])
    assert binary.is_absolute() and binary.is_file(), "compiled Go test worker required"
    return str(binary)


@asynccontextmanager
async def worker(binary):
    process = await asyncio.create_subprocess_exec(
        binary,
        "-test.run=^TestRPCWorkerProcess$",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"SUPERPLANE_TEST_RPC_WORKER": "1", "PATH": "/usr/bin:/bin"},
    )

    async def request(socket, token_file, lease, step="create", **extra):
        binding = {
            key: value
            for key, value in asdict(lease).items()
            if key
            in {
                "operation_id",
                "org_id",
                "workspace_id",
                "holder",
                "attempt_id",
                "fence_token",
            }
        }
        binding.update(extra.pop("binding", {}))
        process.stdin.write(
            json.dumps(
                {
                    "socket": str(socket),
                    "token_file": str(token_file),
                    "binding": binding,
                    "step": step,
                    **extra,
                }
            ).encode()
            + b"\n"
        )
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        assert line.startswith(b"{"), f"Go worker failed: {line!r}"
        return json.loads(line)

    try:
        yield request
    finally:
        process.stdin.close()
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, stderr.decode()


@asynccontextmanager
async def service(pool, lease, effects, *, outcome=CallOutcome.SUCCEEDED):
    state = {"revoked": False, "outcome": outcome}

    async def authenticate(token):
        if token != "scoped-controller-test-token" or state["revoked"]:
            raise OperationRefused("credential refused")
        return ExecutionGrant(cancellation_principal(lease.holder), lease)

    async def provider(call):
        async with pool.acquire() as observer:
            assert (
                await observer.fetchval(
                    "SELECT stage FROM harness_provider_call_intent WHERE idempotency_key=$1",
                    call.idempotency_key,
                )
                == "intended"
            )
        effects.append(call)
        if state.get("cancel_during_call"):
            async with pool.acquire() as connection:
                await request_cancellation(
                    connection,
                    operation_id=lease.operation_id,
                    principal=cancellation_principal(lease.holder),
                )
        return state["outcome"], None, "durable-provider-reference"

    server = ExecutionRPCServer(
        connect=pool.acquire, provider_call=provider, authenticate=authenticate
    )
    with tempfile.TemporaryDirectory(prefix="go-shared-rpc-", dir="/tmp") as directory:
        socket = Path(directory) / "rpc.sock"
        token_file = Path(directory) / "token"
        token_file.write_text("scoped-controller-test-token")
        token_file.chmod(0o600)
        async with server.serve(socket):
            yield socket, token_file, state


async def test_duplicate_and_worker_restart_reuse_durable_observation(
    pool, worker_binary
):
    steps = [
        {
            "step_id": name,
            "provider": "test",
            "operation_kind": "create",
            "target": name,
        }
        for name in ("create", "schedule")
    ]
    async with pool.acquire() as connection:
        record, lease = await rpc_prepared(connection, steps=steps)
    effects = []
    async with service(pool, lease, effects) as (socket, token_file, _):
        async with worker(worker_binary) as request:
            refused = await request(socket, token_file, lease, step="schedule")
            assert refused["error"] and not effects
            first = await request(socket, token_file, lease)
            duplicate = await request(socket, token_file, lease)
            assert not first["error"] and duplicate == first
            assert len(effects) == 1
        async with worker(worker_binary) as request:
            restarted = await request(socket, token_file, lease)
            assert restarted == first and len(effects) == 1
            completed = await request(socket, token_file, lease, step="schedule")
            assert not completed["error"] and len(effects) == 2
            assert completed["result"]["Disposition"] == "settle"
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT state FROM harness_operations WHERE operation_id=$1",
                record.operation_id,
            )
            == "succeeded"
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_provider_call_intent"
            )
            == 2
        )


@pytest.mark.parametrize(
    "failure",
    [
        "token",
        "revocation",
        "removed-token",
        "fence",
        "lease",
        "runtime",
        "workspace",
        "attempt",
        "holder",
        "step",
        "cancel",
    ],
)
async def test_authority_loss_or_unadmitted_step_never_calls_provider(
    pool, worker_binary, failure
):
    async with pool.acquire() as connection:
        _, lease = await rpc_prepared(connection)
        if failure in {"lease", "runtime"}:
            await expire(connection, lease.operation_id, runtime=failure == "runtime")
        if failure == "fence":
            await connection.execute(
                "UPDATE harness_operation_leases SET fence_token=fence_token+1"
            )
    effects = []
    async with service(pool, lease, effects) as (socket, token_file, state):
        async with worker(worker_binary) as request:
            if failure == "token":
                token_file.write_text("invalid")
            if failure == "removed-token":
                token_file.unlink()
            if failure == "revocation":
                assert not (await request(socket, token_file, lease, method="status"))[
                    "error"
                ]
                state["revoked"] = True
            if failure == "cancel":
                assert not (await request(socket, token_file, lease, method="cancel"))[
                    "error"
                ]
            kwargs = {}
            if failure in {"workspace", "attempt", "holder"}:
                key = {
                    "workspace": "workspace_id",
                    "attempt": "attempt_id",
                    "holder": "holder",
                }[failure]
                kwargs["binding"] = {key: "foreign"}
            result = await request(
                socket,
                token_file,
                lease,
                step="unadmitted" if failure == "step" else "create",
                **kwargs,
            )
            assert result["error"] and not effects
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_provider_call_intent"
            )
            == 0
        )


async def test_unknown_provider_outcome_retains_handle_and_refuses_restart_retry(
    pool, worker_binary
):
    async with pool.acquire() as connection:
        _, lease = await rpc_prepared(connection)
    effects = []
    async with service(pool, lease, effects, outcome=CallOutcome.UNKNOWN) as (
        socket,
        token_file,
        _,
    ):
        async with worker(worker_binary) as request:
            result = await request(socket, token_file, lease)
            assert not result["error"]
            assert result["result"]["Disposition"] == "retain"
            assert (
                result["result"]["Call"]["provider_ref"] == "durable-provider-reference"
            )
        async with worker(worker_binary) as request:
            assert (await request(socket, token_file, lease))["error"]
        assert len(effects) == 1
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT stage, outcome, provider_ref FROM harness_provider_call_intent"
        )
        assert tuple(row.values()) == ("intended", None, "durable-provider-reference")


async def test_cancellation_during_provider_call_preserves_disposition_and_handle(
    pool, worker_binary
):
    async with pool.acquire() as connection:
        _, lease = await rpc_prepared(connection)
    effects = []
    async with service(pool, lease, effects) as (socket, token_file, state):
        state["cancel_during_call"] = True
        async with worker(worker_binary) as request:
            result = await request(socket, token_file, lease)
            assert "cancelled" in result["error"]
            assert result["result"]["Disposition"] == "settle"
            assert (
                result["result"]["Call"]["provider_ref"] == "durable-provider-reference"
            )
            assert (await request(socket, token_file, lease))["error"]
        assert len(effects) == 1
