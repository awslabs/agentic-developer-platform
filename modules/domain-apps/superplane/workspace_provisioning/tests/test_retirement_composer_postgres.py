"""Actual native retirement and durable finalization over PostgreSQL.

Successful original apply/bootstrap/preparation provider results are fixture facts;
the current retirement source reconstruction, ordered execution and finalizer run
unmodified. Only cloud and subprocess transports are doubled.
"""

import json
from builtins import ExceptionGroup

import pytest
from harness_jobs.identity import ContractViolation
from harness_jobs.inventory import AllocationResource, InventoryAuthority

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


@pytest.mark.parametrize(
    "remaining", [None, "retain_vpc", "uncertain_control", "uncertain_bootstrap"]
)
def test_native_retirement_source_chain(
    runtime, server, tmp_path, monkeypatch, remaining
):
    loop = runtime.store.store._loop
    try:
        case = loop.run(build(runtime, server, tmp_path))
        if remaining is None:

            async def reject_foreign_allocation():
                authority = InventoryAuthority(
                    connect=case.harness.connect,
                    authenticate=lambda _: None,
                    related_allocation_id="unapproved-foreign-allocation",
                )
                async with case.harness.connect() as connection:
                    with pytest.raises(ContractViolation, match="outside the approved"):
                        await authority.enumerate_resources(
                            connection,
                            case.operation.grant.lease,
                            resources=(
                                AllocationResource(
                                    "foreign", "aws", "foreign", "test", frozenset()
                                ),
                            ),
                        )
                    assert (
                        await connection.fetchval(
                            "SELECT count(*) FROM harness_allocation_resource WHERE allocation_id='unapproved-foreign-allocation'"
                        )
                        == 0
                    )

            loop.run(reject_foreign_allocation())
        context, state = transport(case, monkeypatch)
        state[remaining] = True
        if remaining:
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
        if remaining:
            assert operation != "succeeded"
            assert workspace != "Deleted"
            assert accounting["may_mark_released"] is False
            assert (
                reports
                == {"uncertain_control": 1, "uncertain_bootstrap": 2, "retain_vpc": 0}[
                    remaining
                ]
            )
        else:
            assert operation == "succeeded"
            assert workspace == "Deleted"
            assert accounting["may_mark_released"] is True
            assert accounting["inventory_complete"] is True
            assert accounting["exposure"] == "none"
            assert len(accounting["resource_dispositions"]) == 2
            assert not accounting["unresolved_resources"]
            assert reports == 3
            related = accounting["related_allocations"]
            assert len(related) == 2
            assert related[0]["allocation_id"] == case.plan.allocation_id
            assert related[0]["inventory_complete"] is True
            assert related[0]["may_mark_released"] is True
            assert related[0]["exposure"] == "none"
            assert len(related[0]["resource_dispositions"]) == 1
            assert related[1]["allocation_id"] == "bootstrap-allocation"
            assert related[1]["inventory_complete"] is True
            assert related[1]["may_mark_released"] is True
            assert related[1]["exposure"] == "none"
            assert len(related[1]["resource_dispositions"]) > 1
    finally:
        if hasattr(runtime, "retirement_pool"):
            loop.run(runtime.retirement_pool.close())
