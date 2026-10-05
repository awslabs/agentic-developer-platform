"""Delegated memory routes against Moto storage and current SQL membership."""

import asyncio
from threading import Barrier, Lock

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from sqlalchemy import delete

from src.agentauth import chat_data_routes
from src.agentauth.chat_memory import KIND_TTL
from src.orchestration.chat_data_migration import _owner_fields, inventory
from src.shared.models.organization import TeamMembership
from tests.agentauth.test_chat_history_routes import capability as capability_fixture
from tests.agentauth.test_chat_history_routes import client as client_fixture
from tests.agentauth.test_chat_history_routes import runtime as runtime_fixture
from tests.agentauth.test_chat_history_routes import store as store_fixture
from tests.agentauth.test_chat_history_routes import sts as sts_fixture

capability = capability_fixture
client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture


@pytest.fixture
def memory(runtime, monkeypatch):
    table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="memory",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
    )
    monkeypatch.setenv("MEMORY_TABLE", table.name)
    return table


async def call(client, capability, operation="write", **changes):
    body = {"run_id": "run-a"}
    if operation == "write":
        body.update({"idempotency_key": "memory-a", "expected_version": 0, "content": "Remember this fact"})
    return await client.post(
        f"/v1/chat/data/memory/{operation}",
        json={**body, **changes},
        headers={"Authorization": f"Bearer {capability}", "X-User-Id": "victim", "X-Tenant-Id": "other-tenant"},
    )


def key(memory_id):
    return {"PK": f"memory#{memory_id}", "SK": "record"}


async def create(client, capability, **changes):
    response = await call(client, capability, **changes)
    assert response.status_code == 200, response.text
    return response.json()


async def test_owned_create_read_update_and_retries(client, capability, runtime, memory):
    first = await create(client, capability, content="Key: " + "AKIA" + "A" * 16)
    assert first["version"] == 1
    saved = memory.get_item(Key=key(first["memory_id"]))["Item"]
    assert saved["scope"] == {"tenant": "tenant", "user": "human"}
    assert saved["source"] == {"sessionId": "session-a", "runId": "run-a"}
    assert saved["ttl"] == runtime[-1] + KIND_TTL["fact"]
    assert "AKIA" not in saved["content"]
    read = await call(client, capability, "read", memory_id=first["memory_id"])
    assert read.status_code == 200
    assert read.headers["cache-control"] == "no-store"
    assert read.json()["entries"][0]["id"] == first["memory_id"]
    assert await create(client, capability, content="Key: " + "AKIA" + "A" * 16) == first
    updated = await create(client, capability, memory_id=first["memory_id"], expected_version=1, idempotency_key="update", content="New content")
    assert updated == {"memory_id": first["memory_id"], "version": 2}
    assert await create(client, capability, content="Key: " + "AKIA" + "A" * 16) == first
    assert (await call(client, capability, content="different")).status_code == 409
    assert (await call(client, capability, memory_id=first["memory_id"], expected_version=1, idempotency_key="stale")).status_code == 409
    assert memory.get_item(Key=key(first["memory_id"]))["Item"]["content"] == "New content"
    assert len(memory.scan()["Items"]) == 4


async def test_labels_filter_only_owned_memory_and_bind_cursors(client, capability, memory):
    first = await create(client, capability, labels={"component": "gateway", "persona": "reviewer"})
    await create(client, capability, idempotency_key="second", labels={"component": "worker", "persona": "developer"})
    found = (await call(client, capability, "search", labels={"persona": "reviewer"})).json()
    assert [entry["id"] for entry in found["entries"]] == [first["memory_id"]]
    assert found["entries"][0]["scope"] == {"tenant": "tenant", "user": "human"}
    assert found["entries"][0]["labels"] == {"component": "gateway", "persona": "reviewer"}
    filtered = (await call(client, capability, "search", labels={"component": "gateway"}, limit=1)).json()
    assert filtered["next_cursor"]
    changed = await call(client, capability, "search", labels={"component": "worker"}, limit=1, cursor=filtered["next_cursor"])
    assert changed.status_code == 404
    await create(client, capability, memory_id=first["memory_id"], expected_version=1, idempotency_key="update-labels", labels={"persona": "planner"})
    assert (await call(client, capability, "search", labels={"persona": "reviewer"})).json()["status"] == "empty"
    assert (await call(client, capability, "search", labels={"persona": "planner"})).json()["entries"][0]["version"] == 2


