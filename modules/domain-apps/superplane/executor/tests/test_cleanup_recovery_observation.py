"""Aggregate cleanup needs complete inventory and positive original termination."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from superplane_executor.plan import Plan
from superplane_executor.recovery_observation import observe_request


@pytest.fixture
def cleanup(monkeypatch):
    resources = [
        {"kind": kind, "provider_reference": ref, "presence": "absent"}
        for kind, ref in (
            ("instance", "i-original"),
            ("kubernetes_node", "original-node"),
            ("workspace_object", "original-job"),
            ("network_dependency", "original-network"),
        )
    ]
    snapshot = AsyncMock(return_value={"complete": True, "resources": resources})
    monkeypatch.setattr(
        "workspace_provisioning.recovery_inventory.ProviderInventory.snapshot",
        snapshot,
    )
    ec2 = SimpleNamespace(
        describe_instances=Mock(
            return_value={
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-original",
                                "State": {"Name": "terminated"},
                            }
                        ]
                    }
                ]
            }
        )
    )
    provider = SimpleNamespace(
        sky=SimpleNamespace(status=AsyncMock(return_value="SUCCEEDED")),
        session_for=AsyncMock(
            return_value=(SimpleNamespace(client=Mock(return_value=ec2)), None)
        ),
    )
    operation = SimpleNamespace(
        grant=SimpleNamespace(lease=object()),
        request=SimpleNamespace(parameters={"allocation_id": "original"}),
    )
    authorize = AsyncMock()

    async def observe():
        return await observe_request(
            provider,
            operation,
            Plan({"version": 3, "region": "us-east-1"}, "original", ()),
            operation_kind="delete_cluster",
            request_id="original-down",
            target={"cluster_id": "original"},
            call={"idempotency_key": "original-step"},
            authorize=authorize,
        )

    return SimpleNamespace(
        resources=resources,
        snapshot=snapshot,
        ec2=ec2,
        provider=provider,
        authorize=authorize,
        observe=observe,
    )


@pytest.mark.asyncio
async def test_complete_cleanup_requires_positive_original_termination(cleanup):
    assert await cleanup.observe() == ("succeeded", None)
    cleanup.ec2.describe_instances.assert_called_once_with(InstanceIds=["i-original"])
    assert cleanup.authorize.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["kubernetes_node", "workspace_object", "network_dependency", "instance"]
)
@pytest.mark.parametrize("presence", ["present", "unknown"])
async def test_remaining_or_uncertain_obligation_never_completes(
    cleanup, kind, presence
):
    next(r for r in cleanup.resources if r["kind"] == kind)["presence"] = presence
    assert await cleanup.observe() == ("unknown", None)
    cleanup.ec2.describe_instances.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["empty", "missing-compute", "incomplete", "denied", "replaced", "lost-claim"],
)
async def test_incomplete_or_refused_inventory_cannot_complete(cleanup, failure):
    if failure == "empty":
        cleanup.resources.clear()
    elif failure == "missing-compute":
        cleanup.resources.pop(0)
    elif failure == "incomplete":
        cleanup.snapshot.return_value["complete"] = False
    else:
        cleanup.snapshot.side_effect = RuntimeError(failure)
    assert await cleanup.observe() == ("unknown", None)
    cleanup.ec2.describe_instances.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["empty", "not-found", "running", "different-id", "lost-claim"]
)
async def test_inventory_absence_is_not_positive_compute_termination(cleanup, failure):
    if failure == "empty":
        cleanup.ec2.describe_instances.return_value = {"Reservations": []}
    elif failure == "not-found":
        cleanup.ec2.describe_instances.side_effect = RuntimeError(
            "InvalidInstanceID.NotFound"
        )
    elif failure == "lost-claim":
        cleanup.authorize.side_effect = [None, RuntimeError("claim revoked")]
    else:
        instance = cleanup.ec2.describe_instances.return_value["Reservations"][0][
            "Instances"
        ][0]
        if failure == "running":
            instance["State"]["Name"] = "running"
        else:
            instance["InstanceId"] = "i-replacement"
    assert await cleanup.observe() == ("unknown", None)
