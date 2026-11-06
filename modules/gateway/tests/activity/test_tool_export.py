"""Tool export reads the full tenant/date index, including pages with no matching tools."""

import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import boto3
import pyarrow.parquet as parquet
import pytest
from moto import mock_aws

from src.activity.service import ActivityService


@pytest.fixture
def export_service():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="tool-export-test",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "arrived_at", "AttributeType": "S"},
                {"AttributeName": "tenant_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "tenant-index",
                    "KeySchema": [{"AttributeName": "tenant_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ActivityService(table_name="tool-export-test", dynamodb_resource=resource)


def test_owned_tool_admission_has_correlation_and_unsettled_cost_without_payload(export_service):
    export_service.record_tool_call(
        receipt_id="receipt-one",
        request_id="request-one",
        execution_id="execution-one",
        user_id="person-one",
        tenant_id="tenant-one",
        team_id="team-one",
        tool="browser___start",
        estimated_cost_usd=Decimal("0.05"),
        run_id="run-one",
    )
    item = export_service._table.scan()["Items"][0]
    assert {
        name: item[name]
        for name in (
            "tenant_id",
            "team_id",
            "user_id",
            "run_id",
            "tool_name",
            "receipt_id",
            "request_id",
            "execution_id",
        )
    } == {
        "tenant_id": "tenant-one",
        "team_id": "team-one",
        "user_id": "person-one",
        "run_id": "run-one",
        "tool_name": "browser___start",
        "receipt_id": "receipt-one",
        "request_id": "request-one",
        "execution_id": "execution-one",
    }
    assert item["estimated_cost_usd"] == Decimal("0.05")
    assert item["settled_cost_usd"] is None
    assert item["accounting_status"] == "estimate_only"
    assert item["observed_usage_status"] == "not_observed"
    assert item["reservation_status"] == "estimated_hold"
    assert not {"arguments", "result", "credential", "payload"} & item.keys()

    owned = ActivityService._map_item(item)
    assert owned.receipt_id == "receipt-one" and owned.request_id == "request-one"
    assert owned.execution_id == "execution-one" and owned.accounting_status == "estimate_only"
    assert owned.settled_cost_usd is None and owned.observed_usage_status == "not_observed"

    today = datetime.now(UTC).date()
    csv_bytes = export_service.export_tool_calls(tenant_id="tenant-one", since=today, until=today, format="csv")
    row = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))[0]
    assert row["request_id"] == "request-one" and row["execution_id"] == "execution-one"
    assert row["estimated_cost_usd"] == "0.05" and row["settled_cost_usd"] == ""
    assert row["accounting_status"] == "estimate_only" and row["reservation_status"] == "estimated_hold"
    assert not {"arguments", "result", "credential", "payload"} & row.keys()
    parquet_row = parquet.read_table(
        io.BytesIO(
            export_service.export_tool_calls(
                tenant_id="tenant-one",
                since=today,
                until=today,
                format="parquet",
            )
        )
    ).to_pylist()[0]
    assert parquet_row["settled_cost_usd"] is None
    assert parquet_row["estimated_cost_usd"] == Decimal("0.0500000000")


@pytest.mark.parametrize(
    "foreign_field",
    [
        "receipt_id",
        "tenant_id",
        "user_id",
        "team_id",
        "run_id",
        "tool",
        "request_id",
        "execution_id",
    ],
)
def test_tool_result_updates_only_original_owned_receipt_without_settling_cost(export_service, foreign_field):
    export_service.record_tool_call(
        receipt_id="receipt-one",
        request_id="request-one",
        execution_id="execution-one",
        user_id="person-one",
        tenant_id="tenant-one",
        team_id="team-one",
        tool="websearch___search",
        estimated_cost_usd=Decimal("0.05"),
        run_id="run-one",
    )
    outcome = {
        "receipt_id": "receipt-one",
        "request_id": "request-one",
        "execution_id": "execution-one",
        "tenant_id": "tenant-one",
        "user_id": "person-one",
        "team_id": "team-one",
        "run_id": "run-one",
        "tool": "websearch___search",
        "result_status": "unknown",
    }
    with pytest.raises(ValueError, match="not found"):
        export_service.record_tool_outcome(**{**outcome, foreign_field: "foreign"})
    assert "result_status" not in export_service._table.scan()["Items"][0]
    export_service.record_tool_outcome(**outcome)
    export_service.record_tool_outcome(**{**outcome, "result_status": "confirmed", "cleanup_status": "unknown"})
    export_service.record_tool_outcome(**{**outcome, "cleanup_status": "confirmed"})
    export_service.record_tool_outcome(**outcome)
    item = export_service._table.scan()["Items"][0]
    assert item["result_status"] == item["cleanup_status"] == "confirmed"
    assert item["accounting_status"] == "estimate_only" and item["settled_cost_usd"] is None
    assert item["observed_usage_status"] == "not_observed" and item["reservation_status"] == "estimated_hold"
    assert item["execution_id"] == "execution-one"
    assert not {"arguments", "result", "payload", "credential"} & item.keys()

    today = datetime.now(UTC).date()
    csv_row = list(
        csv.DictReader(
            io.StringIO(
                export_service.export_tool_calls(
                    tenant_id="tenant-one",
                    since=today,
                    until=today,
                    format="csv",
                ).decode("utf-8")
            )
        )
    )[0]
    assert csv_row["result_status"] == csv_row["cleanup_status"] == "confirmed"
    assert csv_row["settled_cost_usd"] == "" and csv_row["observed_usage_status"] == "not_observed"
    parquet_row = parquet.read_table(
        io.BytesIO(
            export_service.export_tool_calls(
                tenant_id="tenant-one",
                since=today,
                until=today,
                format="parquet",
            )
        )
    ).to_pylist()[0]
    assert parquet_row["result_status"] == "confirmed" and parquet_row["settled_cost_usd"] is None
    owned = ActivityService._map_item(item)
    assert owned.result_status == owned.cleanup_status == "confirmed"
    assert owned.settled_cost_usd is None


def test_export_rows_are_scoped_to_tenant_and_day_in_both_formats(export_service):
    today = datetime.now(UTC).date()
    export_service.record_tool_call(
        receipt_id="first",
        user_id="person-one",
        tenant_id="tenant-one",
        team_id="team-one",
        tool="websearch___search",
        estimated_cost_usd=Decimal("0.05"),
        run_id="run-one",
        task_id="tsk-verified",
        story_id="example/project#123",
    )
    export_service.record_tool_call(
        receipt_id="foreign",
        user_id="person-two",
        tenant_id="tenant-two",
        team_id="team-two",
        tool="websearch___search",
        estimated_cost_usd=Decimal("0.09"),
        run_id=None,
    )
    csv_bytes = export_service.export_tool_calls(tenant_id="tenant-one", since=today, until=today, format="csv")
    records = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))
    assert len(records) == 1
    assert records[0]["call_id"] == records[0]["receipt_id"] == "first"
    assert (records[0]["task_id"], records[0]["story_id"]) == ("tsk-verified", "example/project#123")
    assert (records[0]["tool"], records[0]["estimated_cost_usd"]) == ("websearch___search", "0.05")
    assert (records[0]["tenant_id"], records[0]["user_id"], records[0]["team_id"]) == ("tenant-one", "person-one", "team-one")
    table = parquet.read_table(io.BytesIO(export_service.export_tool_calls(tenant_id="tenant-one", since=today, until=today, format="parquet")))
    assert table.num_rows == 1
    assert table.to_pylist()[0]["call_id"] == "first"
    assert table.to_pylist()[0]["estimated_cost_usd"] == Decimal("0.0500000000")
    assert (
        list(
            csv.DictReader(
                io.StringIO(
                    export_service.export_tool_calls(
                        tenant_id="tenant-one",
                        since=today - timedelta(days=1),
                        until=today - timedelta(days=1),
                        format="csv",
                    ).decode("utf-8")
                )
            )
        )
        == []
    )