async def test_existing_records_without_labels_remain_readable(client, capability, memory):
    record = await create(client, capability)
    row = memory.get_item(Key=key(record["memory_id"]))["Item"]
    row.pop("labels")
    memory.put_item(Item=row)
    assert (await call(client, capability, "read", memory_id=record["memory_id"])).json()["entries"][0]["labels"] == {}
    assert (await call(client, capability, "search", labels={"persona": "reviewer"})).json()["status"] == "empty"
    assert (await call(client, capability, "search")).json()["entries"][0]["id"] == record["memory_id"]


@pytest.mark.parametrize("labels", [{"persona": "reviewer"}, {"persona": "developer"}, {"component": "gateway"}])
async def test_labelled_recall_includes_owned_user_wide_preferences(client, capability, memory, labels):
    preference = await create(client, capability, kind="preference", content="Use concise answers")
    await create(client, capability, idempotency_key="fact", content="Use concise facts", kind="fact")
    await create(client, capability, idempotency_key="labelled-preference", kind="preference", labels={"persona": "unrelated"})
    result = (await call(client, capability, "search", labels=labels)).json()
    assert [entry["id"] for entry in result["entries"]] == [preference["memory_id"]]
    assert result["entries"][0]["labels"] == {}
    assert (await call(client, capability, "search", labels=labels, kinds=["fact"])).json()["status"] == "empty"
    assert (await call(client, capability, "search", labels=labels, query="no match")).json()["status"] == "empty"


@pytest.mark.parametrize("labels", [{"user": "victim"}, {"tenant": "other"}, {"component": ""}, {"persona": "x" * 129}])
async def test_invalid_labels_cannot_select_other_owners(client, capability, memory, labels):
    assert (await call(client, capability, "search", labels=labels)).status_code == 422
    assert (await call(client, capability, labels=labels)).status_code == 422
    assert memory.scan()["Items"] == []


async def test_search_pagination_filters_missing_sources_and_empty_are_distinct(client, capability, memory):
    assert (await call(client, capability, "search")).json()["status"] == "empty"
    records = [await create(client, capability, idempotency_key=f"write-{number}", content=f"fact {number}") for number in range(3)]
    first = (await call(client, capability, "search", limit=1)).json()
    assert first["status"] == "partial"
    assert first["coverage"]["complete"] is False
    found = list(first["entries"])
    cursor = first["next_cursor"]
    while cursor:
        page = (await call(client, capability, "search", limit=1, cursor=cursor)).json()
        found.extend(page["entries"])
        cursor = page["next_cursor"]
    assert {entry["id"] for entry in found} == {record["memory_id"] for record in records}
    assert len(found) == 3
    assert (await call(client, capability, "search", limit=1, cursor=first["next_cursor"], query="changed")).status_code == 404
    assert (await call(client, capability, "search", limit=2, cursor=first["next_cursor"])).status_code == 404
    filtered = (await call(client, capability, "search", limit=1, query="no match")).json()
    assert filtered["status"] == "partial" and not filtered["entries"]
    assert (await call(client, capability, "search", query="no match")).json()["status"] == "empty"
    assert (await call(client, capability, "search", kinds=["preference"])).json()["status"] == "empty"
    memory.delete_item(Key=key(records[0]["memory_id"]))
    missing = (await call(client, capability, "read", memory_id=records[0]["memory_id"])).json()
    assert missing["status"] == "partial"
    assert missing["coverage"]["missing_source_ids"] == [records[0]["memory_id"]]
    assert (await call(client, capability, "search")).json()["status"] == "partial"


