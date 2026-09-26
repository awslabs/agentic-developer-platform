"""Real claim-fenced inventory composition cannot hide a remaining original Node."""

# ruff: noqa: F811

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from superplane_executor import node_cleanup
from superplane_executor.recovery_observation import observe_request
from tests.conftest import requires_postgres

from test_node_cleanup_postgres import nodes as nodes  # noqa: F401

pytestmark = requires_postgres


@pytest.mark.parametrize(
    "state", ["present", "deleting", "absent", "replaced", "denied", "empty-ec2"]
)
async def test_successful_down_observes_original_node_before_completing(nodes, state):
    f = nodes
    await node_cleanup.prepare(f.provider, f.operation, f.target, f.plan, f.authorize)
    f.state.instance["State"]["Name"] = "terminated"
    if state == "absent" or state == "empty-ec2":
        f.state.node = None
    elif state == "deleting":
        f.state.node["metadata"]["deletionTimestamp"] = "2026-09-25T00:00:00Z"
    elif state == "replaced":
        f.state.node["metadata"]["uid"] = "replacement"
    elif state == "denied":
        f.state.denied = True
    f.state.empty_ec2 = state == "empty-ec2"
    cloud, _ = await f.provider.session_for(f.operation, f.plan)
    cloud.get_paginator = lambda method: SimpleNamespace(
        paginate=lambda **kwargs: [{"Volumes": [], "NetworkInterfaces": []}]
    )
    cloud.describe_addresses = lambda **kwargs: {"Addresses": []}
    f.provider.session_for = AsyncMock(return_value=(cloud, None))
    # This source stopped before workload deployment. There are no roots to query.
    f.provider.workspace.objects = lambda *args: []
    f.provider.sky = SimpleNamespace(status=AsyncMock(return_value="SUCCEEDED"))
    f.state.events.clear()
    outcome = await observe_request(
        f.provider,
        f.operation,
        f.plan,
        operation_kind="delete_cluster",
        request_id="original-down",
        target=f.target,
        call={"idempotency_key": "original-cleanup"},
        authorize=f.authorize,
    )
    assert outcome == ("succeeded" if state == "absent" else "unknown", None)
    assert f.state.events and all(method == "GET" for method, _ in f.state.events)