@pytest.mark.parametrize("format", ["csv", "parquet"])
def test_export_includes_entire_utc_day_without_adjacent_days(export_service, format):
    day = date(2026, 10, 13)
    timestamps = {
        "previous-day": "2026-10-12T23:59:59.999Z",
        "midnight": "2026-10-13T00:00:00.000Z",
        "first-second": "2026-10-13T00:00:00.999Z",
        "last-millisecond": "2026-10-13T23:59:59.999Z",
        "next-day": "2026-10-14T00:00:00.000Z",
    }
    for receipt, timestamp in timestamps.items():
        export_service._table.put_item(
            Item={
                "event_id": f"tool#{receipt}",
                "arrived_at": timestamp,
                "tenant_id": "tenant-one",
                "user_id": "person-one",
                "channel": "tool",
                "status": "tool_authorized",
                "call_id": receipt,
                "receipt_id": receipt,
                "tool_name": "browser___open",
                "estimated_cost_usd": Decimal("0.01"),
            }
        )

    payload = export_service.export_tool_calls(tenant_id="tenant-one", since=day, until=day, format=format)
    if format == "csv":
        rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8"))))
    else:
        rows = parquet.read_table(io.BytesIO(payload)).to_pylist()
    assert {row["call_id"] for row in rows} == {"midnight", "first-second", "last-millisecond"}


