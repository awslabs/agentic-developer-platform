"""Read actual DynamoDB filters against a disposable moto store (#5668)."""

from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from src.activity.service import ActivityService
from src.activity.stats_service import StatsService


@pytest.fixture
def stores():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        keys = ["user_id", "root_human_id", "tenant_id", "correlation_id"]
        indexes = ["user-index", "root-human-index", "tenant-index", "correlation-index"]
        table = ddb.create_table(
            TableName="a12-events",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"} for key in ["event_id", "arrived_at", *keys]],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": index,
                    "KeySchema": [{"AttributeName": key, "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
                for key, index in zip(keys, indexes, strict=True)
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        for event, tenant, user, parent, root_human in (
            ("own", "one", "human", None, "human"),
            ("delegated", "one", "bot", "own", "human"),
            ("foreign", "two", "human", "own", "human"),
            ("other-owner", "one", "other", "own", "other"),
            ("sparse-legacy", "one", "bot", "own", None),
        ):
            row = {
                "event_id": event,
                "arrived_at": today + "T09:00:00Z",
                "tenant_id": tenant,
                "user_id": user,
                "root_human_id": root_human,
                "correlation_id": "shared-chain",
                "status": "complete",
            }
            if root_human is None:
                row.pop("root_human_id")
            if parent:
                row["parent_invocation_id"] = parent
            table.put_item(Item=row)
        yield ActivityService(table_name="a12-events", dynamodb_resource=ddb), StatsService(table_name="a12-events", dynamodb_resource=ddb)


def test_owner_and_tenant_must_both_match(stores):
    activity, _ = stores
    assert activity.get_invocation("foreign", user_id="human", tenant_id="one") is None
    assert activity.get_invocation("own", user_id="other", tenant_id="one") is None
    assert activity.get_invocation("delegated", user_id="human", tenant_id="one") is not None


@pytest.mark.parametrize("source", ["direct", "descendant"])
@pytest.mark.parametrize(
    ("start", "end", "expected_ids"),
    [
        (
            "2026-10-02T11:59:59Z",
            "2026-10-02T12:00:00.500Z",
            ["at-whole-start", "whole-second", "at-fractional-start", "inside-final-second"],
        ),
        (
            "2026-10-02T12:00:00.250000Z",
            "2026-10-02T12:00:00.500Z",
            ["at-fractional-start", "inside-final-second"],
        ),
        ("2026-10-02T11:59:59Z", "2026-10-02T12:00:00Z", ["at-whole-start"]),
        (
            "2026-10-02T12:00:00.5Z",
            "2026-10-02T12:00:01Z",
            ["at-fractional-end", "at-fractional-end-padded", "after-fractional-end"],
        ),
    ],
)
@pytest.mark.asyncio
async def test_chat_work_window_preserves_mixed_timestamp_precision(stores, monkeypatch, source, start, end, expected_ids):
    from types import SimpleNamespace

    from fastapi import Request

    from src.activity.chat_work import read_work
    from src.agentauth.chat_data_routes import ActivityWorkRequest, activity_window

    monkeypatch.delenv("ADP_TASK_API_READ_ENABLED", raising=False)
    activity, _ = stores
    identity = {"user_id": "window-owner"} if source == "direct" else {"user_id": "bot", "root_human_id": "window-owner"}
    for event_id, stamp in [
        ("before-window", "2026-10-02T11:59:58.999999Z"),
        ("at-whole-start", "2026-10-02T11:59:59Z"),
        ("whole-second", "2026-10-02T12:00:00Z"),
        ("at-fractional-start", "2026-10-02T12:00:00.250000Z"),
        ("inside-final-second", "2026-10-02T12:00:00.499999Z"),
        ("at-fractional-end", "2026-10-02T12:00:00.5Z"),
        ("at-fractional-end-padded", "2026-10-02T12:00:00.500000Z"),
        ("after-fractional-end", "2026-10-02T12:00:00.900000Z"),
        ("at-next-second", "2026-10-02T12:00:01Z"),
    ]:
        activity._table.put_item(Item={"event_id": event_id, "arrived_at": stamp, "tenant_id": "one", "status": "complete", **identity})
    for event_id, tenant_id, user_id in [("foreign-tenant", "two", "window-owner"), ("foreign-owner", "one", "other")]:
        activity._table.put_item(
            Item={
                "event_id": event_id,
                "arrived_at": "2026-10-02T12:00:00.3Z",
                "tenant_id": tenant_id,
                "user_id": user_id,
                "root_human_id": user_id,
                "status": "complete",
            }
        )
    since, until = activity_window(ActivityWorkRequest.model_validate({"run_id": "chat-run", "from": start, "to": end, "timezone": "UTC"}))
    cursor = None
    record_ids = []
    for _ in range(30):
        result = await read_work(
            Request({"type": "http", "headers": []}),
            SimpleNamespace(user_id="window-owner", tenant_id="one"),
            activity,
            since=since,
            until=until,
            page_size=1,
            last_key=cursor,
            observed_at=datetime(2026, 10, 5, tzinfo=UTC),
        )
        record_ids.extend(run["invocation_id"] for run in result["runs"])
        cursor = result["last_key"]
        if cursor is None:
            break
    else:
        pytest.fail("Activity pagination did not finish")
    assert sorted(record_ids) == sorted(expected_ids)


def test_all_owner_list_and_chain_paths_filter_other_tenants(stores):
    activity, _ = stores
    page = activity.query_by_user("human", tenant_id="one")
    assert {item.invocation_id for item in page.items} == {"own", "delegated"}
    chain = activity.get_chain("shared-chain", user_id="human", tenant_id="one")
    assert chain.total_count == 2
    chains = activity.query_chains_by_user("human", tenant_id="one")
    assert len(chains.chains) == 1
    assert {row.invocation_id for row in chains.chains[0].descendants} == {"delegated"}
    assert activity.get_chain("shared-chain", user_id="human", tenant_id="").total_count == 0


def test_stats_cache_and_cost_enrichment_inputs_remain_tenant_scoped(stores):
    _, stats = stores
    one = stats._fetch_items_merged(user_id="human", tenant_id="one", days=7)
    two = stats._fetch_items_merged(user_id="human", tenant_id="two", days=7)
    assert {row["event_id"] for row in one} == {"own", "delegated"}
    assert {row["event_id"] for row in two} == {"foreign"}
    assert stats.get_stats_by_user("human", tenant_id="one") != stats.get_stats_by_user("human", tenant_id="two")


def test_same_tenant_correlation_does_not_grant_another_owner_or_sparse_legacy_rows(stores):
    activity, _ = stores
    chain = activity.get_chain("shared-chain", user_id="human", tenant_id="one")
    rendered = chain.model_dump_json()
    assert "other-owner" not in rendered
    assert "sparse-legacy" not in rendered
    assert chain.total_count == 2
    admin_chain = activity.get_chain("shared-chain", tenant_id="one")
    assert admin_chain.total_count == 4  # Authorized tenant-wide readers retain all tenant rows.
    assert activity.get_chain("shared-chain", user_id="unrelated", tenant_id="one").total_count == 0


def test_root_backfill_cannot_import_other_owner_even_when_own_child_references_it(stores):
    activity, _ = stores
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    for event, user, parent in (("private-root", "other", None), ("owned-child", "human", "private-root")):
        row = {
            "event_id": event,
            "arrived_at": today + "T10:00:00Z",
            "tenant_id": "one",
            "user_id": user,
            "correlation_id": "mixed-backfill",
            "status": "complete",
        }
        if parent:
            row["parent_invocation_id"] = parent
        activity._table.put_item(Item=row)
    chains = activity.query_chains_by_user("human", tenant_id="one")
    mixed = next(chain for chain in chains.chains if chain.chain_id == "mixed-backfill")
    assert mixed.root.invocation_id == "owned-child"
    assert mixed.descendants == []
    assert "private-root" not in [row.invocation_id for row in mixed.descendants]


def test_owner_transcripts_are_readable_but_cross_user_and_tenant_ids_are_hidden(stores, monkeypatch):
    from io import BytesIO
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.activity import routes

    activity, _ = stores
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    for invocation_id in ("own", "delegated", "foreign", "other-owner"):
        activity._table.update_item(
            Key={"event_id": invocation_id, "arrived_at": today + "T09:00:00Z"},
            UpdateExpression="SET transcript_key = :key",
            ExpressionAttributeValues={":key": f"reports/{invocation_id}.md"},
        )
    monkeypatch.setattr(routes, "resolve_canonical_user_id", AsyncMock(return_value="human"))
    monkeypatch.setattr(routes, "_get_run_logs_bucket", lambda: "fixture-run-logs")
    storage = MagicMock()
    storage.get_object.side_effect = lambda **kwargs: {"Body": BytesIO(f"# {kwargs['Key']}".encode())}
    monkeypatch.setattr(routes, "_get_s3_client", lambda: storage)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_current_user] = lambda: SimpleNamespace(user_id="opaque-sub", org_id="one")
    app.dependency_overrides[routes.get_db] = lambda: None
    app.dependency_overrides[routes.get_activity_service] = lambda: activity
    with TestClient(app) as client:
        for invocation_id in ("own", "delegated"):
            response = client.get(f"/me/agent-invocations/{invocation_id}/transcript")
            assert response.status_code == 200, response.text
            assert response.text == f"# reports/{invocation_id}.md"
        for invocation_id in ("foreign", "other-owner"):
            response = client.get(
                f"/me/agent-invocations/{invocation_id}/transcript",
                params={"user_id": "other", "tenant_id": "two"},
            )
            assert response.status_code == 404, response.text
    assert storage.get_object.call_count == 2
    assert {call.kwargs["Key"] for call in storage.get_object.call_args_list} == {"reports/own.md", "reports/delegated.md"}
