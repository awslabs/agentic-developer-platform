"""Cancellation and SQL status cannot substitute for provider drain evidence."""

from contextlib import asynccontextmanager
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harness_jobs.effects import CallEffect, call_effect
from harness_jobs.execution import CallOutcome
from harness_jobs.identity import OperationRefused

from workspace_provisioning.retirement_lifecycle import RetirementLifecycle

from .test_retirement_plan import inventory


@pytest.fixture
def lifecycle():
    record = inventory()
    connection = SimpleNamespace(
        fetch=AsyncMock(return_value=[]),
        fetchval=AsyncMock(return_value=0),
        fetchrow=AsyncMock(
            return_value={
                "namespace": record.namespace,
                "cluster_arn": record.cluster_arn,
                "endpoint": "https://workspace.example",
            }
        ),
    )

    @asynccontextmanager
    async def acquire():
        yield connection

    response = SimpleNamespace(
        status_code=200, json=lambda: {"items": [], "metadata": {}}
    )
    workspace = SimpleNamespace(
        path=lambda target, kind: "/" + kind, request=AsyncMock(return_value=response)
    )
    pool = SimpleNamespace(acquire=acquire)
    adapter = RetirementLifecycle(
        domain_pool=pool, execution_pool=pool, workspace=workspace
    )
    operation = SimpleNamespace(
        grant=SimpleNamespace(
            lease=SimpleNamespace(
                operation_id="teardown",
                org_id=record.org_id,
                workspace_id=record.workspace_id,
            )
        )
    )
    return adapter, record, operation, connection, workspace


@pytest.mark.asyncio
async def test_no_active_sql_rows_still_requires_fresh_workload_reads(lifecycle):
    adapter, record, operation, _, workspace = lifecycle
    result = await adapter.drain(operation, record, AsyncMock())
    assert result[0] is CallOutcome.SUCCEEDED
    assert workspace.request.await_count == 5
    assert all(call.args[2] == "GET" for call in workspace.request.await_args_list)
    assert (
        call_effect("drain-governed-workloads", provider="superplane-governance")
        is CallEffect.REMOVES
    )


@pytest.mark.asyncio
async def test_governed_workload_presence_remains_unresolved_without_label_deletion(
    lifecycle,
):
    adapter, record, operation, _, workspace = lifecycle
    workspace.request.return_value = SimpleNamespace(
        status_code=200, json=lambda: {"items": [{"metadata": {"uid": "owned-pod"}}]}
    )
    adapter.drain_seconds = 0
    result = await adapter.drain(operation, record, AsyncMock())
    assert result[0] is CallOutcome.UNKNOWN
    assert all(call.args[2] == "GET" for call in workspace.request.await_args_list)


@pytest.mark.asyncio
async def test_unanswered_provider_drain_refuses(lifecycle):
    adapter, record, operation, _, workspace = lifecycle
    workspace.request.return_value = SimpleNamespace(status_code=403)
    with pytest.raises(OperationRefused):
        await adapter.drain(operation, record, AsyncMock())


@pytest.mark.asyncio
async def test_unregister_only_changes_active_projection_and_keeps_ownership(lifecycle):
    adapter, record, operation, connection, _ = lifecycle
    connection.fetchval.return_value = record.workspace_id
    assert (await adapter.status(operation, record, "retired", AsyncMock()))[
        0
    ] is CallOutcome.SUCCEEDED
    statement = connection.fetchval.await_args.args[0]
    assert "UPDATE workspaces" in statement
    assert "DELETE" not in statement
    assert "w.is_default=false" in statement


@pytest.mark.asyncio
async def test_managed_destroy_requires_owned_inventory_and_live_persistent_fence(
    lifecycle,
):
    adapter, record, operation, _, workspace = lifecycle
    assert not await adapter.managed_destroy_ready(operation, record, AsyncMock())
    workspace.request.assert_not_called()


@pytest.mark.asyncio
async def test_unlabelled_or_replaced_workloads_block_terraform_cascade(lifecycle):
    adapter, record, operation, connection, workspace = lifecycle
    adapter.managed_objects = (
        ("Pod", "other-namespace", "tenant-work", "original-uid"),
    )
    adapter.managed_fence = AsyncMock(return_value=True)
    operation.request = SimpleNamespace(
        parameters={
            "managed_workload_inventory_sha256": hashlib.sha256(
                json.dumps(adapter.managed_objects, separators=(",", ":")).encode()
            ).hexdigest()
        }
    )
    connection.fetchval.return_value = "https://workspace.example"
    workspace.request.return_value = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "items": [
                {
                    "metadata": {
                        "namespace": "other-namespace",
                        "name": "tenant-work",
                        "uid": "replacement",
                    }
                }
            ]
        },
    )
    assert not await adapter.managed_destroy_ready(operation, record, AsyncMock())
    assert all(call.args[2] == "GET" for call in workspace.request.await_args_list)
