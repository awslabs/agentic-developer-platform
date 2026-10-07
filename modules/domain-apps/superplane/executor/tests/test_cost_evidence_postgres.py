"""Real finalization retains resource evidence without manufacturing zero charges."""

# Ruff treats imported pytest fixtures as unused when parameters shadow them.
# ruff: noqa: F811

import json

import pytest
from harness_jobs.identity import OperationRefused
from tests.conftest import requires_postgres

from test_lifecycle_postgres import system as system  # noqa: F401

pytestmark = requires_postgres


@pytest.mark.parametrize("leaked_volume", [False, True])
async def test_paid_cleanup_records_each_cost_category_even_when_resources_are_gone(
    system, leaked_volume
):
    pool, admit, server, cloud, _, _, _ = system
    source, token = await admit("provision")
    for step in ("1", "2", "3", "4"):
        result = await server.dispatch(
            {"token": token, "method": "execute_step", "arguments": {"step_id": step}}
        )
        assert result[1] == "settle"

    async def read(operation):
        async with pool.acquire() as connection:
            return json.loads(
                await connection.fetchval(
                    "SELECT observation::text FROM controller_execution_accounting WHERE operation_id=$1",
                    operation.grant.lease.operation_id,
                )
            )

    before = await read(source)
    assert (
        before["cost_evidence"]["source_operation_id"]
        == source.grant.lease.operation_id
    )
    cloud.leaked_volume = leaked_volume
    cleanup, token = await admit("teardown")
    request = {"token": token, "method": "execute_step", "arguments": {"step_id": "1"}}
    if leaked_volume:
        with pytest.raises(
            OperationRefused, match="allocation reconciliation requires recovery"
        ):
            await server.dispatch(request)
    else:
        assert (await server.dispatch(request))[1] == "settle"
    after = await read(cleanup)
    assert after["release_permitted"] is (not leaked_volume)
    # This legacy fixture has no recorded source operation parameter. The report
    # cannot invent one merely because cleanup observes the same allocation.
    assert after["cost_evidence"]["source_operation_id"] is None
    assert (
        after["cost_evidence"]["observation_operation_id"]
        == cleanup.grant.lease.operation_id
    )
    assert (
        before["cost_evidence"]["allocation_id"]
        == after["cost_evidence"]["allocation_id"]
    )
    for category in ("compute", "storage", "network", "transfer"):
        old = before["cost_evidence"]["categories"][category]
        new = after["cost_evidence"]["categories"][category]
        assert {item["resource_id"] for item in old["resource_observations"]} == {
            item["resource_id"] for item in new["resource_observations"]
        }
        assert new["resource_inventory_complete"] is True
        assert new["usage"]["status"] == "unknown"
        assert new["usage"]["quantity"] is None
        assert new["billed_cost"]["status"] == "unknown"
        assert new["billed_cost"]["amount"] is None
        assert new["billed_cost"]["interval_end"] is None
    volume = after["cost_evidence"]["categories"]["storage"]["resource_observations"]
    assert len(volume) == 1
    assert volume[0]["provider_reference"] == "vol-0123456789abcdef0"
    assert volume[0]["disposition"] == ("settle" if leaked_volume else "release")
    assert cloud.launches == 1
