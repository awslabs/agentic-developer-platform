"""Regional observations use durable identity; provider I/O is explicitly simulated."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError
from harness_jobs.inventory import AllocationResource, ResourcePresence
from superplane_executor.inventory import Finalizer
from superplane_executor.plan import Plan


@pytest.mark.parametrize(
    "state", ["present", "empty", "not-found", "denied", "foreign", "bare"]
)
async def test_inventory_queries_exact_original_region_and_retains_unknown(state):
    calls = []

    class EC2:
        def describe_volumes(self, **kwargs):
            calls.append(kwargs)
            if state == "denied":
                raise PermissionError("simulated regional inventory denial")
            if state == "not-found":
                raise ClientError(
                    {"Error": {"Code": "InvalidVolume.NotFound"}}, "DescribeVolumes"
                )
            return {
                "Volumes": []
                if state == "empty"
                else [{"VolumeId": "vol-0123456789abcdef0", "State": "available"}]
            }

    def client(service, *, region_name):
        assert service == "ec2"
        assert region_name == "us-west-2"
        return EC2()

    plan = Plan(
        {
            "version": 4,
            "provider_account_id": "123456789012",
            "regions": [{"region": "us-east-1"}, {"region": "us-west-2"}],
        },
        "allocation",
        (),
    )
    ref = "arn:aws:ec2:us-west-2:123456789012:volume/vol-0123456789abcdef0"
    if state == "foreign":
        ref = ref.replace("123456789012", "999999999999")
    if state == "bare":
        ref = "vol-0123456789abcdef0"
    resource = AllocationResource(ref, "aws", ref, "volume", frozenset())
    # Observation uses no registry or local selection memory; only the stored ARN.
    finalizer = object.__new__(Finalizer)
    finalizer.provider = SimpleNamespace(
        session_for=AsyncMock(return_value=(SimpleNamespace(client=client), "role"))
    )
    result = await finalizer.observe(None, None, plan, resource)
    expected = {
        "present": ResourcePresence.PRESENT,
        "not-found": ResourcePresence.ABSENT,
    }
    assert result.presence is expected.get(state, ResourcePresence.UNKNOWN)
    assert result.queried_by == ref
    assert len(calls) == (0 if state in {"foreign", "bare"} else 1)
    if calls:
        assert calls[0] == {"VolumeIds": ["vol-0123456789abcdef0"]}


async def test_failed_regional_listing_cannot_report_empty_discovery():
    from superplane_executor.provider import Provider

    regions_read = []

    class EC2:
        def __init__(self, region):
            self.region = region

        def get_paginator(self, method):
            assert method == "describe_instances"
            return self

        def paginate(self, **kwargs):
            regions_read.append(self.region)
            if self.region == "us-west-2":
                raise PermissionError("simulated regional listing denial")
            return [{"Reservations": []}]

    def client(service, *, region_name):
        assert service == "ec2"
        return EC2(region_name)

    provider = object.__new__(Provider)
    provider.session_for = AsyncMock(
        return_value=(SimpleNamespace(client=client), "role")
    )
    plan = Plan(
        {
            "version": 4,
            "regions": [{"region": "us-east-1"}, {"region": "us-west-2"}],
        },
        "allocation",
        (),
    )
    with pytest.raises(PermissionError, match="regional listing denial"):
        await provider.instances(None, plan, include_terminated=True)
    assert regions_read == ["us-east-1", "us-west-2"]


@pytest.mark.parametrize("region", ["us-west-2", "unapproved", None])
async def test_recovered_launch_uses_the_inventory_identity(region):
    from harness_jobs.identity import OperationRefused
    from superplane_executor.recovery_observation import observe_request

    plan = Plan(
        {
            "version": 4,
            "node_count": 1,
            "provider_account_id": "123456789012",
            "regions": [{"region": "us-east-1"}, {"region": "us-west-2"}],
        },
        "allocation",
        (),
    )
    provider = SimpleNamespace(
        sky=SimpleNamespace(status=AsyncMock(return_value="SUCCEEDED")),
        instances=AsyncMock(
            return_value=[
                {"InstanceId": "i-0123456789abcdef0", "SuperplaneRegion": region}
            ]
        ),
    )
    if region != "us-west-2":
        with pytest.raises(OperationRefused, match="region is outside approval"):
            await observe_request(
                provider, None, plan, operation_kind="launch", request_id="request"
            )
    else:
        assert await observe_request(
            provider, None, plan, operation_kind="launch", request_id="request"
        ) == (
            "succeeded",
            "arn:aws:ec2:us-west-2:123456789012:instance/i-0123456789abcdef0",
        )