@pytest.mark.parametrize(
    "tenant,owner,acl,status",
    [("tenant", "other-user", [], 404), ("other-tenant", "other-user", ["human"], 404), ("tenant", "other-user", ["human"], 200)],
)
async def test_guessed_ids_require_current_same_tenant_acl(client, capability, memory, tenant, owner, acl, status):
    record = await create(client, capability)
    row = memory.get_item(Key=key(record["memory_id"]))["Item"]
    row.update({**_owner_fields((tenant, "team", owner)), "scope": {"tenant": tenant, "user": owner}, "aclUserIds": acl})
    memory.put_item(Item=row)
    assert (await call(client, capability, "read", memory_id=record["memory_id"])).status_code == status
    assert (await call(client, capability, memory_id=record["memory_id"], expected_version=1, idempotency_key="other-write")).status_code == 404
    if status == 200:
        row["aclUserIds"] = []
        memory.put_item(Item=row)
        assert (await call(client, capability, "read", memory_id=record["memory_id"])).status_code == 404


@pytest.mark.parametrize("target", ["record", "index"])
@pytest.mark.parametrize("ownership", ["missing", "conflicting"])
async def test_legacy_or_conflicting_ownership_is_not_inferred(client, capability, memory, target, ownership):
    record = await create(client, capability)
    row = (
        memory.get_item(Key=key(record["memory_id"]))["Item"]
        if target == "record"
        else next(row for row in memory.scan()["Items"] if row["SK"].startswith("mem#"))
    )
    if ownership == "missing":
        del row["ownerUserId"]
    else:
        row["user_id"] = "other-user"
    memory.put_item(Item=row)
    assert (await call(client, capability, "search")).status_code == 404
    assert (await call(client, capability, memory_id=record["memory_id"], expected_version=1, idempotency_key="update")).status_code == 404


@pytest.mark.parametrize("kind", list(KIND_TTL))
async def test_retention_refresh_does_not_revive_expired_memories(client, capability, memory, runtime, monkeypatch, kind):
    record = await create(client, capability, kind=kind)
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 10)
    assert (await call(client, capability, "read", memory_id=record["memory_id"])).status_code == 200
    row = memory.get_item(Key=key(record["memory_id"]))["Item"]
    pointer = next(item for item in memory.scan()["Items"] if item["SK"].startswith("mem#"))
    assert pointer["kind"] == kind
    assert pointer.get("ttl") == row.get("ttl")
    if kind == "preference":
        assert "ttl" not in row
        return
    assert row["ttl"] == runtime[-1] + KIND_TTL[kind] + (10 if kind in {"fact", "learning"} else 0)
    row["ttl"] = runtime[-1] - 1
    memory.put_item(Item=row)
    assert (await call(client, capability, "read", memory_id=record["memory_id"])).json()["status"] == "partial"
    assert memory.get_item(Key=key(record["memory_id"]))["Item"]["ttl"] == row["ttl"]


@pytest.mark.parametrize("previous_kind", list(KIND_TTL))
@pytest.mark.parametrize("kind", list(KIND_TTL))
async def test_kind_updates_keep_index_retention_consistent(client, capability, memory, runtime, monkeypatch, previous_kind, kind):
    record = await create(client, capability, kind=previous_kind)
    pointer = next(item for item in memory.scan()["Items"] if item["SK"].startswith("mem#"))
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 10)
    updated = await create(client, capability, memory_id=record["memory_id"], expected_version=1, idempotency_key="update", kind=kind)
    assert updated["version"] == 2
    row = memory.get_item(Key=key(record["memory_id"]))["Item"]
    pointers = [item for item in memory.scan()["Items"] if item["SK"].startswith("mem#")]
    assert len(pointers) == 1
    assert (pointers[0]["PK"], pointers[0]["SK"]) == (pointer["PK"], pointer["SK"])
    assert pointers[0]["kind"] == row["kind"] == kind
    if kind == "preference":
        assert "ttl" not in pointers[0] and "ttl" not in row
    else:
        assert pointers[0]["ttl"] == row["ttl"] == runtime[-1] + 10 + KIND_TTL[kind]


