"""Actual native retirement and durable finalization over PostgreSQL.

Successful original apply/bootstrap/preparation provider results are fixture facts;
the current retirement source reconstruction, ordered execution and finalizer run
unmodified. Only cloud and subprocess transports are doubled.
"""

import json

import pytest

from workspace_provisioning.retirement_composer import run_retirement

from .managed_retirement_cloud_fixture import transport
from .managed_retirement_composition_fixture import build


async def durable_result(case):
    lease = case.operation.grant.lease
    async with case.harness.connect() as connection:
        operation = await connection.fetchval(
            "SELECT state FROM harness_operations WHERE operation_id=$1",
            lease.operation_id,
        )
        workspace = await connection.fetchval(
            "SELECT status FROM workspaces WHERE id::text=$1", lease.workspace_id
        )
        accounting = await connection.fetchval(
            "SELECT observation FROM controller_execution_accounting WHERE operation_id=$1",
            lease.operation_id,
        )
        calls = await connection.fetch(
            "SELECT provider_ref,outcome FROM harness_provider_call_intent WHERE operation_id=$1",
            lease.operation_id,
        )
        reports = await connection.fetchval(
            "SELECT count(*) FROM harness_provider_report WHERE operation_id=$1",
            lease.operation_id,
        )
        return operation, workspace, json.loads(accounting), calls, reports


@pytest.mark.parametrize("retain_vpc", [False, True])
def test_native_retirement_source_chain(
    runtime, server, tmp_path, monkeypatch, retain_vpc
):
    loop = runtime.store.store._loop
    try:
        case = loop.run(build(runtime, server, tmp_path))
        context, state = transport(case, monkeypatch)
        state["retain_vpc"] = retain_vpc
        if retain_vpc:
            with pytest.raises(ExceptionGroup):
                loop.run(run_retirement(case.operation, context))
        else:
            result = loop.run(run_retirement(case.operation, context))
            assert result["status"] == "retired"
        assert state["destroyed"] is True
        assert len(state["process_calls"]) == 1
        operation, workspace, accounting, calls, reports = loop.run(
            durable_result(case)
        )
        assert all(call["provider_ref"] is None for call in calls)
        assert accounting["allocation_id"] == "original-allocation"
        if retain_vpc:
            assert operation != "succeeded"
            assert workspace != "Deleted"
            assert accounting["may_mark_released"] is False
            assert reports == 0
        else:
            assert operation == "succeeded"
            assert workspace == "Deleted"
            assert accounting["may_mark_released"] is True
            assert accounting["inventory_complete"] is True
            assert accounting["exposure"] == "none"
            assert len(accounting["resource_dispositions"]) == 2
            assert not accounting["unresolved_resources"]
            assert reports == 1
    finally:
        if hasattr(runtime, "retirement_pool"):
            loop.run(runtime.retirement_pool.close())
