"""Summary persistence through admitted routes and DynamoDB/SQL emulation."""

import asyncio
from threading import Barrier, Lock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import delete

from src.shared.models.organization import TeamMembership
from tests.agentauth.test_chat_history_routes import seed_history
from tests.agentauth.test_chat_history_write import HEADER, append, read, rows
from tests.agentauth.test_chat_history_write import capability as admitted_capability_fixture
from tests.agentauth.test_chat_history_write import client as client_fixture
from tests.agentauth.test_chat_history_write import runtime as runtime_fixture
from tests.agentauth.test_chat_history_write import store as store_fixture
from tests.agentauth.test_chat_history_write import sts as sts_fixture
from tests.agentauth.test_chat_user_turn import retained_input_table as retained_input_table_fixture

admitted_capability = admitted_capability_fixture
retained_input_table = retained_input_table_fixture
client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture


@pytest.fixture
async def capability(runtime, admitted_capability):
    runtime[2].update_item(Key=HEADER, UpdateExpression="REMOVE historyVersion, historyNextOrdinal")
    return admitted_capability


@pytest.fixture
async def history(runtime, capability):
    seed_history(runtime)
    runtime[2].update_item(
        Key={"PK": HEADER["PK"], "SK": "msg#message-2"},
        UpdateExpression="SET ts = :ts",
        ExpressionAttributeValues={":ts": "2026-10-03T11:31:00+01:00"},
    )
    return capability


async def summarize(client, capability, **changes):
    return await client.post(
        "/v1/chat/data/history/summary/append",
        json={
            "run_id": "run-write",
            "session_id": "session-a",
            "expected_version": 0,
            "idempotency_key": "summary-a",
            "content": "A source-grounded summary",
            "tokens": 5,
            "source_ids": ["message-1", "message-2"],
            **changes,
        },
        headers={"Authorization": f"Bearer {capability}", "X-User-Id": "other-user", "X-Tenant-Id": "other-tenant"},
    )


async def test_summary_derives_source_metadata_and_preserves_timestamp_precision(client, runtime, history):
    before = (await read(client, history)).json()["entries"]
    response = await summarize(client, history, content="Summary includes " + "AKIA" + "A" * 16)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["version"] == 1
    summary = (await read(client, history, "summary", summary_id=result["summary_id"])).json()["entries"][0]["summary"]
    assert summary["kind"] == "leaf"
    assert summary["depth"] == 0
    assert summary["sourceIds"] == ["message-1", "message-2"]
    assert summary["parentIds"] == []
    assert summary["earliestAt"] == "2026-10-03T10:30:00.123456Z"
    assert summary["latestAt"] == "2026-10-03T11:31:00+01:00"
    assert "AKIA" not in summary["content"]
    page = (await read(client, history)).json()
    assert page["entries"] == before
    assert page["version"] == 1
    expanded = (await read(client, history, "messages", ids=summary["sourceIds"])).json()
    assert expanded["status"] == "ok"
    assert [entry["ref"] for entry in expanded["entries"]] == summary["sourceIds"]
    for row in rows(runtime):
        if row["SK"] == "sum#" + result["summary_id"] or row["SK"].startswith("summary-write#"):
            assert (row["tenantId"], row["teamId"], row["ownerUserId"], row["runId"]) == ("tenant", "team", "human", "run-write")
    next_message = await append(client, history, expected_version=1)
    assert next_message.status_code == 200
    assert next_message.json()["ordinal"] == 4


async def test_condensed_summary_inherits_verified_sources_and_depth(client, runtime, history):
    first = await summarize(client, history)
    parent = first.json()["summary_id"]
    condensed = await summarize(client, history, source_ids=[], parent_ids=[parent], expected_version=1, idempotency_key="condensed-a")
    assert condensed.status_code == 200, condensed.text
    value = (await read(client, history, "summary", summary_id=condensed.json()["summary_id"])).json()["entries"][0]["summary"]
    assert (value["kind"], value["depth"], value["parentIds"], value["sourceIds"]) == ("condensed", 1, [parent], ["message-1", "message-2"])
    assert value["earliestAt"] == "2026-10-03T10:30:00.123456Z"
    assert value["latestAt"] == "2026-10-03T11:31:00+01:00"


async def test_summary_retry_and_conflicts_share_the_history_version(client, runtime, history):
    first = await summarize(client, history)
    assert first.status_code == 200
    assert (await append(client, history, expected_version=1)).status_code == 200
    before = rows(runtime)
    assert (await summarize(client, history)).json() == first.json()
    assert (await summarize(client, history, content="different")).status_code == 409
    assert (await summarize(client, history, expected_version=2)).status_code == 409
    assert (await summarize(client, history, idempotency_key="new-key")).status_code == 409
    assert rows(runtime) == before


