"""Independent direct and descendant activity cursors for delegated chat."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.activity.chat_work import read_work
from src.activity.schemas import InvocationItem
from src.activity.service import ActivityService, WorkActivityPage, _decode_cursor, _encode_cursor

START = "2026-10-01T00:00:00.000000Z"
END = "2026-10-04T00:00:00.000000Z"


def test_direct_and_descendant_indexes_keep_separate_cursors(mock_dynamodb_resource):
    service = ActivityService(table_name="test", dynamodb_resource=mock_dynamodb_resource)
    requests = []

    def query(**kwargs):
        requests.append(kwargs)
        index = kwargs["IndexName"]
        starting = kwargs.get("ExclusiveStartKey", {}).get("event_id")
        offset = int(starting.rsplit(":", 1)[-1]) if starting else 0
        if offset < 20:
            return {"Items": [], "LastEvaluatedKey": {"event_id": f"{index}:{offset + 1}"}}
        if index == "root-human-index":
            return {
                "Items": [
                    {
                        "event_id": "child",
                        "arrived_at": "2026-10-02T11:00:00Z",
                        "root_human_id": "owner",
                        "tenant_id": "tenant-a",
                        "status": "complete",
                    }
                ]
            }
        return {"Items": []}

    service._table.query.side_effect = query
    first = service.query_work_by_user("owner", tenant_id="tenant-a", page_size=1, since=START, until=END)
    assert first.items == []
    assert first.direct_cursor is not None and first.descendant_cursor is not None
    second = service.query_work_by_user(
        "owner",
        tenant_id="tenant-a",
        page_size=1,
        since=START,
        until=END,
        first_page=False,
        direct_cursor=first.direct_cursor,
        descendant_cursor=first.descendant_cursor,
    )
    assert [item.invocation_id for item in second.items] == ["child"]
    assert second.direct_cursor is None and second.descendant_cursor is None
    assert all(request["IndexName"] in {"user-index", "root-human-index"} for request in requests)
    assert all(request["Limit"] == 1 for request in requests)


def test_composite_cursor_follows_empty_filtered_pages_and_binds_scope(monkeypatch):
    monkeypatch.delenv("ADP_TASK_API_READ_ENABLED", raising=False)
    service = MagicMock()
    service.query_work_by_user.side_effect = [
        WorkActivityPage(items=[], direct_cursor="direct-next", descendant_cursor="descendant-next"),
        WorkActivityPage(
            items=[InvocationItem(invocation_id="child", invoked_at="2026-10-03T15:00:00Z")], direct_cursor=None, descendant_cursor=None
        ),
    ]
    launch = SimpleNamespace(user_id="owner", tenant_id="tenant-a")
    first = asyncio.run(read_work(MagicMock(), launch, service, since=START, until=END, page_size=1, last_key=None))
    assert first["runs"] == []
    cursor = first["last_key"]
    assert _decode_cursor(cursor)["direct"] == "direct-next"
    assert _decode_cursor(cursor)["descendants"] == "descendant-next"
    with pytest.raises(HTTPException) as denied:
        asyncio.run(
            read_work(
                MagicMock(), SimpleNamespace(user_id="other", tenant_id="tenant-a"), service, since=START, until=END, page_size=1, last_key=cursor
            )
        )
    assert denied.value.status_code == 400
    forged = _encode_cursor({**_decode_cursor(cursor), "tasks": "foreign-key"})
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(read_work(MagicMock(), launch, service, since=START, until=END, page_size=1, last_key=forged))
    assert invalid.value.status_code == 400
    second = asyncio.run(read_work(MagicMock(), launch, service, since=START, until=END, page_size=1, last_key=cursor))
    assert [item["invocation_id"] for item in second["runs"]] == ["child"]
    assert second["last_key"] is None
    service.query_work_by_user.assert_called_with(
        "owner",
        tenant_id="tenant-a",
        page_size=1,
        since=START,
        until=END,
        direct_cursor="direct-next",
        descendant_cursor="descendant-next",
        first_page=False,
    )


def test_task_cursor_survives_out_of_window_page_without_repeating_completed_source(monkeypatch):
    from src.activity import chat_work
    from src.tasks.read_store import TaskRecord

    monkeypatch.setenv("ADP_TASK_API_READ_ENABLED", "true")
    monkeypatch.setenv("ADP_TASK_API_HUMAN_ENABLED", "true")
    launch = SimpleNamespace(user_id="owner", tenant_id="tenant-a")
    service = MagicMock()
    service.query_work_by_user.return_value = WorkActivityPage(items=[], direct_cursor=None, descendant_cursor=None)
    stamp = "2026-10-02T00:00:00Z"
    task_key = "2026-10-02T00:00:00Z#tsk_12345678-1234-4123-8123-123456789abc"
    outside = TaskRecord("tsk_outside", "before", "tenant-a", "human:owner", "developer", "completed", 1, "2026-09-30T23:59:59Z", stamp, END)
    inside = TaskRecord("tsk_inside", "inside", "tenant-a", "human:owner", "developer", "completed", 1, stamp, stamp, END)
    store = MagicMock()
    store.list_owned.side_effect = [(["tsk_outside"], task_key), (["tsk_inside"], None)]
    store.load_task.side_effect = lambda *, task_id: {"tsk_outside": outside, "tsk_inside": inside}[task_id]
    monkeypatch.setattr(chat_work.task_readthrough, "get_store", lambda: store)
    policy = MagicMock()
    policy.get.return_value = {"status": "active", "task_scopes": ["read"]}
    monkeypatch.setattr(chat_work, "TaskServicePolicyStore", lambda: policy)
    request = MagicMock()
    request.headers = {}
    first = asyncio.run(read_work(request, launch, service, since=START, until=END, page_size=1, last_key=None))
    assert first["runs"] == []
    assert _decode_cursor(first["last_key"])["tasks"] == task_key
    second = asyncio.run(read_work(request, launch, service, since=START, until=END, page_size=1, last_key=first["last_key"]))
    assert [run["invocation_id"] for run in second["runs"]] == ["inside"]
    assert second["last_key"] is None
    assert service.query_work_by_user.call_args.kwargs == {
        "tenant_id": "tenant-a",
        "page_size": 1,
        "since": START,
        "until": END,
        "direct_cursor": None,
        "descendant_cursor": None,
        "first_page": False,
    }
    assert [call.kwargs["after"] for call in store.list_owned.call_args_list] == [None, task_key]
