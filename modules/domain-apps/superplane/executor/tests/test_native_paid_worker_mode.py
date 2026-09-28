"""The installed native worker refuses mixed-queue lifecycle tasks before effects."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from harness_jobs.identity import OperationRefused
from superplane_executor import task_worker


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", [False, True])
async def test_native_worker_refuses_lifecycle_before_database_or_provider(
    monkeypatch, recovery
):
    monkeypatch.setenv("SUPERPLANE_PAID_WORKER_MODE", "native-controller")
    pools = AsyncMock(side_effect=AssertionError("lifecycle must not reach DB pools"))
    monkeypatch.setattr(task_worker, "pools", pools)
    operation = SimpleNamespace(
        job_id="job",
        grant=SimpleNamespace(
            lease=SimpleNamespace(org_id="org", workspace_id="workspace")
        ),
        request=SimpleNamespace(parameters={"runtime_config_sha256": "a" * 64}),
    )
    transport = SimpleNamespace(
        post=AsyncMock(return_value={"operation_type": "workspace_lifecycle"}),
        _verified_operation=Mock(return_value=operation),
    )
    original = {
        "mode": "recovery" if recovery else "execute",
        "operation_id": "operation",
        "org_id": "org",
        "workspace_id": "workspace",
        "job_id": "job",
    }
    with pytest.raises(OperationRefused, match="workspace lifecycle tasks"):
        await task_worker.execute(
            transport, original, datetime.now(UTC), asyncio.Event()
        )
    pools.assert_not_called()
    assert transport.post.await_count == 1


@pytest.mark.parametrize("mode", ["legacy", "native-controller"])
def test_native_task_selection_preserves_controller_mode(monkeypatch, mode):
    monkeypatch.setenv("SUPERPLANE_PAID_WORKER_MODE", mode)
    task_worker.require_selected_task_mode(lifecycle=False)


def test_legacy_default_remains_explicitly_backward_compatible(monkeypatch):
    monkeypatch.delenv("SUPERPLANE_PAID_WORKER_MODE", raising=False)
    task_worker.require_selected_task_mode(lifecycle=True)
