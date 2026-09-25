"""Native observation consumes the production paid-task credential refresh.

The existing admitted launch supplies Provider's real authorization closure. Its
transport completion delegates to the real native invocation reader, isolating
credential composition from native plan/Journal tests in test_node_command_postgres.
No clock, token lifetime, controller interval, or authorization check is mocked.
"""

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
import threading
from uuid import uuid4

import pytest
from harness_jobs.execution_plan import admitted_steps, step_key
from harness_jobs.leases import read_lease
from harness_jobs.store import OperationStore

from superplane_executor import node_command, task_worker
from superplane_executor.node_command_plan import DOCUMENTS
from superplane_executor.node_runner import success_receipt
from test_lifecycle_postgres import system as system
from test_node_runners import contract
from tests.conftest import requires_postgres

pytestmark = requires_postgres


@pytest.mark.parametrize("failure", [None, "refresh", "registration"])
async def test_native_observation_crosses_initial_assignment_with_same_fence(
    system, tmp_path, monkeypatch, failure
):
    pool, admit, original_server, cloud, kube, original_registry, authority = system
    operation, _ = await admit("provision")
    lease = operation.grant.lease
    provider = original_server._provider_call
    sky = provider.sky
    assignment = tmp_path / "paid-assignment.json"
    for key, value in {
        "SUPERPLANE_RUN_HANDOFF_FILE": tmp_path / "paid-handoff.json",
        "SUPERPLANE_TASK_ASSIGNMENT_FILE": assignment,
        "SUPERPLANE_EXECUTION_SOCKET": tmp_path / "paid.sock",
        "SUPERPLANE_EXECUTION_CREDENTIALS_DIR": tmp_path / "paid-tokens",
        "SKYPILOT_URL": "https://sky.example",
        "SKYPILOT_SERVICE_TOKEN_FILE": tmp_path / "sky-token",
        "SUPERPLANE_WORKSPACE_CREDENTIALS_DIR": tmp_path,
        "SUPERPLANE_MANAGEMENT_API_SERVER": "https://workspace.example",
        "SUPERPLANE_WORKER_GID": os.getgid(),
    }.items():
        monkeypatch.setenv(key, str(value))

    class Transport:
        fail_refresh = False

        async def resolve(self, operation_id):
            verified = await authority.resolve(operation_id)
            async with pool.acquire() as connection:
                current = await read_lease(connection, operation_id=operation_id)
            assert current is not None
            return replace(verified, grant=replace(verified.grant, lease=current))

        async def preflight(self, current):
            await authority.preflight(current)

        async def delivery_role(self, current):
            return await authority.delivery_role(current)

        async def post(self, path, body):
            assert path.endswith("/task/status") and body == {}
            if self.fail_refresh:
                raise ConnectionError("simulated task-status outage")
            async with pool.acquire() as connection:
                state = await connection.fetchval(
                    "SELECT state FROM harness_operations WHERE operation_id=$1",
                    lease.operation_id,
                )
            return {"state": state, "cancelled": False}

    transport = Transport()
    captured = {}
    server_type = task_worker.ControllerRPCServer

    def server_factory(**kwargs):
        captured["server"] = server_type(**kwargs)
        return captured["server"]

    monkeypatch.setattr(task_worker, "SkyPilot", lambda *args: sky)
    monkeypatch.setattr(task_worker, "Workspace", lambda *args: kube)
    monkeypatch.setattr(task_worker, "Provider", lambda **kwargs: provider)
    monkeypatch.setattr(task_worker, "ControllerRPCServer", server_factory)

    value = contract()
    value.update(
        operation_id=lease.operation_id,
        attempt_id=lease.attempt_id,
        fence_token=lease.fence_token,
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        runtime_deadline=lease.runtime_deadline.isoformat(),
    )
    command_id = str(uuid4())
    document, _, plugin, _ = DOCUMENTS["node-bootstrap"]
    row = {
        "contract": json.dumps(value),
        "purpose": "node-bootstrap",
        "region": value["region"],
        "account_id": value["account_id"],
        "command_id": command_id,
        "instance_id": value["instance_id"],
    }
    entered = threading.Event()
    release = threading.Event()
    observed = []

    class SSM:
        def get_command_invocation(self, **arguments):
            assert arguments == {
                "CommandId": command_id,
                "InstanceId": value["instance_id"],
                "PluginName": plugin,
            }
            entered.set()
            assert release.wait(50), "test did not release original invocation"
            return {
                **arguments,
                "DocumentName": document,
                "DocumentVersion": "1",
                "Status": "Success",
                "StatusDetails": "Success",
                "ResponseCode": 0,
                "StandardErrorContent": "",
                "StandardOutputContent": json.dumps(success_receipt(value)),
            }

    async def complete(request_id, authorize):
        receipt = await node_command.invocation(SSM(), row, authorize)
        observed.append(receipt)
        return receipt is not None

    monkeypatch.setattr(sky, "complete", complete)
    stop = asyncio.Event()
    controller = asyncio.create_task(
        task_worker.controller_task(
            transport, operation, lease.runtime_deadline, pool, pool, stop
        )
    )
    execution = None
    try:
        async with asyncio.timeout(10):
            while not assignment.exists():
                if controller.done():
                    await controller
                await asyncio.sleep(0.01)
        task_registry = provider.registry
        original_assignment = assignment.read_text()
        name = json.loads(original_assignment)["credential_name"]
        token, binding, initial_expiry = task_registry.tokens[name]
        assert 0 < (initial_expiry - datetime.now(UTC)).total_seconds() <= 30
        async with pool.acquire() as connection:
            registration = await connection.fetchrow("SELECT * FROM observation_leases")
            assignment_count = await connection.fetchval(
                "SELECT count(*) FROM controller_executions"
            )
            record = await OperationStore().get(
                connection, operation.grant.principal, lease.operation_id
            )
        key = step_key(record, admitted_steps(record)[0])
        execution = asyncio.create_task(
            captured["server"].dispatch(
                {
                    "token": token,
                    "method": "execute_step",
                    "arguments": {"step_id": "1"},
                }
            )
        )
        async with asyncio.timeout(10):
            while not entered.is_set():
                if execution.done():
                    await execution
                    pytest.fail("provider never entered native invocation")
                await asyncio.sleep(0.01)
        if failure == "refresh":
            transport.fail_refresh = True
            with pytest.raises(ConnectionError, match="task-status outage"):
                await asyncio.wait_for(asyncio.shield(controller), 10)
        elif failure == "registration":
            async with pool.acquire() as connection:
                await connection.execute(
                    "UPDATE observation_leases SET expires_at=clock_timestamp()-interval '1 second'"
                )
            with pytest.raises(PermissionError):
                await asyncio.wait_for(asyncio.shield(controller), 10)

        # Cross the actual first token expiry while the real SDK reader is in
        # flight. A missing refresh fails its post-call Provider authorization.
        await asyncio.sleep(
            max(0, (initial_expiry - datetime.now(UTC)).total_seconds()) + 1
        )
        assert datetime.now(UTC) > initial_expiry
        if failure is None:
            assert not controller.done()
            refreshed_token, refreshed_binding, refreshed_expiry = task_registry.tokens[
                name
            ]
            assert (refreshed_token, refreshed_binding) == (token, binding)
            assert refreshed_expiry > datetime.now(UTC)
            assert assignment.read_text() == original_assignment
        else:
            assert not task_registry.tokens
            assert not assignment.exists()
        release.set()
        # Failure can surface through RPC/finalization or as a durable UNKNOWN;
        # either way, accepted SSM output must not become provider success.
        await asyncio.gather(asyncio.wait_for(execution, 10), return_exceptions=True)
        async with pool.acquire() as connection:
            outcome = await connection.fetchval(
                "SELECT outcome FROM harness_provider_call_intent WHERE idempotency_key=$1",
                key,
            )
            current = await read_lease(connection, operation_id=lease.operation_id)
            assert current is not None
            for field in (
                "operation_id",
                "holder",
                "attempt_id",
                "fence_token",
                "runtime_deadline",
                "attempts",
            ):
                assert getattr(current, field) == getattr(lease, field)
            assert (
                await connection.fetchval("SELECT count(*) FROM controller_executions")
                == assignment_count
            )
            if failure != "registration":
                assert (
                    await connection.fetchrow("SELECT * FROM observation_leases")
                    == registration
                )
        assert cloud.launches == 1
        if failure is None:
            assert observed == [success_receipt(value)]
            assert outcome == "succeeded"
            assert execution.exception() is None
            assert current.expires_at > lease.expires_at
        else:
            assert observed == []
            assert outcome == "unknown"
    finally:
        release.set()
        stop.set()
        if execution is not None and not execution.done():
            execution.cancel()
        if not controller.done():
            controller.cancel()
        await asyncio.gather(
            controller, *([execution] if execution else []), return_exceptions=True
        )
