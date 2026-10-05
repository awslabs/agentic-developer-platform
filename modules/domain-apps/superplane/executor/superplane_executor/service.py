"""Trusted sidecar entry point. Go receives only the RPC socket and scoped tokens."""

import asyncio
import fcntl
import json
import os
import signal
import stat
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import asyncpg
from harness_jobs.execution_rpc import ExecutionRPCServer

from .authority import GatewayAuthority, read_token
from .handoff import read_handoff
from .provider import Provider
from .inventory import Finalizer
from .recovery import ScopedRecovery
from .recovery_authority import RecoveryAuthority
from .recovery_inventory import RecoveryFinalizer
from .registry import AssignmentRegistry
from .skypilot import SkyPilot
from .workspace import Workspace


def required(name):
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is required")
    return value


def operation_ids(path):
    # This projection is only a selector from the existing ADP job. Possession
    # does not authorize anything: Gateway verifies each selected operation.
    with Path(path).open("rb") as source:
        raw = source.read(32769)
    if len(raw) > 32768:
        raise ValueError("operation selector too large")
    data = json.loads(raw)
    if (
        not isinstance(data, list)
        or len(data) > 32
        or any(not isinstance(x, str) or not 1 <= len(x) <= 255 for x in data)
        or len(set(data)) != len(data)
    ):
        raise ValueError("invalid operation selector")
    return data


class ControllerRPCServer(ExecutionRPCServer):
    """Queue effects before borrowing database connections; keep control RPCs live.

    One provider/finalizer can need several independent connections. Letting all
    32 assignments hold one dispatch connection first would exhaust the pool and
    deadlock their nested inventory/authentication reads. Queued requests hold no
    database capability and authenticate only when their execution slot opens.
    """

    def __init__(self, **kwargs):
        super().__init__(max_connections=64, **kwargs)
        self._controller_step = asyncio.Semaphore(1)

    async def dispatch(self, request):
        if isinstance(request, dict) and request.get("method") == "execute_step":
            async with self._controller_step:
                return await super().dispatch(request)
        return await super().dispatch(request)


async def recover(authority, provider, *, operation_id=None):
    """A dedicated, authenticated recovery run; no borrowed execution grant."""
    principal = await authority.recovery_scope()
    return await ScopedRecovery(
        provider,
        principal=principal,
        observe_claim=authority.observe,
        finalize=RecoveryFinalizer(provider, authority),
        ledger=authority,
        operation_id=operation_id,
    ).run()


async def serve_recovery(stop):
    # Verify the real recovery run before opening database capability. Provider
    # credentials stay behind Gateway's protected observation endpoints.
    async with AsyncExitStack() as stack:
        transport = GatewayAuthority(
            endpoint=required("ADP_EXECUTION_AUTHORITY_ENDPOINT"),
            region=required("AWS_REGION"),
            run_credential_file=required("ADP_RUN_CREDENTIAL_FILE"),
            workload_token_file=required("ADP_WORKLOAD_TOKEN_FILE"),
        )
        stack.push_async_callback(transport.aclose)
        authority = RecoveryAuthority(transport)
        await authority.recovery_scope()
        pools = []
        for name in ("SUPERPLANE_DOMAIN_DSN_FILE", "SUPERPLANE_EXECUTION_DSN_FILE"):
            pools.append(
                await stack.enter_async_context(
                    await asyncpg.create_pool(
                        read_token(Path(required(name))),
                        min_size=1,
                        max_size=8,
                        timeout=10,
                        command_timeout=20,
                    )
                )
            )
        provider = SimpleNamespace(domain_pool=pools[0], execution_pool=pools[1])
        while not stop.is_set():
            # Revocation ends this service context, releasing both pools. The
            # outer loop can only reopen after a new authenticated scope read.
            await recover(authority, provider)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass


