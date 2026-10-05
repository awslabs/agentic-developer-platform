"""Compaction through mounted routes with DynamoDB transactions and SQL membership."""

import asyncio
from threading import Barrier, Lock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import delete

from src.agentauth import chat_data_routes
from src.agentauth.chat_history_compaction import MAX_COMPACTION_ITEMS
from src.shared.models.organization import TeamMembership
from tests.agentauth.test_chat_history_routes import seed_history
from tests.agentauth.test_chat_history_summary import admitted_capability as admitted_capability_fixture
from tests.agentauth.test_chat_history_summary import capability as capability_fixture
from tests.agentauth.test_chat_history_summary import client as client_fixture
from tests.agentauth.test_chat_history_summary import history as history_fixture
from tests.agentauth.test_chat_history_summary import runtime as runtime_fixture
from tests.agentauth.test_chat_history_summary import store as store_fixture
from tests.agentauth.test_chat_history_summary import sts as sts_fixture
from tests.agentauth.test_chat_history_write import HEADER, append, read, rows
from tests.agentauth.test_chat_user_turn import retained_input_table as retained_input_table_fixture

capability = capability_fixture
admitted_capability = admitted_capability_fixture
retained_input_table = retained_input_table_fixture
client = client_fixture
history = history_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture


async def compact(client, capability, **changes):
    return await client.post(
        "/v1/chat/data/history/compact",
        json={
            "run_id": "run-write",
            "session_id": "session-a",
            "expected_version": 0,
            "idempotency_key": "compact-a",
            "content": "A compacted timeline",
            "tokens": 5,
            "source_ids": ["message-1", "message-2"],
            "parent_ids": ["summary-1"],
            "from_ordinal": 1,
            "to_ordinal": 3,
            **changes,
        },
        headers={"Authorization": f"Bearer {capability}", "X-User-Id": "other-user", "X-Tenant-Id": "other-tenant"},
    )


async def test_atomic_replacement_retains_sources_attachments_order_and_retention(client, runtime, history, monkeypatch):
    monkeypatch.setenv("SESSION_TTL_SECONDS", "2000")
    sources = [row for row in rows(runtime) if row["SK"].startswith(("msg#", "sum#"))]
    cursor = (await read(client, history, limit=1)).json()["next_cursor"]
    result = await compact(client, history, content="Key: " + "AKIA" + "A" * 16)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    summary_id = result.json()["summary_id"]
    assert result.json()["version"] == 1
    page = (await read(client, history)).json()
    assert page["entries"] == [{"ordinal": 1, "type": "sum", "ref": summary_id, "tokens": 5}]
    assert page["version"] == 1
    assert all(source in rows(runtime) for source in sources)
    summary = (await read(client, history, "summary", summary_id=summary_id)).json()["entries"][0]["summary"]
    assert summary["sourceIds"] == ["message-1", "message-2"]
    assert summary["parentIds"] == ["summary-1"]
    assert (summary["kind"], summary["depth"]) == ("condensed", 2)
    assert summary["earliestAt"] == "2026-10-03T10:30:00.123456Z"
    assert summary["latestAt"] == "2026-10-03T11:31:00+01:00"
    assert "AKIA" not in summary["content"]
    messages = (await read(client, history, "messages", ids=summary["sourceIds"])).json()
    assert messages["status"] == "ok"
    assert messages["entries"][0]["message"]["parts"] == [{"type": "file", "artifactId": "attachment-1"}]
    assert (await read(client, history, limit=1, cursor=cursor)).status_code == 404
    assert runtime[2].get_item(Key=HEADER)["Item"]["ttl"] == runtime[-1] + 2000
    assert runtime[2].get_item(Key=HEADER)["Item"]["timelineEpoch"] == 1
    following = await append(client, history, expected_version=1)
    assert following.status_code == 200
    assert runtime[2].get_item(Key=HEADER)["Item"]["timelineEpoch"] == 1
    assert following.json()["ordinal"] == 4


