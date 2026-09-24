"""The paid task consumes canonical registration without acquiring its lease."""

# Imported pytest fixtures are intentionally named by test parameters.
# ruff: noqa: F811

import json

import pytest
from harness_jobs.identity import OperationRefused

from superplane_executor.task_registry import TaskRegistry
from superplane_executor.task_worker import write_private
from test_lifecycle_postgres import system as system


async def test_task_assignment_preserves_actual_run_and_registration_owner(
    system,
    tmp_path,
):
    pool, admit, server, cloud, _, registry, _ = system
    operation, _ = await admit("provision")
    async with pool.acquire() as c:
        original_owner = await c.fetchrow(
            "SELECT holder,expires_at FROM observation_leases"
        )
        original_assignments = await c.fetchval(
            "SELECT count(*) FROM controller_executions"
        )
    directory = tmp_path / "paid"
    directory.mkdir()
    task = TaskRegistry(
        original=operation,
        write_private=write_private,
        assignment_file=directory / "assignment.json",
        domain_pool=pool,
        execution_pool=pool,
        authority=registry.authority,
        instance_file=directory / "unused-instance",
        token_dir=directory / "tokens",
        submitter_id="paid-task",
        validate_plan=registry.validate_plan,
        handoffs=registry.handoffs,
    )
    name = await task.publish(operation.grant.lease.operation_id)
    data = json.loads((directory / "assignment.json").read_text())
    assert data["holder"] == operation.grant.lease.holder
    assert data["attempt_id"] == operation.grant.lease.attempt_id
    assert data["job_id"] == operation.job_id
    assert data["credential_name"] == name and data["step_ids"]
    token = (directory / "tokens" / name).read_text()
    grant = await task.authenticate(token)
    assert grant.lease.operation_id == operation.grant.lease.operation_id
    async with pool.acquire() as c:
        assert (
            await c.fetchrow("SELECT holder,expires_at FROM observation_leases")
            == original_owner
        )
        assert (
            await c.fetchval("SELECT count(*) FROM controller_executions")
            == original_assignments
        )
        await c.execute(
            "UPDATE observation_leases SET expires_at=clock_timestamp()-interval '1 second'"
        )
    with pytest.raises(OperationRefused, match="registration"):
        await task.authenticate(token)
    assert cloud.launches == 0
