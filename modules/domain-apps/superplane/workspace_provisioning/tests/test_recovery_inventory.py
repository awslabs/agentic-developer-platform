"""Original-claim fencing around fresh provider enumeration and retained absence."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harness_jobs.identity import OperationRefused
from harness_jobs.inventory import (
    AllocationResource,
    ResourceObservation,
    ResourcePresence,
)

from workspace_provisioning.recovery_inventory import ProviderInventory
from superplane_executor.recovery_observation import observe_request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,status,instances,expected",
    [
        ("delete_cluster", "SUCCEEDED", [], ("succeeded", None)),
        (
            "launch",
            "SUCCEEDED",
            [{"InstanceId": "i-actual"}],
            ("succeeded", "i-actual"),
        ),
        ("launch", "SUCCEEDED", [], ("unknown", None)),
        ("launch", "RUNNING", [], ("unknown", None)),
        ("delete_cluster", "FAILED", [], ("unknown", None)),
    ],
)
async def test_recovery_returns_resource_identity_never_request_receipt(
    kind, status, instances, expected
):
    provider = SimpleNamespace(
        sky=SimpleNamespace(status=AsyncMock(return_value=status)),
        instances=AsyncMock(return_value=instances),
    )
    operation, plan = object(), SimpleNamespace(data={"node_count": 1})
    assert (
        await observe_request(
            provider, operation, plan, operation_kind=kind, request_id="request-receipt"
        )
        == expected
    )
    if kind == "launch" and status == "SUCCEEDED":
        provider.instances.assert_awaited_once_with(operation, plan)
    else:
        provider.instances.assert_not_awaited()


@pytest.fixture
def snapshot(monkeypatch):
    connection = SimpleNamespace(fetch=AsyncMock(return_value=[]))

    @asynccontextmanager
    async def transaction():
        yield

    @asynccontextmanager
    async def acquire():
        yield connection

    connection.transaction = transaction
    lease = SimpleNamespace(
        operation_id="op",
        org_id="org",
        workspace_id="ws",
        holder="recovery-holder",
        attempt_id="attempt",
        fence_token=7,
    )
    operation = SimpleNamespace(
        grant=SimpleNamespace(lease=lease),
        request=SimpleNamespace(parameters={"allocation_id": "original"}),
    )
    context = AsyncMock(return_value=(operation, {"namespace": "ws"}, "plan"))
    provider = SimpleNamespace(execution_pool=SimpleNamespace(acquire=acquire))
    adapter = ProviderInventory(provider=provider, context=context)
    resource = AllocationResource(
        "volume-1", "aws", "vol-1", "volume", frozenset({"create-key"})
    )
    adapter.discover = AsyncMock(return_value={"vol-1": resource})
    adapter.observe = AsyncMock(
        return_value=ResourceObservation(
            ResourcePresence.ABSENT, "vol-1", "provider absent"
        )
    )
    lock = AsyncMock(return_value=True)
    monkeypatch.setattr("workspace_provisioning.recovery_inventory.lock_lease", lock)
    return adapter, lease, context, lock


@pytest.mark.asyncio
async def test_retained_absent_handle_is_freshly_observed_under_original_claim(
    snapshot,
):
    adapter, lease, context, lock = snapshot
    result = await adapter.snapshot(lease, "original", "query-1")
    assert result["complete"] is True
    assert result["resources"][0]["presence"] == "absent"
    assert result["resources"][0]["operation_keys"] == ["create-key"]
    assert adapter.discover.await_count == adapter.observe.await_count == 1
    assert context.await_count == lock.await_count == 2


@pytest.mark.asyncio
async def test_failed_provider_enumeration_never_returns_empty_complete(snapshot):
    adapter, lease, _, _ = snapshot
    adapter.discover.side_effect = ConnectionError("provider unavailable")
    with pytest.raises(ConnectionError):
        await adapter.snapshot(lease, "original", "query-1")
    adapter.observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_original_allocation_cannot_be_exchanged(snapshot):
    adapter, lease, _, _ = snapshot
    with pytest.raises(OperationRefused):
        await adapter.snapshot(lease, "new-allocation", "query-1")
    adapter.discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_loss_after_reads_withholds_observation(snapshot):
    adapter, lease, _, lock = snapshot
    lock.side_effect = [True, False]
    with pytest.raises(OperationRefused):
        await adapter.snapshot(lease, "original", "query-1")
    adapter.observe.assert_awaited_once()
