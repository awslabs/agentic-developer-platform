import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harness_jobs.identity import OperationRefused
from superplane_executor import task_worker
from superplane_executor.observation_only import VARIABLE, require_selected

SELECTOR = {
    k: k + "-original"
    for k in ("operation_id", "org_id", "workspace_id", "job_id", "attempt_id")
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"mode": "execution"},
        {"workspace_id": "foreign"},
        {"operation_id": "successor"},
        {"attempt_id": "other"},
    ],
)
async def test_observer_refuses_before_run_credential_or_execution(monkeypatch, change):
    monkeypatch.setenv(VARIABLE, json.dumps(SELECTOR))
    original = {**SELECTOR, "mode": "recovery", **change}
    transport = SimpleNamespace(
        post=AsyncMock(
            return_value={"body": json.dumps({"domain_operation": original})}
        )
    )
    with pytest.raises(OperationRefused, match="observer refuses"):
        await task_worker.bootstrap(transport)
    assert transport.post.await_args.args[0].endswith("/task/acquire")
    assert transport.post.await_count == 1  # No bootstrap, credential or lease.
    transport.post.reset_mock()
    with pytest.raises(OperationRefused, match="observer refuses"):
        await task_worker.execute(
            transport, original, datetime.now(UTC), asyncio.Event()
        )
    transport.post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "null",
        "[]",
        "{}",
        "not-json",
        json.dumps({**SELECTOR, "permission": "execute"}),
    ],
)
async def test_invalid_selector_never_acquires_a_message(monkeypatch, raw):
    monkeypatch.setenv(VARIABLE, raw)
    transport = SimpleNamespace(post=AsyncMock())
    with pytest.raises(OperationRefused, match="selector"):
        await task_worker.bootstrap(transport)
    transport.post.assert_not_awaited()


def test_exact_recovery_only_and_normal_workers_unchanged(monkeypatch):
    monkeypatch.setenv(VARIABLE, json.dumps(SELECTOR))
    require_selected({**SELECTOR, "mode": "recovery", "domain": "superplane"})
    monkeypatch.delenv(VARIABLE)
    require_selected({**SELECTOR, "mode": "execution"})
