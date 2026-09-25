"""Actual paid worker/result publication must join Pod placement to live capacity."""
# ruff: noqa: F811 - fixture imports intentionally share parameter names

from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from superplane_executor import results

from tests.test_batch_results_postgres import (
    output as output,
    read,
    batch_workload as batch_workload,
    batch_runtime as batch_runtime,
    workload as workload,
    lifecycle as lifecycle,
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    pytestmark as pytestmark,
)


@pytest.mark.parametrize(
    "change",
    [
        "foreign-before-completion",
        "foreign-at-capture",
        "node-replaced-at-capture",
        "instance-gone-at-capture",
        "revoked-at-capture",
        "heartbeat-only",
    ],
)
async def test_worker_cannot_publish_result_for_changed_actual_placement(
    output, monkeypatch, change
):
    runtime = output.runtime
    actual_request, actual_capture = runtime.kube.request, results.capture
    state = SimpleNamespace(capturing=False, nodes=0)

    async def capture(*args):
        state.capturing = True
        return await actual_capture(*args)

    async def transport(operation, target, method, path, **kwargs):
        response = await actual_request(operation, target, method, path, **kwargs)
        if method != "GET" or response.status_code != 200:
            return response
        value = deepcopy(response.json())
        if "/pods" in path and (
            change == "foreign-before-completion"
            or (change == "foreign-at-capture" and state.capturing)
        ):
            for pod in value.get("items", [value]):
                # Preserve all approved labels/resources/selectors. Only actual
                # admission-time node placement differs from allocated capacity.
                pod["spec"]["nodeName"] = "foreign-ready-node"
        if path.startswith("/api/v1/nodes?") and state.capturing:
            state.nodes += 1
            if change == "node-replaced-at-capture" and state.nodes > 1:
                value["items"][0]["metadata"]["uid"] = "replacement-node"
            elif change == "heartbeat-only":
                value["items"][0]["metadata"]["resourceVersion"] = str(state.nodes)
            elif change == "instance-gone-at-capture":
                # First proof's API call is in flight when the provider instance
                # disappears. The second proof must enumerate provider truth again.
                runtime.cloud.exists = False
            elif change == "revoked-at-capture":
                async with output.c.connections.connect() as connection:
                    await connection.execute(
                        "UPDATE harness_operation_leases SET fence_token=fence_token+1 WHERE operation_id=$1",
                        operation.grant.lease.operation_id,
                    )
        return httpx.Response(200, json=value)

    monkeypatch.setattr(runtime.kube, "request", transport)
    monkeypatch.setattr(results, "capture", capture)
    await runtime.execute(await runtime.publish(SimpleNamespace(**output.created)))
    result = await read(output)
    async with output.c.connections.connect() as connection:
        stored = await connection.fetchval(
            "SELECT count(*) FROM controller_batch_results"
        )
        outcome = await connection.fetchval(
            "SELECT state FROM harness_operations WHERE operation_id=$1",
            output.created["operation_id"],
        )
    if change == "heartbeat-only":
        assert result["status"] == "retained" and stored == 1
        assert outcome == "succeeded" and state.nodes >= 2
    else:
        assert result["status"] == "not_captured" and stored == 0
        assert outcome != "succeeded"
    assert state.capturing is (change != "foreign-before-completion")
