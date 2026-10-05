"""Native paid workers select only admitted, supported lifecycle phases."""

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


def test_default_and_controller_modes_do_not_enable_lifecycle(monkeypatch):
    monkeypatch.delenv("SUPERPLANE_PAID_WORKER_MODE", raising=False)
    with pytest.raises(OperationRefused, match="workspace lifecycle tasks"):
        task_worker.require_selected_task_mode(lifecycle=True)
    monkeypatch.setenv("SUPERPLANE_PAID_WORKER_MODE", "native-controller")
    with pytest.raises(OperationRefused, match="workspace lifecycle tasks"):
        task_worker.require_selected_task_mode(lifecycle=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", sorted(task_worker.LIFECYCLE_PHASES))
async def test_native_lifecycle_uses_verified_operation_phase(monkeypatch, phase):
    from workspace_provisioning import runtime

    monkeypatch.setenv("SUPERPLANE_PAID_WORKER_MODE", "native-lifecycle")
    monkeypatch.setenv("SUPERPLANE_LIFECYCLE_POLICY_FILE", "/run/policy/lifecycle.json")
    monkeypatch.setenv("SUPERPLANE_LIFECYCLE_STATE_DIR", "/run/state")
    operation = SimpleNamespace(
        job_id="job",
        grant=SimpleNamespace(
            lease=SimpleNamespace(org_id="org", workspace_id="workspace")
        ),
        request=SimpleNamespace(
            parameters={
                "lifecycle_phase": phase,
                "runtime_config_sha256": "a" * 64,
            }
        ),
    )
    transport = SimpleNamespace(
        post=AsyncMock(return_value={"verified": True}),
        _verified_operation=Mock(return_value=operation),
    )
    domain = SimpleNamespace(acquire=Mock())
    execution = SimpleNamespace(acquire=Mock())
    monkeypatch.setattr(
        task_worker, "pools", AsyncMock(return_value=(domain, execution))
    )
    run_lifecycle = AsyncMock()
    monkeypatch.setattr(runtime, "run_lifecycle", run_lifecycle)
    await task_worker.execute(
        transport,
        {
            "mode": "execute",
            "operation_id": "operation",
            "job_id": "job",
            "org_id": "org",
            "workspace_id": "workspace",
        },
        datetime.now(UTC),
        asyncio.Event(),
    )
    run_lifecycle.assert_awaited_once()
    passed_operation, context = run_lifecycle.await_args.args
    assert passed_operation is operation
    assert context.authority is transport
    assert context.connect is execution.acquire
    assert context.domain_connect is domain.acquire
    assert context.policy_file.as_posix() == "/run/policy/lifecycle.json"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["retire-workspace", "python:arbitrary", "prepare-infrastructure"]
)
async def test_native_lifecycle_refuses_unsupported_or_incomplete_phase_before_pools(
    monkeypatch, phase
):
    monkeypatch.setenv("SUPERPLANE_PAID_WORKER_MODE", "native-lifecycle")
    pools = AsyncMock(side_effect=AssertionError("must refuse before database"))
    monkeypatch.setattr(task_worker, "pools", pools)
    operation = SimpleNamespace(
        job_id="job",
        grant=SimpleNamespace(
            lease=SimpleNamespace(org_id="org", workspace_id="workspace")
        ),
        request=SimpleNamespace(parameters={"lifecycle_phase": phase}),
    )
    transport = SimpleNamespace(
        post=AsyncMock(return_value={}),
        _verified_operation=Mock(return_value=operation),
    )
    with pytest.raises(
        OperationRefused, match="phase is unavailable|configuration is missing"
    ):
        await task_worker.execute(
            transport,
            {
                "mode": "execute",
                "operation_id": "operation",
                "job_id": "job",
                "org_id": "org",
                "workspace_id": "workspace",
            },
            datetime.now(UTC),
            asyncio.Event(),
        )
    pools.assert_not_called()


@pytest.mark.asyncio
async def test_native_retirement_access_dispatches_only_approved_phase(monkeypatch):
    from workspace_provisioning import retirement_access_runtime

    monkeypatch.setenv("SUPERPLANE_PAID_WORKER_MODE", "native-lifecycle")
    monkeypatch.setenv("SUPERPLANE_LIFECYCLE_POLICY_FILE", "/run/policy/lifecycle.json")
    monkeypatch.setenv("SUPERPLANE_LIFECYCLE_STATE_DIR", "/run/state")
    operation = SimpleNamespace(
        job_id="job",
        grant=SimpleNamespace(
            lease=SimpleNamespace(org_id="org", workspace_id="workspace")
        ),
        request=SimpleNamespace(
            parameters={"lifecycle_phase": "prepare-retirement-access"}
        ),
    )
    transport = SimpleNamespace(
        post=AsyncMock(return_value={}),
        _verified_operation=Mock(return_value=operation),
    )
    domain = SimpleNamespace(acquire=Mock())
    execution = SimpleNamespace(acquire=Mock())
    monkeypatch.setattr(
        task_worker, "pools", AsyncMock(return_value=(domain, execution))
    )
    run_access = AsyncMock()
    monkeypatch.setattr(retirement_access_runtime, "run_retirement_access", run_access)
    await task_worker.execute(
        transport,
        {
            "mode": "execute",
            "operation_id": "operation",
            "job_id": "job",
            "org_id": "org",
            "workspace_id": "workspace",
        },
        datetime.now(UTC),
        asyncio.Event(),
    )
    run_access.assert_awaited_once()
    passed_operation, context = run_access.await_args.args
    assert passed_operation is operation
    assert context.authority is transport
    assert context.connect is execution.acquire