async def test_retry_after_later_writes_has_one_effect_and_payload_conflicts_fail(client, runtime, history):
    first = await compact(client, history)
    assert first.status_code == 200
    assert (await append(client, history, expected_version=1)).status_code == 200
    before = rows(runtime)
    assert (await compact(client, history)).json() == first.json()
    for changes in ({"content": "changed"}, {"to_ordinal": 4}, {"expected_version": 2}, {"idempotency_key": "different"}):
        assert (await compact(client, history, **changes)).status_code == 409
    assert rows(runtime) == before


@pytest.mark.parametrize("competitor", ["same-key", "different-key", "append"])
async def test_concurrent_writes_are_version_fenced(client, runtime, history, monkeypatch, competitor):
    original = runtime[1].store.client.transact_write_items
    barrier, lock = Barrier(2, timeout=10), Lock()

    def commit(**kwargs):
        barrier.wait()
        with lock:
            return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", commit)
    other = (
        append(client, history, expected_version=0)
        if competitor == "append"
        else compact(client, history, idempotency_key="compact-a" if competitor == "same-key" else "compact-b")
    )
    responses = await asyncio.gather(compact(client, history), other)
    assert sorted(response.status_code for response in responses) == ([200, 200] if competitor == "same-key" else [200, 409])
    if competitor == "same-key":
        assert responses[0].json() == responses[1].json()
    page = (await read(client, history)).json()
    assert page["version"] == 1
    assert len(page["entries"]) == (1 if responses[0].status_code == 200 or competitor != "append" else 4)


@pytest.mark.parametrize("failure", ["unavailable", "lost-response"])
async def test_failure_recovery_is_atomic_and_idempotent(client, runtime, history, monkeypatch, failure):
    original = runtime[1].store.client.transact_write_items
    before = rows(runtime)

    def commit(**kwargs):
        if failure == "lost-response":
            original(**kwargs)
            raise EndpointConnectionError(endpoint_url="https://storage.test")
        raise ClientError({"Error": {"Code": "InternalServerError", "Message": "private diagnostics"}}, "TransactWriteItems")

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", commit)
    response = await compact(client, history)
    assert response.status_code == 503
    assert "private diagnostics" not in response.text
    after = rows(runtime)
    if failure == "unavailable":
        assert after == before
    else:
        assert len([row for row in after if row["SK"].startswith("item#")]) == 1
    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original)
    first = await compact(client, history)
    assert first.status_code == 200
    assert (await compact(client, history)).json() == first.json()
    if failure == "lost-response":
        assert rows(runtime) == after


@pytest.mark.parametrize("race", ["lease", "version", "item-owner", "item-alias", "item-ref", "item-deleted"])
async def test_changed_lease_version_or_range_rolls_back_every_compaction_write(client, runtime, history, monkeypatch, race):
    original = runtime[1].store.client.transact_write_items
    changed = []

    def commit(**kwargs):
        key = HEADER if race in {"lease", "version"} else {"PK": HEADER["PK"], "SK": "item#00000003"}
        row = runtime[2].get_item(Key=key)["Item"]
        if race == "lease":
            row["chatLease"]["generation"] += 1
        elif race == "version":
            row["historyVersion"] = 1
        elif race == "item-owner":
            row["ownerUserId"] = "other-user"
        elif race == "item-alias":
            row["user_id"] = "other-user"
        elif race == "item-ref":
            row["ref"] = "another-message"
        if race == "item-deleted":
            runtime[2].delete_item(Key=key)
        else:
            runtime[2].put_item(Item=row)
        changed.extend(rows(runtime))
        return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", commit)
    assert (await compact(client, history)).status_code == (404 if race == "lease" else 409)
    assert rows(runtime) == changed