async def test_legacy_index_expiration_requires_evidence(client, capability, memory, runtime):
    record = await create(client, capability)
    pointer = next(item for item in memory.scan()["Items"] if item["SK"].startswith("mem#"))
    pointer.pop("kind", None)
    pointer.pop("ttl", None)
    memory.put_item(Item=pointer)
    row = memory.get_item(Key=key(record["memory_id"]))["Item"]
    memory.put_item(Item={**row, "ttl": runtime[-1]})
    response = await call(client, capability, "search")
    assert response.status_code == 200 and response.json()["status"] == "empty"
    memory.delete_item(Key=key(record["memory_id"]))
    response = await call(client, capability, "search")
    assert response.json()["status"] == "partial"
    assert response.json()["coverage"]["missing_source_ids"] == [record["memory_id"]]


@pytest.mark.parametrize("retention", [{"kind": "unknown", "ttl": 1}, {"kind": "fact", "ttl": None}, {"kind": "fact", "ttl": True}])
async def test_invalid_index_retention_cannot_hide_missing_sources(client, capability, memory, retention):
    record = await create(client, capability)
    pointer = next(item for item in memory.scan()["Items"] if item["SK"].startswith("mem#"))
    memory.put_item(Item={**pointer, **retention})
    memory.delete_item(Key=key(record["memory_id"]))
    assert (await call(client, capability, "search")).status_code == 503


async def test_expired_index_is_authorized_before_being_skipped(client, capability, memory, runtime):
    record = await create(client, capability)
    pointer = next(item for item in memory.scan()["Items"] if item["SK"].startswith("mem#"))
    memory.put_item(Item={**pointer, "ttl": runtime[-1], "user_id": "other-owner"})
    memory.delete_item(Key=key(record["memory_id"]))
    assert (await call(client, capability, "search")).status_code == 404


@pytest.mark.parametrize("operation", ["read", "write"])
async def test_index_race_rolls_back_record_retention(client, capability, memory, runtime, monkeypatch, operation):
    record = await create(client, capability)
    before = memory.get_item(Key=key(record["memory_id"]))["Item"]
    pointer = next(item for item in memory.scan()["Items"] if item["SK"].startswith("mem#"))
    original = runtime[1].store.client.transact_write_items

    def commit(**kwargs):
        memory.put_item(Item={**pointer, "user_id": "other-owner"})
        return original(**kwargs)

    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 10)
    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", commit)
    changes = {"expected_version": 1, "idempotency_key": "update"} if operation == "write" else {}
    response = await call(client, capability, operation, memory_id=record["memory_id"], **changes)
    assert response.status_code == (409 if operation == "write" else 503)
    assert memory.get_item(Key=key(record["memory_id"]))["Item"] == before
    assert memory.get_item(Key={"PK": pointer["PK"], "SK": pointer["SK"]})["Item"]["user_id"] == "other-owner"


@pytest.mark.parametrize("same_key", [False, True])
async def test_concurrent_updates_and_create_retries_have_one_effect(client, capability, memory, runtime, monkeypatch, same_key):
    existing = None if same_key else await create(client, capability)
    original = runtime[1].store.client.transact_write_items
    barrier, lock = Barrier(2, timeout=10), Lock()

    def commit(**kwargs):
        barrier.wait()
        with lock:
            return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", commit)
    changes = {"memory_id": existing["memory_id"], "expected_version": 1} if existing else {}
    responses = await asyncio.gather(
        call(client, capability, idempotency_key="race-a", **changes),
        call(client, capability, idempotency_key="race-a" if same_key else "race-b", **changes),
    )
    assert sorted(response.status_code for response in responses) == ([200, 200] if same_key else [200, 409])
    if same_key:
        assert responses[0].json() == responses[1].json()
    assert len([row for row in memory.scan()["Items"] if row["SK"] == "record"]) == 1


@pytest.mark.parametrize("lost_response", [False, True])
async def test_failed_transactions_and_lost_responses_are_recoverable(client, capability, memory, runtime, monkeypatch, lost_response):
    original = runtime[1].store.client.transact_write_items

    def commit(**kwargs):
        if lost_response:
            original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://storage.test")

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", commit)
    assert (await call(client, capability)).status_code == 503
    assert len(memory.scan()["Items"]) == (3 if lost_response else 0)
    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original)
    result = await create(client, capability)
    assert await create(client, capability) == result
    assert len(memory.scan()["Items"]) == 3


