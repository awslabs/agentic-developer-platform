"""ACT03: incomplete sources never become assertions that no agent work exists."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from src.activity import chat_work
from src.activity.schemas import InvocationItem
from src.activity.service import ActivityService, WorkActivityPage
from src.agentauth.task_service_policy import TaskServicePolicyError

START = "2026-10-02T00:00:00.000000Z"
END = "2026-10-04T00:00:00.000000Z"
LAUNCH = SimpleNamespace(user_id="owner", tenant_id="tenant-a")
OBSERVED = datetime(2026, 10, 5, tzinfo=UTC)


def read(service, *, since=START, until=END):
    return asyncio.run(chat_work.read_work(MagicMock(), LAUNCH, service, since=since, until=until, page_size=20, last_key=None, observed_at=OBSERVED))


def failure(code):
    return ClientError({"Error": {"Code": code, "Message": "synthetic failure"}}, "Query")


def test_missing_descendant_index_keeps_direct_work_with_partial_coverage(mock_dynamodb_resource, monkeypatch):
    monkeypatch.delenv("ADP_TASK_API_READ_ENABLED", raising=False)
    service = ActivityService(table_name="fixture", dynamodb_resource=mock_dynamodb_resource)

    def query(**kwargs):
        if kwargs["IndexName"] == "root-human-index":
            raise failure("ValidationException")
        return {
            "Items": [
                {
                    "event_id": "direct",
                    "arrived_at": "2026-10-03T11:00:00Z",
                    "tenant_id": "tenant-a",
                    "user_id": "owner",
                    "status": "complete",
                    "summary": "Patch proposed",
                }
            ]
        }

    service._table.query.side_effect = query
    result = read(service)
    assert [item["invocation_id"] for item in result["runs"]] == ["direct"]
    assert result["status"] == "partial"
    assert {entry["reason"] for entry in result["coverage"] if entry["status"] != "available"} >= {"index_missing", "not_enabled"}
    assert result["from"] == START and result["to"] == END
    assert result["observed_at"] == "2026-10-05T00:00:00Z"


def test_all_indexes_missing_is_unavailable_not_empty(mock_dynamodb_resource, monkeypatch):
    monkeypatch.delenv("ADP_TASK_API_READ_ENABLED", raising=False)
    service = ActivityService(table_name="fixture", dynamodb_resource=mock_dynamodb_resource)
    service._table.query.side_effect = failure("ResourceNotFoundException")
    result = read(service)
    assert result["runs"] == [] and result["status"] == "unavailable"
    assert {(entry["source"], entry["reason"]) for entry in result["coverage"]} >= {
        ("activity_direct", "index_missing"),
        ("activity_descendants", "index_missing"),
        ("tasks", "not_enabled"),
    }


def test_expired_retention_window_never_returns_a_complete_empty_result(mock_dynamodb_resource, monkeypatch):
    monkeypatch.delenv("ADP_TASK_API_READ_ENABLED", raising=False)
    service = ActivityService(table_name="fixture", dynamodb_resource=mock_dynamodb_resource)
    service._table.query.return_value = {"Items": []}
    result = read(service, since="2026-08-01T00:00:00Z", until="2026-08-02T00:00:00Z")
    assert result["runs"] == [] and result["status"] == "partial"
    assert {entry["reason"] for entry in result["coverage"]} >= {"window_before_retention"}
    gap = next(entry for entry in result["coverage"] if entry["source"] == "activity_retention")
    assert gap["from"] == "2026-08-01T00:00:00Z" and gap["to"] == "2026-08-02T00:00:00Z"


def test_provider_failure_keeps_other_index_records(mock_dynamodb_resource, monkeypatch):
    monkeypatch.delenv("ADP_TASK_API_READ_ENABLED", raising=False)
    service = ActivityService(table_name="fixture", dynamodb_resource=mock_dynamodb_resource)

    def query(**kwargs):
        if kwargs["IndexName"] == "user-index":
            raise failure("ProvisionedThroughputExceededException")
        return {
            "Items": [
                {
                    "event_id": "descendant",
                    "arrived_at": "2026-10-03T10:00:00Z",
                    "tenant_id": "tenant-a",
                    "root_human_id": "owner",
                    "status": "failed",
                    "error_message": "Run failed",
                }
            ]
        }

    service._table.query.side_effect = query
    result = read(service)
    assert [item["invocation_id"] for item in result["runs"]] == ["descendant"]
    assert result["runs"][0]["error"] == "Run failed" and result["status"] == "partial"
    assert ("activity_direct", "provider_failure") in {(item["source"], item["reason"]) for item in result["coverage"]}


@pytest.mark.parametrize("failure_point", ["policy", "task_store"])
def test_task_failure_keeps_activity_and_exposes_unavailable_coverage(monkeypatch, failure_point):
    monkeypatch.setenv("ADP_TASK_API_READ_ENABLED", "true")
    monkeypatch.setenv("ADP_TASK_API_HUMAN_ENABLED", "true")
    service = MagicMock()
    service.query_work_by_user.return_value = WorkActivityPage(
        items=[InvocationItem(invocation_id="legacy", invoked_at="2026-10-03T09:00:00Z", summary="Run started")],
        direct_cursor=None,
        descendant_cursor=None,
        coverage=[{"source": "activity_direct", "status": "available", "reason": "queried"}],
    )
    policy = MagicMock()
    if failure_point == "policy":
        policy.get.side_effect = TaskServicePolicyError("unavailable")
    else:
        policy.get.return_value = {"status": "active", "task_scopes": ["read"]}
        monkeypatch.setattr(chat_work.task_readthrough, "get_store", MagicMock(side_effect=failure("ProvisionedThroughputExceededException")))
    monkeypatch.setattr(chat_work, "TaskServicePolicyStore", lambda: policy)
    result = read(service)
    assert [item["invocation_id"] for item in result["runs"]] == ["legacy"]
    assert result["status"] == "partial"
    assert ("tasks", "provider_failure") in {(item["source"], item["reason"]) for item in result["coverage"]}


def test_missing_summary_and_transcript_are_explicitly_incomplete(monkeypatch):
    monkeypatch.delenv("ADP_TASK_API_READ_ENABLED", raising=False)
    service = MagicMock()
    service.query_work_by_user.return_value = WorkActivityPage(
        items=[InvocationItem(invocation_id="legacy", invoked_at="2026-10-03T09:00:00Z", status="complete")],
        direct_cursor=None,
        descendant_cursor=None,
        coverage=[{"source": "activity_direct", "status": "available", "reason": "queried"}],
    )
    result = read(service)
    assert result["status"] == "partial"
    assert {entry["source"] for entry in result["coverage"] if entry["reason"] == "missing"} == {"summary"}
    assert {entry["source"] for entry in result["coverage"] if entry["reason"] == "missing_or_pending"} == {"transcript"}
    assert result["runs"][0]["status"] == "complete" and result["runs"][0]["summary"] is None


def test_empty_continuation_after_twenty_pages_is_not_complete_window_coverage(monkeypatch):
    monkeypatch.setenv("ADP_TASK_API_READ_ENABLED", "true")
    monkeypatch.setenv("ADP_TASK_API_HUMAN_ENABLED", "true")
    policy = MagicMock()
    policy.get.side_effect = TaskServicePolicyError("unavailable")
    monkeypatch.setattr(chat_work, "TaskServicePolicyStore", lambda: policy)
    service = MagicMock()
    available = {"source": "activity_direct", "status": "available", "reason": "queried"}
    missing = {"source": "activity_descendants", "status": "unavailable", "reason": "index_missing"}
    service.query_work_by_user.side_effect = [
        WorkActivityPage(
            items=[],
            direct_cursor=f"page-{page_number + 1}" if page_number < 21 else None,
            descendant_cursor=None,
            coverage=[available, missing] if page_number == 1 else [available],
        )
        for page_number in range(1, 22)
    ]
    cursor = None
    for page_number in range(1, 22):
        result = asyncio.run(
            chat_work.read_work(MagicMock(), LAUNCH, service, since=START, until=END, page_size=20, last_key=cursor, observed_at=OBSERVED)
        )
        if page_number == 1:
            assert result["status"] == "partial"
            assert {entry["reason"] for entry in result["coverage"]} >= {"index_missing", "provider_failure"}
        cursor = result["last_key"]
        assert bool(cursor) == (page_number < 21)
    assert result["runs"] == []
    assert result["status"] == "partial"
    assert {"source": "pagination", "status": "partial", "reason": "continuation_only"} in result["coverage"]
    assert service.query_work_by_user.call_count == 21
    policy.get.assert_called_once()