@pytest.mark.parametrize("target", ["item#00000003", "msg#message-1", "sum#summary-1"])
@pytest.mark.parametrize("ownership", ["missing", "other-user", "other-tenant"])
async def test_each_range_row_and_source_requires_ownership(client, runtime, history, target, ownership):
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
    assert (await compact(client, history)).status_code == 404
    assert rows(runtime) == before


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"from_ordinal": 3, "to_ordinal": 1}, 422),
        ({"from_ordinal": True}, 422),
        ({"to_ordinal": 100_000_000}, 422),
        ({"from_ordinal": 0}, 409),
        ({"to_ordinal": 4}, 409),
        ({"parent_ids": []}, 409),
        ({"source_ids": ["message-2", "message-1"]}, 409),
        ({"source_ids": ["missing"]}, 409),
        ({"ownerUserId": "other-user"}, 422),
        ({"summary_id": "chosen"}, 422),
    ],
)
async def test_invalid_ranges_and_mismatched_sources_do_not_change_data(client, runtime, history, changes, status):
    before = rows(runtime)
    assert (await compact(client, history, **changes)).status_code == status
    assert rows(runtime) == before


@pytest.mark.parametrize("count", [MAX_COMPACTION_ITEMS, MAX_COMPACTION_ITEMS + 1])
async def test_atomic_limit_is_enforced_across_query_pages(client, runtime, history, monkeypatch, count):
    table = runtime[2]
    owner = {"tenantId": "tenant", "orgId": "tenant", "teamId": "team", "ownerUserId": "human", "PK": HEADER["PK"]}
    with table.batch_writer() as batch:
        for ordinal in range(1, count + 1):
            batch.put_item(Item={**owner, "SK": f"item#{ordinal:08d}", "ordinal": ordinal, "type": "msg", "ref": f"dense-{ordinal}"})
            batch.put_item(
                Item={**owner, "SK": f"msg#dense-{ordinal}", "role": "user", "content": "source", "ts": "2026-10-03T10:30:00Z", "tokens": 1}
            )
    original = table.query

    def paged(**kwargs):
        return original(**{**kwargs, "Limit": min(17, kwargs.get("Limit", 17))})

    monkeypatch.setattr(table, "query", paged)
    before = rows(runtime)
    response = await compact(client, history, to_ordinal=count, source_ids=[f"dense-{ordinal}" for ordinal in range(1, count + 1)], parent_ids=[])
    assert response.status_code == (200 if count == MAX_COMPACTION_ITEMS else 409), response.text
    if count == MAX_COMPACTION_ITEMS:
        assert len((await read(client, history)).json()["entries"]) == 1
        assert runtime[2].get_item(Key=HEADER)["Item"]["historyNextOrdinal"] == count + 1
    else:
        assert rows(runtime) == before


async def test_gaps_left_by_prior_compaction_can_be_condensed(client, runtime, history):
    first = await compact(client, history, to_ordinal=2, source_ids=["message-1"])
    assert first.status_code == 200
    second = await compact(
        client, history, expected_version=1, idempotency_key="compact-b", source_ids=["message-2"], parent_ids=[first.json()["summary_id"]]
    )
    assert second.status_code == 200, second.text
    assert len((await read(client, history)).json()["entries"]) == 1