def test_export_pages_past_filtered_rows_without_skipping_calls(export_service, monkeypatch):
    today = datetime.now(UTC).date()
    export_service._table.put_item(
        Item={
            "event_id": "other",
            "arrived_at": f"{today}T00:00:00Z",
            "tenant_id": "tenant-one",
            "channel": "model",
            "status": "complete",
        }
    )
    for receipt in ("first", "second"):
        export_service.record_tool_call(
            receipt_id=receipt,
            user_id="person-one",
            tenant_id="tenant-one",
            tool="browser___open",
            estimated_cost_usd=Decimal("0.01"),
            run_id=None,
        )
    original_query = export_service._table.query
    calls = []

    def query(**kwargs):
        calls.append(kwargs)
        return original_query(**kwargs, Limit=1)

    monkeypatch.setattr(export_service._table, "query", query)
    rows = list(
        csv.DictReader(
            io.StringIO(
                export_service.export_tool_calls(
                    tenant_id="tenant-one",
                    since=today,
                    until=today,
                    format="csv",
                ).decode("utf-8")
            )
        )
    )
    assert {row["call_id"] for row in rows} == {"first", "second"}
    assert all(not row["task_id"] and not row["story_id"] for row in rows)
    assert len(calls) >= 3
    assert "ExclusiveStartKey" not in calls[0]
    assert all("ExclusiveStartKey" in call for call in calls[1:])


@pytest.mark.parametrize("format", ["csv", "parquet"])
def test_empty_export_still_has_a_schema(export_service, format):
    today = date.today() - timedelta(days=10)
    payload = export_service.export_tool_calls(tenant_id="tenant-one", since=today, until=today, format=format)
    if format == "csv":
        assert "call_id,task_id,story_id" in payload.decode("utf-8")
        assert list(csv.DictReader(io.StringIO(payload.decode("utf-8")))) == []
    else:
        table = parquet.read_table(io.BytesIO(payload))
        assert table.num_rows == 0 and "call_id" in table.column_names


def test_export_refuses_reversed_range_and_unsupported_format(export_service):
    today = date.today()
    with pytest.raises(ValueError, match="end date"):
        export_service.export_tool_calls(tenant_id="tenant-one", since=today, until=today - timedelta(days=1), format="csv")
    with pytest.raises(ValueError, match="unsupported"):
        export_service.export_tool_calls(tenant_id="tenant-one", since=today, until=today, format="json")


def test_populated_day_export_through_http_and_unavailable_index_is_not_empty_success(export_service, org_admin_user, monkeypatch):
    from unittest.mock import AsyncMock

    from botocore.exceptions import ClientError
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.activity.routes import get_access_control, get_activity_service, router
    from src.auth.dependencies import get_current_user

    today = datetime.now(UTC).date()
    export_service.record_tool_call(
        receipt_id="day-call",
        user_id="person-one",
        tenant_id=org_admin_user.org_id,
        team_id="team-one",
        tool="websearch___search",
        estimated_cost_usd=Decimal("0.05"),
        run_id="run-one",
        task_id="tsk-verified",
        story_id="example/project#123",
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: org_admin_user
    app.dependency_overrides[get_activity_service] = lambda: export_service
    app.dependency_overrides[get_access_control] = lambda: type("Access", (), {"check_permission": AsyncMock(return_value=True)})()
    client = TestClient(app)
    path = f"/admin/tool-calls/export?since={today}&until={today}"
    response = client.get(path)
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1
    assert {key: rows[0][key] for key in ("call_id", "task_id", "tool", "estimated_cost_usd")} == {
        "call_id": "day-call",
        "task_id": "tsk-verified",
        "tool": "websearch___search",
        "estimated_cost_usd": "0.05",
    }
    assert parquet.read_table(io.BytesIO(client.get(path + "&format=parquet").content)).num_rows == 1

    def unavailable(**kwargs):
        raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}}, "Query")

    monkeypatch.setattr(export_service._table, "query", unavailable)
    assert client.get(path).status_code == 503