async def test_membership_revocation_blocks_reads_search_writes_and_receipt_replay(client, capability, memory, db_session_factory):
    result = await create(client, capability)
    async with db_session_factory() as db:
        await db.execute(delete(TeamMembership).where(TeamMembership.id == "member"))
        await db.commit()
    for operation, fields in (("write", {}), ("search", {}), ("read", {"memory_id": result["memory_id"]})):
        assert (await call(client, capability, operation, **fields)).status_code == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"ownerUserId": "victim"},
        {"scope": {"user": "victim"}},
        {"aclUserIds": ["victim"]},
        {"ttl": 9999999999},
        {"expected_version": True},
        {"memory_id": "scope#user#victim"},
        {"labels": {"user": "victim"}},
        {"labels": {"tenant": "other-tenant"}},
    ],
)
async def test_model_cannot_override_ownership_sharing_retention_or_keys(client, capability, memory, changes):
    assert (await call(client, capability, **changes)).status_code == 422
    assert memory.scan()["Items"] == []


async def test_authority_and_configuration_fail_closed(client, capability, memory, runtime, monkeypatch):
    assert (await call(client, "forged")).status_code == 401
    assert (await call(client, capability, run_id="other-run")).status_code == 404
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 300)
    assert (await call(client, capability)).status_code == 401
    monkeypatch.delenv("MEMORY_TABLE")
    assert (await call(client, capability)).status_code == 503
    monkeypatch.delenv("ADP_CHAT_DATA_ENABLED")
    assert (await call(client, capability)).status_code == 503
    assert memory.scan()["Items"] == []


@pytest.mark.parametrize("race", ["lease", "grant", "epoch", "resource-acl"])
async def test_revocation_during_commit_rolls_back_memory_and_receipt(client, capability, memory, runtime, monkeypatch, race):
    record = await create(client, capability)
    original = runtime[1].store.client.transact_write_items
    authority = runtime[1].store
    before = memory.scan()["Items"]

    def commit(**kwargs):
        if race == "lease":
            runtime[2].update_item(
                Key={"PK": "session#session-a", "SK": "header"},
                UpdateExpression="SET chatLease.generation = :generation",
                ExpressionAttributeValues={":generation": 2},
            )
        elif race == "resource-acl":
            row = memory.get_item(Key=key(record["memory_id"]))["Item"]
            row["aclUserIds"] = ["new-reader"]
            memory.put_item(Item=row)
        else:
            authority.client.update_item(
                TableName=authority.table,
                Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "GRANT#run-a#1" if race == "grant" else "EXEC#run-a"}},
                UpdateExpression="SET #field = :value",
                ExpressionAttributeNames={"#field": "revoked" if race == "grant" else "current_credential_epoch"},
                ExpressionAttributeValues={":value": {"BOOL": True} if race == "grant" else {"N": "2"}},
            )
        return original(**kwargs)

    monkeypatch.setattr(authority.client, "transact_write_items", commit)
    response = await call(client, capability, memory_id=record["memory_id"], expected_version=1, idempotency_key="update", content="replacement")
    assert response.status_code == (409 if race == "resource-acl" else 404)
    assert memory.get_item(Key=key(record["memory_id"]))["Item"]["version"] == 1
    assert len(memory.scan()["Items"]) == len(before)


async def test_memory_inventory_recognizes_explicit_records_without_adopting_legacy(client, capability, memory, runtime):
    record = await create(client, capability, labels={"component": "gateway", "persona": "reviewer"})
    memory.put_item(Item={"PK": "scope#user#human", "SK": "mem#legacy", "scope": {"user": "human", "tenant": "tenant"}})
    conflict = {**memory.get_item(Key=key(record["memory_id"]))["Item"], "id": "mem_" + "b" * 32}
    conflict["PK"] = f"memory#{conflict['id']}"
    conflict["user_id"] = "other-user"
    memory.put_item(Item=conflict)

    class EmptyCatalog:
        def scan(self):
            return {"Items": []}

    before = memory.scan()["Items"]
    for apply in (False, True, True):
        assert inventory(runtime[2], EmptyCatalog(), memory, apply=apply)["memory"] == {"total": 5, "owned": 3, "quarantined": 2}
        assert memory.scan()["Items"] == before