@pytest.mark.parametrize(
    "timeline,parents,expected",
    [
        (
            [("sum", "parent-1"), ("msg", "message-1")],
            {"parent-1": ["message-9", "message-2"]},
            ["message-9", "message-2", "message-1"],
        ),
        (
            [("msg", "message-1"), ("sum", "parent-1"), ("msg", "message-4"), ("sum", "parent-2"), ("msg", "message-7")],
            {"parent-1": ["message-2", "message-3"], "parent-2": ["message-5", "message-6"]},
            ["message-1", "message-2", "message-3", "message-4", "message-5", "message-6", "message-7"],
        ),
        (
            [("sum", "parent-1"), ("msg", "message-2"), ("sum", "parent-2"), ("msg", "message-4")],
            {"parent-1": ["message-1", "message-2"], "parent-2": ["message-2", "message-3", "message-1"]},
            ["message-1", "message-2", "message-3", "message-4"],
        ),
    ],
    ids=["earlier-parent", "interleaved", "overlapping-sources"],
)
async def test_compaction_expands_sources_in_timeline_order(client, runtime, capability, timeline, parents, expected):
    owner = {"tenantId": "tenant", "orgId": "tenant", "teamId": "team", "ownerUserId": "human", "PK": HEADER["PK"]}
    stamp = "2026-10-03T10:30:00Z"
    with runtime[2].batch_writer() as batch:
        for reference in expected:
            batch.put_item(Item={**owner, "SK": f"msg#{reference}", "role": "user", "content": reference, "ts": stamp, "tokens": 1})
        for reference, sources in parents.items():
            batch.put_item(
                Item={
                    **owner,
                    "SK": f"sum#{reference}",
                    "kind": "leaf",
                    "depth": 0,
                    "content": "earlier messages",
                    "sourceIds": sources,
                    "earliestAt": stamp,
                    "latestAt": stamp,
                    "tokens": 1,
                }
            )
        for ordinal, (kind, reference) in enumerate(timeline, start=1):
            batch.put_item(Item={**owner, "SK": f"item#{ordinal:08d}", "ordinal": ordinal, "type": kind, "ref": reference})
    changes = {
        "to_ordinal": len(timeline),
        "source_ids": [reference for kind, reference in timeline if kind == "msg"],
        "parent_ids": [reference for kind, reference in timeline if kind == "sum"],
    }
    response = await compact(client, capability, **changes)
    assert response.status_code == 200, response.text
    summary_id = response.json()["summary_id"]
    summary = (await read(client, capability, "summary", summary_id=summary_id)).json()["entries"][0]["summary"]
    assert summary["sourceIds"] == expected
    assert summary["parentIds"] == changes["parent_ids"]
    messages = (await read(client, capability, "messages", ids=summary["sourceIds"])).json()
    assert messages["status"] == "ok"
    assert [entry["ref"] for entry in messages["entries"]] == expected
    assert [entry["message"]["content"] for entry in messages["entries"]] == expected
    before = rows(runtime)
    assert (await compact(client, capability, **changes)).json() == response.json()
    assert rows(runtime) == before
    condensed = await compact(
        client, capability, expected_version=1, idempotency_key="compact-b", to_ordinal=1, source_ids=[], parent_ids=[summary_id]
    )
    assert condensed.status_code == 200, condensed.text
    expanded = (await read(client, capability, "summary", summary_id=condensed.json()["summary_id"])).json()["entries"][0]["summary"]
    assert expanded["sourceIds"] == expected
    assert expanded["depth"] == 2


@pytest.mark.parametrize("tenant,acl", [("tenant", ()), ("tenant", ("human",)), ("other-tenant", ("human",))])
async def test_other_sessions_cannot_be_compacted_even_when_shared(client, runtime, history, tenant, acl):
    seed_history(runtime, "other-session", owner="other-user", tenant=tenant, acl=acl)
    before = rows(runtime)
    assert (await compact(client, history, session_id="other-session")).status_code == 404
    assert rows(runtime) == before


async def test_revoked_members_cannot_replay_success(client, runtime, history, db_session_factory):
    assert (await compact(client, history)).status_code == 200
    before = rows(runtime)
    async with db_session_factory() as db:
        await db.execute(delete(TeamMembership).where(TeamMembership.id == "member"))
        await db.commit()
    assert (await compact(client, history)).status_code == 404
    assert rows(runtime) == before


async def test_expired_wrong_run_forged_and_disabled_capabilities_fail_closed(client, runtime, history, monkeypatch):
    before = rows(runtime)
    assert (await compact(client, "forged")).status_code == 401
    assert (await compact(client, history, run_id="other-run")).status_code == 404
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 300)
    expired = await compact(client, history)
    assert (expired.status_code, expired.json()["detail"]["error"]) == (401, "capability_expired")
    monkeypatch.delenv("ADP_CHAT_DATA_ENABLED")
    assert (await compact(client, history)).status_code == 503
    assert rows(runtime) == before
