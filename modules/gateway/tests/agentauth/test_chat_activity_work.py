"""Delegated chat Activity reads bind scope to the verified launch."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.activity.routes import get_activity_service
from src.activity.schemas import InvocationItem
from src.activity.service import WorkActivityPage, _decode_cursor
from src.agentauth import chat_data_routes
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.workload import WORKLOAD_HEADER
from tests.agentauth.chat_activity_fixtures import HEADERS, workload_runtime

URL = "/v1/chat/data/activity/work"
WINDOW = {"from": "2026-10-01T00:00:00Z", "to": "2026-10-04T00:00:00Z", "timezone": "UTC"}


def test_delegated_work_uses_verified_identity_and_retains_lineage(monkeypatch):
    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "true")
    monkeypatch.delenv("ADP_TASK_API_READ_ENABLED", raising=False)
    capabilities = MagicMock()
    capabilities.verify_run.return_value = SimpleNamespace(user_id="owner", tenant_id="tenant-a")
    service = MagicMock()
    service.query_work_by_user.return_value = WorkActivityPage(
        items=[
            InvocationItem(
                invocation_id="child",
                invoked_at="2026-10-02T08:00:00Z",
                status="failed",
                repo="sample/widgets",
                issue_number=14,
                persona="reviewer",
                trigger_kind="agent",
                triggered_by_invocation_id="root",
                correlation_id="chain-a",
            )
        ],
        direct_cursor="next",
        descendant_cursor=None,
    )
    services = workload_runtime(capabilities)
    app = FastAPI()
    app.include_router(chat_data_routes.router)
    app.dependency_overrides[chat_data_routes.runtime] = lambda: services
    app.dependency_overrides[get_activity_service] = lambda: service
    with TestClient(app) as client:
        for workload in (None, "foreign-workload"):
            headers = {"Authorization": "Bearer delegated"}
            if workload is not None:
                headers[WORKLOAD_HEADER] = workload
            refused = client.post(URL, headers=headers, json={**WINDOW, "run_id": "run-a"})
            assert refused.status_code == 404
        capabilities.verify_pod.assert_not_called()
        refused = client.post(URL, headers={**HEADERS, "Authorization": "Bearer forged"}, json={**WINDOW, "run_id": "run-a"})
        assert refused.status_code == 404
        assert refused.headers["cache-control"] == "no-store"
        capabilities.verify_pod.assert_called_once()
        capabilities.verify_run.assert_not_called()
        service.query_work_by_user.assert_not_called()
        response = client.post(URL, headers=HEADERS, json={**WINDOW, "run_id": "run-a"})
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["runs"][0]["parent_invocation_id"] == "root"
        assert response.json()["issues"][0]["invocation_ids"] == ["child"]
        assert _decode_cursor(response.json()["last_key"])["direct"] == "next"
        assert service.query_work_by_user.call_args.args == ("owner",)
        assert service.query_work_by_user.call_args.kwargs["tenant_id"] == "tenant-a"
        capabilities.verify_run.assert_called_with("delegated", run_id="run-a", operation="activity.read", now=chat_data_routes.clock())
        for forged in ({"user_id": "victim"}, {"tenant_id": "tenant-b"}):
            assert client.post(URL, headers=HEADERS, json={**WINDOW, "run_id": "run-a", **forged}).status_code == 422
        capabilities.verify_run.side_effect = ChatAuthorizationRefusedError("scope refused")
        assert client.post(URL, headers=HEADERS, json={**WINDOW, "run_id": "forged"}).status_code == 404
    assert service.query_work_by_user.call_count == 1


def test_delegated_task_read_uses_canonical_owner_and_active_policy(monkeypatch):
    from src.activity import chat_work
    from src.tasks.read_store import TaskRecord

    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "true")
    monkeypatch.setenv("ADP_TASK_API_READ_ENABLED", "true")
    monkeypatch.setenv("ADP_TASK_API_HUMAN_ENABLED", "true")
    capabilities = MagicMock()
    capabilities.verify_run.return_value = SimpleNamespace(user_id="owner", tenant_id="tenant-a")
    service = MagicMock()
    service.query_work_by_user.return_value = WorkActivityPage(items=[], direct_cursor=None, descendant_cursor=None)
    task = TaskRecord(
        "tsk_owned",
        "task-run",
        "tenant-a",
        "human:owner",
        "developer",
        "completed",
        1,
        "2026-10-02T08:00:00Z",
        "2026-10-02T09:00:00Z",
        "2026-10-03T00:00:00Z",
        result={"report": {"summary": "Created a patch"}},
    )
    foreign = TaskRecord(
        "tsk_foreign",
        "foreign-run",
        "tenant-b",
        "human:intruder",
        "developer",
        "completed",
        1,
        "2026-10-02T08:00:00Z",
        "2026-10-02T09:00:00Z",
        "2026-10-03T00:00:00Z",
    )
    store = MagicMock()
    store.list_owned.return_value = (["tsk_owned", "tsk_foreign"], None)
    store.load_task.side_effect = lambda *, task_id: {"tsk_owned": task, "tsk_foreign": foreign}[task_id]
    monkeypatch.setattr(chat_work.task_readthrough, "get_store", lambda: store)
    policy = MagicMock()
    policy.get.return_value = {"status": "active", "task_scopes": ["read"]}
    monkeypatch.setattr(chat_work, "TaskServicePolicyStore", lambda: policy)
    services = workload_runtime(capabilities)
    app = FastAPI()
    app.include_router(chat_data_routes.router)
    app.dependency_overrides[chat_data_routes.runtime] = lambda: services
    app.dependency_overrides[get_activity_service] = lambda: service
    with TestClient(app) as client:
        response = client.post(URL, headers=HEADERS, json={**WINDOW, "run_id": "run-a"})
    assert response.status_code == 200, response.text
    assert [run["invocation_id"] for run in response.json()["runs"]] == ["task-run"]
    assert response.json()["runs"][0]["task_result"] == {"report": {"summary": "Created a patch"}}
    assert response.json()["runs"][0]["evidence"] == {"task_report": "/me/agent-invocations/task-run/transcript"}
    store.list_owned.assert_called_once_with(tenant="tenant-a", principal="human:owner", limit=20, after=None)
    store.require_policy.assert_called_once_with(tenant="tenant-a", principal="human:owner", persona="developer")
    policy.get.assert_called_once_with(tenant_id="tenant-a", canonical_principal_id="human:owner")


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ({"from": "2026-10-02T01:00:00Z", "to": "2026-10-02T02:00:00Z", "timezone": "UTC"}, ["start", "inside"]),
        ({"from": "2026-03-08T01:30:00-05:00", "to": "2026-03-08T03:30:00-04:00", "timezone": "America/New_York"}, ["start", "inside"]),
        ({"from": "2026-11-01T01:30:00-04:00", "to": "2026-11-01T01:30:00-05:00", "timezone": "America/New_York"}, ["start", "inside"]),
    ],
)
def test_half_open_utc_and_dst_windows(window, expected, monkeypatch):
    from datetime import datetime, timedelta

    from src.agentauth.chat_data_routes import ActivityWorkRequest, activity_window

    since, until = activity_window(ActivityWorkRequest.model_validate({**window, "run_id": "run-a"}))
    start = datetime.fromisoformat(since.replace("Z", "+00:00"))
    end = datetime.fromisoformat(until.replace("Z", "+00:00"))
    stamps = [start - timedelta(microseconds=1), start, end - timedelta(microseconds=1), end]
    records = [
        InvocationItem(invocation_id=key, invoked_at=stamp.isoformat().replace("+00:00", "Z"))
        for key, stamp in zip(("before", "start", "inside", "end"), stamps)
    ]
    monkeypatch.delenv("ADP_TASK_API_READ_ENABLED", raising=False)
    launch = SimpleNamespace(user_id="owner", tenant_id="tenant-a")
    service = MagicMock()
    service.query_work_by_user.return_value = WorkActivityPage(items=records, direct_cursor=None, descendant_cursor=None)
    import asyncio

    from src.activity.chat_work import read_work

    result = asyncio.run(read_work(MagicMock(), launch, service, since=since, until=until, page_size=20, last_key=None))
    assert [run["invocation_id"] for run in result["runs"]] == expected
    assert result["last_key"] is None


def test_ambiguous_or_nonexistent_local_dst_time_requires_offset():
    from fastapi import HTTPException

    from src.agentauth.chat_data_routes import ActivityWorkRequest, activity_window

    for local in ("2026-03-08T02:30:00", "2026-11-01T01:30:00"):
        with pytest.raises(HTTPException) as error:
            activity_window(
                ActivityWorkRequest.model_validate({"run_id": "run-a", "from": local, "to": "2026-11-02T00:00:00Z", "timezone": "America/New_York"})
            )
        assert error.value.status_code == 422