@pytest.mark.parametrize("same_key", [False, True])
async def test_concurrent_summaries_use_atomic_version_and_receipt_fences(client, runtime, history, monkeypatch, same_key):
    """Serialize Moto commits, not request snapshots, to emulate atomic transactions."""
    original = runtime[1].store.client.transact_write_items
    barrier, lock = Barrier(2, timeout=10), Lock()

    def commit(**kwargs):
        barrier.wait()
        with lock:
            return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", commit)
    before = len(rows(runtime))
    responses = await asyncio.gather(summarize(client, history), summarize(client, history, idempotency_key="summary-a" if same_key else "summary-b"))
    assert sorted(response.status_code for response in responses) == ([200, 200] if same_key else [200, 409])
    if same_key:
        assert responses[0].json() == responses[1].json()
    assert len(rows(runtime)) == before + 2


@pytest.mark.parametrize("failure", ["unavailable", "lost_response", "lease_replaced"])
async def test_summary_failure_is_atomic_and_lost_response_is_recoverable(client, runtime, history, monkeypatch, failure):
    original = runtime[1].store.client.transact_write_items
    before = rows(runtime)

    def changed(**kwargs):
        if failure == "lost_response":
            original(**kwargs)
            raise EndpointConnectionError(endpoint_url="https://storage.test")
        if failure == "lease_replaced":
            header = runtime[2].get_item(Key=HEADER)["Item"]
            header["chatLease"]["generation"] += 1
            runtime[2].put_item(Item=header)
            return original(**kwargs)
        raise ClientError({"Error": {"Code": "InternalServerError", "Message": "private diagnostics"}}, "TransactWriteItems")

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", changed)
    response = await summarize(client, history)
    assert response.status_code == (404 if failure == "lease_replaced" else 503)
    assert "private diagnostics" not in response.text
    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original)
    if failure == "lease_replaced":
        assert len(rows(runtime)) == len(before)
    else:
        if failure == "unavailable":
            assert rows(runtime) == before
        first = await summarize(client, history)
        assert first.status_code == 200
        assert (await summarize(client, history)).json() == first.json()
        assert len(rows(runtime)) == len(before) + 2


@pytest.mark.parametrize("target", ["msg#message-1", "sum#summary-1"])
@pytest.mark.parametrize("ownership", ["missing", "other-user", "other-tenant"])
async def test_every_summary_source_requires_canonical_ownership(client, runtime, history, target, ownership):
    key = {"PK": HEADER["PK"], "SK": target}
    row = runtime[2].get_item(Key=key)["Item"]
    if ownership == "missing":
        del row["ownerUserId"]
    elif ownership == "other-user":
        row["ownerUserId"] = "other-user"
    else:
        row["tenantId"] = row["orgId"] = "other-tenant"
    runtime[2].put_item(Item=row)
    before = rows(runtime)
    response = await summarize(client, history, **({"source_ids": [], "parent_ids": ["summary-1"]} if target.startswith("sum#") else {}))
    assert response.status_code == 404
    assert rows(runtime) == before


async def test_parent_summary_cannot_launder_missing_or_unowned_descendants(client, runtime, history):
    key = {"PK": HEADER["PK"], "SK": "msg#message-1"}
    runtime[2].delete_item(Key=key)
    assert (await summarize(client, history, source_ids=[], parent_ids=["summary-1"])).status_code == 409
    runtime[2].put_item(Item={**key, "role": "user", "content": "legacy private", "ts": "2026-10-03T00:00:00Z", "tokens": 2})
    assert (await summarize(client, history, source_ids=[], parent_ids=["summary-1"])).status_code == 404


@pytest.mark.parametrize("changes", [{"source_ids": ["unknown"]}, {"source_ids": [], "parent_ids": ["unknown"]}])
async def test_missing_sources_are_conflicts_not_empty_success(client, runtime, history, changes):
    before = rows(runtime)
    assert (await summarize(client, history, **changes)).status_code == 409
    assert rows(runtime) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"source_ids": []},
        {"source_ids": ["message-1", "message-1"]},
        {"parent_ids": ["summary-1", "summary-1"]},
        {"source_ids": ["session#other/private"]},
        {"source_ids": [f"message-{number}" for number in range(1001)]},
        {"depth": 100},
        {"kind": "leaf"},
        {"earliest_at": "2000-01-01T00:00:00Z"},
        {"ownerUserId": "other-user"},
    ],
)
async def test_source_bounds_and_server_owned_metadata_cannot_be_overridden(client, runtime, history, changes):
    before = rows(runtime)
    assert (await summarize(client, history, **changes)).status_code == 422
    assert rows(runtime) == before


async def test_shared_resources_are_read_only_and_revoked_callers_cannot_replay(client, runtime, history, db_session_factory):
    seed_history(runtime, "shared-session", owner="other-user", acl=("human",))
    assert (await summarize(client, history, session_id="shared-session")).status_code == 404
    assert (await summarize(client, history)).status_code == 200
    async with db_session_factory() as db:
        await db.execute(delete(TeamMembership).where(TeamMembership.id == "member"))
        await db.commit()
    assert (await summarize(client, history)).status_code == 404


async def test_summary_route_remains_disabled_without_rollout_flag(client, runtime, history, monkeypatch):
    before = rows(runtime)
    monkeypatch.delenv("ADP_CHAT_DATA_ENABLED")
    assert (await summarize(client, history)).status_code == 503
    assert rows(runtime) == before
