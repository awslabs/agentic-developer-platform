"""Bounded Unix-socket execution protocol; workers receive only a scoped run token.

Run the server in the trusted service process, with database/provider capabilities
and the credential verifier. Mount only its socket into the separate worker UID or
container. Never pass an OperationExecutor, connection factory, provider hook, or
service environment to the worker. Authentication is mandatory on every request.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from .execution import (
    CallOutcome,
    CancellationPending,
    OperationExecutor,
    ProviderCallRefused,
    disposition_for,
    read_call,
)
from .execution_plan import ExecutionStep as ExecutionStep
from .execution_plan import admitted_steps as _admitted_steps
from .execution_plan import step_key
from .identity import (
    ContractViolation,
    OperationRefused,
    OperationState,
    ResolvedPrincipal,
)
from .leases import ExecutionLease, LeaseRefused, lock_lease
from .store import OperationStore

MAX_MESSAGE_BYTES = 65536
METHODS = frozenset(
    {
        "status",
        "cancel_requested",
        "cancel",
        "execute_step",
        "renew",
        "release",
    }
)


@dataclass(frozen=True)
class ExecutionGrant:
    """Server-side result of verifying a revocable, operation-bound run credential."""

    principal: ResolvedPrincipal
    lease: ExecutionLease

    def __post_init__(self):
        if (
            not isinstance(self.principal, ResolvedPrincipal)
            or not self.principal.may_provision
        ):
            raise OperationRefused("Execution permission required")
        if not isinstance(self.lease, ExecutionLease) or (
            self.principal.org_id,
            self.principal.workspace_id,
            self.principal.subject,
        ) != (self.lease.org_id, self.lease.workspace_id, self.lease.holder):
            raise OperationRefused("Run identity does not match execution grant")


def admitted_steps(record):
    try:
        return _admitted_steps(record)
    except ContractViolation as exc:
        raise ProviderCallRefused(
            "An approved execution-step plan is required"
        ) from exc


def _wire(value):
    if is_dataclass(value):
        return _wire(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _wire(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_wire(v) for v in value]
    return value


class ExecutionRPCServer:
    """Trusted side of the protocol. This instance must never enter worker memory."""

    def __init__(
        self,
        *,
        connect,
        provider_call,
        authenticate: Callable[[str], Awaitable[ExecutionGrant]],
        max_connections=16,
    ):
        if not all(callable(c) for c in (connect, provider_call, authenticate)):
            raise ContractViolation(
                "Trusted connections, provider and authentication required"
            )
        if type(max_connections) is not int or not 1 <= max_connections <= 256:
            raise ContractViolation("max_connections must be between 1 and 256")
        self._connect, self._provider_call, self._authenticate = (
            connect,
            provider_call,
            authenticate,
        )
        self._slots = asyncio.Semaphore(max_connections)

    async def dispatch(self, request):
        if not isinstance(request, dict) or set(request) != {
            "token",
            "method",
            "arguments",
        }:
            raise ProviderCallRefused("Invalid execution request")
        token, method, arguments = (
            request[k] for k in ("token", "method", "arguments")
        )
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise ProviderCallRefused("Invalid execution credential")
        if (
            not isinstance(method, str)
            or method not in METHODS
            or not isinstance(arguments, dict)
        ):
            raise ProviderCallRefused("Invalid execution method")
        grant = await self._authenticate(token)
        if not isinstance(grant, ExecutionGrant):
            raise ProviderCallRefused("Execution credential refused")
        # No operation, holder, tenant, token, connection or hook comes from arguments.
        runtime = OperationExecutor(
            grant.lease, connect=self._connect, provider_call=self._provider_call
        )
        arguments = dict(arguments)
        if method == "execute_step":
            if set(arguments) != {"step_id"} or not isinstance(
                arguments["step_id"], str
            ):
                raise ProviderCallRefused("Only an admitted step ID may be requested")
            return _wire(await self.execute_step(grant, runtime, arguments["step_id"]))
        if method == "renew" and "duration_seconds" in arguments:
            seconds = arguments.pop("duration_seconds")
            if type(seconds) not in (int, float):
                raise ProviderCallRefused("Invalid renewal duration")
            arguments["duration"] = timedelta(seconds=seconds)
        result = await getattr(runtime, method)(**arguments)
        return _wire(result.lease if isinstance(result, OperationExecutor) else result)

    async def execute_step(self, grant, runtime, step_id):
        async with self._connect() as connection, connection.transaction():
            if not await lock_lease(connection, grant.lease):
                raise ProviderCallRefused("Execution lease is no longer live")
            record = await OperationStore().get(
                connection, grant.principal, grant.lease.operation_id
            )
            if record is None:
                raise ProviderCallRefused("Admitted operation unavailable")
            steps = admitted_steps(record)
            step = next((s for s in steps if s.step_id == step_id), None)
            if step is None:
                raise ProviderCallRefused("Step is not in the admitted plan")
            for predecessor in steps[: steps.index(step)]:
                call = await read_call(
                    connection, idempotency_key=step_key(record, predecessor)
                )
                if call is None or call.outcome is not CallOutcome.SUCCEEDED:
                    raise ProviderCallRefused("Preceding admitted step is incomplete")
            existing = await read_call(
                connection, idempotency_key=step_key(record, step)
            )
            if existing is not None and (
                existing.outcome is not CallOutcome.SUCCEEDED
                or (existing.provider, existing.operation_kind, existing.target)
                != (step.provider, step.operation_kind, step.target)
            ):
                raise ProviderCallRefused("Existing step requires recovery")
        if existing is None:
            result = await runtime.execute_provider(
                idempotency_key=step_key(record, step),
                provider=step.provider,
                operation_kind=step.operation_kind,
                target=step.target,
            )
        else:
            result = (existing, disposition_for(existing))
        # Completion belongs to the trusted service and requires every admitted
        # descriptor's provider observation. The worker has no raw reporting method.
        async with self._connect() as connection, connection.transaction():
            if not await lock_lease(connection, grant.lease):
                raise ProviderCallRefused("Execution lease is no longer live")
            calls = [
                await read_call(connection, idempotency_key=step_key(record, s))
                for s in steps
            ]
            if all(c is not None and c.outcome is CallOutcome.SUCCEEDED for c in calls):
                await runtime._settle(connection, OperationState.SUCCEEDED, None)
            elif result[0].outcome is CallOutcome.ABSENT:
                await runtime._settle(
                    connection, OperationState.FAILED, "provider reports absence"
                )
        return result

    async def handle(self, reader, writer):
        try:
            if self._slots.locked():
                raise ProviderCallRefused("Execution service at capacity")
            async with self._slots:
                line = await asyncio.wait_for(reader.readline(), 15)
                if not line.endswith(b"\n") or len(line) > MAX_MESSAGE_BYTES:
                    raise ProviderCallRefused("Invalid message size")
                request = json.loads(line)
                result = await asyncio.wait_for(self.dispatch(request), 900)
                response = {"ok": True, "result": result}
        except CancellationPending as exc:
            response = {
                "ok": False,
                "error": "cancellation_pending",
                "call": _wire(exc.call),
                "disposition": exc.disposition.value,
            }
        except (
            ProviderCallRefused,
            LeaseRefused,
            OperationRefused,
            ContractViolation,
            ValueError,
            TypeError,
            KeyError,
        ):
            response = {"ok": False, "error": "refused"}
        except Exception:  # noqa: BLE001
            # Provider/database/verifier exceptions may contain secrets. None cross RPC.
            response = {"ok": False, "error": "unavailable"}
        try:
            data = json.dumps(response, separators=(",", ":")).encode() + b"\n"
            if len(data) > MAX_MESSAGE_BYTES:
                data = b'{"ok":false,"error":"unavailable"}\n'
            writer.write(data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    @asynccontextmanager
    async def serve(self, socket_path, *, worker_gid=None):
        """Bind a private socket; refuse existing paths rather than unlinking them.

        The deployment runs service and worker as separate UIDs/containers. Their
        only shared capability is this socket plus a separately issued scoped token.
        """
        path = Path(socket_path)
        if path.exists() or path.is_symlink():
            raise ContractViolation("Execution socket path already exists")
        server = await asyncio.start_unix_server(
            self.handle, path=str(path), limit=MAX_MESSAGE_BYTES
        )
        try:
            if worker_gid is not None:
                os.chown(path, -1, worker_gid)
            os.chmod(path, 0o660 if worker_gid is not None else 0o600)
            async with server:
                yield server
        finally:
            path.unlink(missing_ok=True)


@dataclass(frozen=True)
class ExecutionClient:
    """Worker endpoint and scoped credential, without executable service state."""

    socket_path: str
    token: str = field(repr=False)
    timeout_seconds: float = 120

    async def request(self, method: str, **arguments):
        """Only named protocol methods can reach the separately authenticated server."""
        if method not in METHODS:
            raise ProviderCallRefused("Invalid execution method")
        payload = (
            json.dumps(
                {"token": self.token, "method": method, "arguments": _wire(arguments)}
            ).encode()
            + b"\n"
        )
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ContractViolation("Execution request too large")
        async with asyncio.timeout(self.timeout_seconds):
            reader, writer = await asyncio.open_unix_connection(
                self.socket_path, limit=MAX_MESSAGE_BYTES
            )
            try:
                writer.write(payload)
                await writer.drain()
                line = await reader.readline()
                if not line.endswith(b"\n") or len(line) > MAX_MESSAGE_BYTES:
                    raise ProviderCallRefused("Invalid execution response")
                response = json.loads(line)
            finally:
                writer.close()
                await writer.wait_closed()
        if not response.get("ok"):
            raise ExecutionRPCError(response.get("error", "unavailable"), response)
        return response["result"]


class ExecutionRPCError(ProviderCallRefused):
    def __init__(self, code, response):
        super().__init__(code)
        self.code, self.response = code, response


def main():
    """Trusted entry point; composition comes from operator configuration."""
    import argparse
    import importlib

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--composition", required=True, help="trusted module:factory")
    parser.add_argument("--worker-gid", type=int)
    args = parser.parse_args()
    module, separator, name = args.composition.partition(":")
    if not separator or not name.isidentifier():
        parser.error("composition must be a trusted module:factory")
    factory = getattr(importlib.import_module(module), name)

    async def run():
        # The factory owns pool and verified run-credential integration. Its lifetime
        # is this service process, never the worker process or its environment.
        async with factory() as service:
            if not isinstance(service, ExecutionRPCServer):
                raise ContractViolation("Composition must yield ExecutionRPCServer")
            async with service.serve(args.socket, worker_gid=args.worker_gid) as server:
                await server.serve_forever()

    asyncio.run(run())


if __name__ == "__main__":
    main()
