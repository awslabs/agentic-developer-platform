"""Deterministic paid pod entry point; all runtime tests run in isolated CI."""

import asyncio
import hashlib
import json
import os
import signal
import ssl
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import boto3
from harness_jobs.identity import OperationRefused
from harness_jobs.schema import check_schema_version

from .authority import GatewayAuthority, read_token
from .handoff import read_handoff
from .inventory import Finalizer
from .provider import Provider
from .recovery_authority import RecoveryAuthority
from .service import ControllerRPCServer, recover, required
from .skypilot import SkyPilot
from .task_registry import TaskRegistry
from .workspace import Workspace

PREFIX = "/internal/v1/controller-execution"
TERMINAL = {"succeeded", "failed", "unknown", "cancelled"}


def require_selected_task_mode(*, lifecycle):
    """A native-only deployment never inherits lifecycle authority from a queue."""
    mode = os.environ.get("SUPERPLANE_PAID_WORKER_MODE", "legacy")
    if mode not in {"legacy", "native-controller"}:
        raise OperationRefused("paid worker deployment mode is unavailable")
    if lifecycle and mode == "native-controller":
        raise OperationRefused("native paid worker refuses workspace lifecycle tasks")


def write_private(path, value, *, mode=0o600):
    path = Path(path)
    if not path.is_absolute() or not path.parent.is_dir():
        raise OperationRefused("private paid worker mount unavailable")
    temporary = path.parent / (".pending-" + uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "w") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def bootstrap(transport):
    result = await transport.post(PREFIX + "/task/acquire", {}, bootstrap=True)
    if result.get("body") is None:
        return None
    envelope = json.loads(result["body"])
    digest = hashlib.sha256(
        json.dumps(
            envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()
    bound = await transport.post(
        PREFIX + "/bootstrap",
        {
            "invocation_id": envelope["message_id"],
            "envelope_digest": digest,
        },
        bootstrap=True,
    )
    original = envelope["domain_operation"]
    if (
        bound["domain_operation"] != original
        or bound["invocation_id"] != envelope["message_id"]
    ):
        raise OperationRefused("paid task bootstrap changed original admission")
    if (
        original["adp_org_id"] != envelope["tenant_id"]
        or original["org_id"] != original["domain_org_id"]
    ):
        raise OperationRefused("paid task tenant mapping changed")
    deadline = datetime.fromisoformat(bound["not_after"])
    if deadline.tzinfo is None or deadline <= datetime.now(UTC):
        raise OperationRefused("paid task approval expired")
    write_private(transport.run_file, bound["credential"])
    return original, deadline


async def pools(stack):
    schema = required("SUPERPLANE_OPERATION_SCHEMA")
    if not schema.replace("_", "a").isalnum() or schema == "public":
        raise OperationRefused("dedicated operation schema required")
    tls = ssl.create_default_context(cafile=required("SUPERPLANE_DATABASE_CA_FILE"))
    result = []
    for name in ("SUPERPLANE_DOMAIN_DSN_FILE", "SUPERPLANE_EXECUTION_DSN_FILE"):
        pool = await stack.enter_async_context(
            await asyncpg.create_pool(
                read_token(Path(required(name))),
                min_size=1,
                max_size=8,
                timeout=10,
                command_timeout=20,
                ssl=tls,
                server_settings={"search_path": schema + ",public"},
            )
        )
        async with pool.acquire() as connection:
            await check_schema_version(connection)
        result.append(pool)
    return result


async def maintain(transport, stop, deadline):
    renewed = asyncio.get_running_loop().time()
    while not stop.is_set():
        if datetime.now(UTC) >= deadline:
            raise OperationRefused("paid task approval expired")
        await transport.post(PREFIX + "/task/heartbeat", {})
        if asyncio.get_running_loop().time() - renewed >= 300:
            credential = await transport.post(PREFIX + "/renew", {})
            write_private(transport.run_file, credential["credential"])
            renewed = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except TimeoutError:
            pass


async def controller_task(
    transport, operation, deadline, domain_pool, execution_pool, stop
):
    handoff_file = Path(required("SUPERPLANE_RUN_HANDOFF_FILE"))
    assignment_file = Path(required("SUPERPLANE_TASK_ASSIGNMENT_FILE"))
    socket = Path(required("SUPERPLANE_EXECUTION_SOCKET"))
    token_dir = Path(required("SUPERPLANE_EXECUTION_CREDENTIALS_DIR"))
    write_private(
        handoff_file,
        json.dumps(
            {
                "version": 1,
                "grants": [
                    {
                        "operation_id": operation.grant.lease.operation_id,
                        "attempt_id": operation.grant.lease.attempt_id,
                        "job_id": operation.job_id,
                        "not_after": deadline.isoformat(),
                    }
                ],
            }
        ),
    )
    async with AsyncExitStack() as stack:
        sky = SkyPilot(
            required("SKYPILOT_URL"), required("SKYPILOT_SERVICE_TOKEN_FILE")
        )
        stack.push_async_callback(sky.aclose)
        provider = Provider(
            sky=sky,
            workspace=Workspace(
                required("SUPERPLANE_WORKSPACE_CREDENTIALS_DIR"),
                required("SUPERPLANE_MANAGEMENT_API_SERVER"),
            ),
            domain_pool=domain_pool,
            execution_pool=execution_pool,
        )
        registry = TaskRegistry(
            original=operation,
            write_private=write_private,
            assignment_file=assignment_file,
            domain_pool=domain_pool,
            execution_pool=execution_pool,
            authority=transport,
            instance_file=handoff_file,
            token_dir=token_dir,
            submitter_id="paid-task",
            validate_plan=provider.validate_plan,
            handoff_reader=lambda: read_handoff(handoff_file),
        )
        provider.registry = registry
        server = ControllerRPCServer(
            connect=execution_pool.acquire,
            provider_call=provider,
            authenticate=registry.authenticate,
            after_step=Finalizer(provider, registry),
        )
        async with server.serve(
            str(socket), worker_gid=int(required("SUPERPLANE_WORKER_GID"))
        ):
            try:
                while not stop.is_set():
                    status = await transport.post(PREFIX + "/task/status", {})
                    if status["state"] in TERMINAL:
                        return
                    if status["cancelled"]:
                        raise OperationRefused("paid task cancelled; recovery required")
                    name = await registry.publish(operation.grant.lease.operation_id)
                    token = registry.tokens[name][0]
                    await server.dispatch(
                        {"token": token, "method": "renew", "arguments": {}}
                    )
                    if assignment_file.with_suffix(".finished").exists():
                        # Go completion is a wake-up signal, never settlement evidence.
                        status = await transport.post(PREFIX + "/task/status", {})
                        if status["state"] not in TERMINAL:
                            raise OperationRefused(
                                "worker stopped before durable settlement"
                            )
                        return
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=5)
                    except TimeoutError:
                        pass
            finally:
                registry.revoke_except(set())
                handoff_file.unlink(missing_ok=True)
                assignment_file.unlink(missing_ok=True)


async def execute(transport, original, deadline, stop):
    async with AsyncExitStack() as stack:
        if original["mode"] == "recovery":
            scope = await transport.post(PREFIX + "/recovery/scope", {})
            require_selected_task_mode(
                lifecycle=scope.get("operation_type") == "workspace_lifecycle"
            )
            authority = RecoveryAuthority(transport)
            await authority.recovery_scope()
            domain, execution = await pools(stack)
            if scope.get("operation_type") == "workspace_lifecycle":
                from workspace_provisioning.recovery import recover_lifecycle

                await recover_lifecycle(
                    authority,
                    SimpleNamespace(domain_pool=domain, execution_pool=execution),
                    operation_id=original["operation_id"],
                    context=SimpleNamespace(
                        connect=execution.acquire,
                        domain_connect=domain.acquire,
                        authority=authority,
                        policy_file=Path(required("SUPERPLANE_LIFECYCLE_POLICY_FILE")),
                        state_root=Path(required("SUPERPLANE_LIFECYCLE_STATE_DIR")),
                    ),
                )
                return
            if scope.get("operation_type") != "controller":
                raise OperationRefused("paid recovery operation type is unavailable")
            await recover(
                authority,
                SimpleNamespace(domain_pool=domain, execution_pool=execution),
                operation_id=original["operation_id"],
            )
            return
        data = await transport.post(
            PREFIX + "/lease", {"operation_id": original["operation_id"]}
        )
        operation = transport._verified_operation(data, original["operation_id"])
        lease = operation.grant.lease
        if (operation.job_id, lease.org_id, lease.workspace_id) != (
            original["job_id"],
            original["org_id"],
            original["workspace_id"],
        ):
            raise OperationRefused("paid lease changed original admission")
        require_selected_task_mode(
            lifecycle="runtime_config_sha256" in operation.request.parameters
        )
        domain, execution = await pools(stack)
        if "runtime_config_sha256" in operation.request.parameters:
            from workspace_provisioning.runtime import run_lifecycle

            policy_file = Path(required("SUPERPLANE_LIFECYCLE_POLICY_FILE"))
            await run_lifecycle(
                operation,
                SimpleNamespace(
                    connect=execution.acquire,
                    domain_connect=domain.acquire,
                    authority=transport,
                    policy_file=policy_file,
                    state_root=Path(required("SUPERPLANE_LIFECYCLE_STATE_DIR")),
                    base_session=boto3.Session(),
                ),
            )
        else:
            await controller_task(
                transport, operation, deadline, domain, execution, stop
            )


async def run(stop):
    transport = GatewayAuthority(
        endpoint=required("ADP_EXECUTION_AUTHORITY_ENDPOINT"),
        region=required("AWS_REGION"),
        run_credential_file=required("ADP_RUN_CREDENTIAL_FILE"),
        workload_token_file=required("ADP_WORKLOAD_TOKEN_FILE"),
    )
    running = []
    try:
        task = await bootstrap(transport)
        if task is None:
            return
        original, deadline = task
        heartbeat = asyncio.create_task(maintain(transport, stop, deadline))
        execution = asyncio.create_task(execute(transport, original, deadline, stop))
        stopped = asyncio.create_task(stop.wait())
        running = [heartbeat, execution, stopped]
        done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
        if execution in done:
            await execution
            if not stop.is_set():
                await transport.post(PREFIX + "/task/ack", {})
        elif heartbeat in done:
            await heartbeat
    finally:
        stop.set()
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        transport.run_file.unlink(missing_ok=True)
        await transport.aclose()


def main():
    async def entry():
        stop = asyncio.Event()
        for name in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(name, stop.set)
        await run(stop)

    try:
        asyncio.run(entry())
    except Exception:
        # Transport/provider exception strings may contain credentials or bodies.
        raise SystemExit("paid task stopped; durable recovery required") from None