async def serve(stop):
    # Explicit DSN files keep passwords out of argv, logs, the worker environment,
    # and the assignment registry. Migrations are a separate controlled release.
    socket = Path(required("SUPERPLANE_EXECUTION_SOCKET"))
    token_dir = Path(required("SUPERPLANE_EXECUTION_CREDENTIALS_DIR"))
    instance_file = Path(required("SUPERPLANE_CONTROLLER_INSTANCE_FILE"))
    if not all(p.is_absolute() for p in (socket, token_dir, instance_file)):
        raise ValueError("execution mounts must use absolute paths")
    async with AsyncExitStack() as stack:
        lock = stack.enter_context(
            (socket.parent / ".execution-service.lock").open("a")
        )
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # The lock is held for the entire process lifetime. A stale socket from a
        # killed previous instance in this pod-private directory is safe to retire.
        if socket.exists() or socket.is_symlink():
            info = socket.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("execution socket ownership mismatch")
            socket.unlink()
        domain_pool = await stack.enter_async_context(
            await asyncpg.create_pool(
                read_token(Path(required("SUPERPLANE_DOMAIN_DSN_FILE"))),
                min_size=1,
                max_size=8,
                timeout=10,
                command_timeout=20,
            )
        )
        execution_pool = await stack.enter_async_context(
            await asyncpg.create_pool(
                read_token(Path(required("SUPERPLANE_EXECUTION_DSN_FILE"))),
                min_size=1,
                max_size=8,
                timeout=10,
                command_timeout=20,
            )
        )
        authority = GatewayAuthority(
            endpoint=required("ADP_EXECUTION_AUTHORITY_ENDPOINT"),
            region=required("AWS_REGION"),
            run_credential_file=required("ADP_RUN_CREDENTIAL_FILE"),
            workload_token_file=required("ADP_WORKLOAD_TOKEN_FILE"),
        )
        stack.push_async_callback(authority.aclose)
        sky = SkyPilot(
            required("SKYPILOT_URL"), required("SKYPILOT_SERVICE_TOKEN_FILE")
        )
        stack.push_async_callback(sky.aclose)
        workspace = Workspace(
            required("SUPERPLANE_WORKSPACE_CREDENTIALS_DIR"),
            required("SUPERPLANE_MANAGEMENT_API_SERVER"),
        )
        provider = Provider(
            sky=sky,
            workspace=workspace,
            domain_pool=domain_pool,
            execution_pool=execution_pool,
        )
        registry = AssignmentRegistry(
            domain_pool=domain_pool,
            execution_pool=execution_pool,
            authority=authority,
            instance_file=instance_file,
            token_dir=token_dir,
            submitter_id=required("SUPERPLANE_REGISTRY_SUBMITTER_ID"),
            validate_plan=provider.validate_plan,
            handoff_reader=lambda: read_handoff(
                required("SUPERPLANE_RUN_HANDOFF_FILE")
            ),
        )
        registry.handoffs = read_handoff(required("SUPERPLANE_RUN_HANDOFF_FILE"))
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
                    try:
                        selected = operation_ids(
                            required("SUPERPLANE_EXECUTION_OPERATION_FILE")
                        )
                        if not selected:
                            return
                        # Re-read every cycle: a rotated or withdrawn grant must take
                        # effect without a restart, and an unreadable one must revoke.
                        registry.handoffs = read_handoff(
                            required("SUPERPLANE_RUN_HANDOFF_FILE")
                        )
                        if not any(
                            operation_id in registry.handoffs
                            and registry.handoffs[operation_id].live()
                            for operation_id in selected
                        ):
                            return
                        await registry.refresh(selected)
                        # Renew only the already verified live grant. The shared
                        # service retains its admitted runtime/attempt ceilings.
                        for name, (token, _, _) in tuple(registry.tokens.items()):
                            try:
                                await server.dispatch(
                                    {"token": token, "method": "renew", "arguments": {}}
                                )
                            except Exception:
                                registry.revoke_except(set(registry.tokens) - {name})
                    except Exception:
                        registry.revoke_except(set())
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=5)
                    except TimeoutError:
                        pass
            finally:
                registry.revoke_except(set())


async def run(stop=None):
    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
    while not stop.is_set():
        try:
            mode = os.environ.get("SUPERPLANE_EXECUTOR_MODE", "execution")
            if mode == "recovery":
                await serve_recovery(stop)
                continue
            if mode != "execution":
                raise ValueError("unsupported trusted executor mode")
            selected = operation_ids(required("SUPERPLANE_EXECUTION_OPERATION_FILE"))
            # No live grant means no authority, so no pool, socket or provider client
            # is opened at all. An idle pod is the correct answer, not a degraded one.
            granted = read_handoff(required("SUPERPLANE_RUN_HANDOFF_FILE"))
            if any(
                operation_id in granted and granted[operation_id].live()
                for operation_id in selected
            ):
                await serve(stop)
        except Exception:
            # Empty/missing run projections and unavailable dependencies are idle,
            # not authority. No socket/assignments are published until verified.
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except TimeoutError:
            pass


def main():
    try:
        asyncio.run(run())
    except Exception:
        # Startup errors can contain DSNs or cloud response bodies. Detailed
        # diagnostics belong to the credential-safe service observer, not stderr.
        raise SystemExit(
            "trusted execution service configuration or dependency unavailable"
        ) from None


if __name__ == "__main__":
    main()
