"""Missing projections must not trigger database/provider startup in an idle pod."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from superplane_executor import service


async def test_idle_projection_does_not_open_dependencies(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPERPLANE_EXECUTION_OPERATION_FILE", str(tmp_path / "absent"))
    serve = AsyncMock()
    monkeypatch.setattr(service, "serve", serve)
    waiting = asyncio.Event()

    async def wait(awaitable, timeout):
        awaitable.close()
        waiting.set()
        await asyncio.Future()

    monkeypatch.setattr(service.asyncio, "wait_for", wait)
    task = asyncio.create_task(service.run(asyncio.Event()))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    serve.assert_not_called()


async def test_recovery_authentication_refusal_opens_no_pools_or_provider(
    monkeypatch, tmp_path
):
    from superplane_executor.recovery_authority import RecoveryAuthority

    for key, value in {
        "ADP_EXECUTION_AUTHORITY_ENDPOINT": "https://gateway.example",
        "AWS_REGION": "us-east-1",
        "ADP_RUN_CREDENTIAL_FILE": str(tmp_path / "run"),
        "ADP_WORKLOAD_TOKEN_FILE": str(tmp_path / "pod"),
    }.items():
        monkeypatch.setenv(key, value)
    refused = AsyncMock(side_effect=PermissionError("run revoked"))
    monkeypatch.setattr(RecoveryAuthority, "recovery_scope", refused)
    pools = AsyncMock()
    monkeypatch.setattr(service.asyncpg, "create_pool", pools)
    with pytest.raises(PermissionError, match="revoked"):
        await service.serve_recovery(asyncio.Event())
    pools.assert_not_called()


@pytest.mark.parametrize("change", ["permission", "expired", "mutable", "missing"])
async def test_recovery_scope_requires_a_live_observation_only_grant(change):
    from datetime import UTC, datetime, timedelta
    from harness_jobs.identity import OperationRefused
    from superplane_executor.recovery_authority import RecoveryAuthority

    data = {
        "version": 1,
        "observation_only": True,
        "not_after": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        "org_id": "adp-org",
        "workspace_id": "workspace",
        "subject": "real-run",
        "permissions": ["workspace:recover"],
    }
    if change == "permission":
        data["permissions"] = ["workspace:provision"]
    elif change == "expired":
        data["not_after"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif change == "mutable":
        data["observation_only"] = False
    else:
        data.pop("subject")
    transport = AsyncMock()
    transport.post.return_value = data
    with pytest.raises(OperationRefused):
        await RecoveryAuthority(transport).recovery_scope()
    transport.resolve.assert_not_called()


async def test_unrelated_live_handoff_cannot_open_execution_service(
    monkeypatch, tmp_path
):
    import json
    from datetime import UTC, datetime, timedelta

    operations = tmp_path / "operations.json"
    operations.write_text(json.dumps(["selected"]))
    handoff = tmp_path / "handoff.json"
    handoff.write_text(
        json.dumps(
            {
                "version": 1,
                "grants": [
                    {
                        "operation_id": "unrelated",
                        "job_id": "job",
                        "attempt_id": "attempt",
                        "not_after": (
                            datetime.now(UTC) + timedelta(minutes=5)
                        ).isoformat(),
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("SUPERPLANE_EXECUTION_OPERATION_FILE", str(operations))
    monkeypatch.setenv("SUPERPLANE_RUN_HANDOFF_FILE", str(handoff))
    serve = AsyncMock()
    monkeypatch.setattr(service, "serve", serve)
    stop = asyncio.Event()

    async def wait(awaitable, timeout):
        stop.set()
        return await awaitable

    monkeypatch.setattr(service.asyncio, "wait_for", wait)
    await service.run(stop)
    serve.assert_not_called()
