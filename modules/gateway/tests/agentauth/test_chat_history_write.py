"""Assistant writes through admitted HTTP capabilities and transactional DynamoDB emulation."""

import asyncio
import hashlib
from threading import Barrier, Lock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from src.agentauth import chat_data_routes
from tests.agentauth.test_chat_data_routes import exchange
from tests.agentauth.test_chat_history_routes import client as client_fixture
from tests.agentauth.test_chat_history_routes import read as history_read
from tests.agentauth.test_chat_history_routes import runtime as runtime_fixture
from tests.agentauth.test_chat_history_routes import seed_history
from tests.agentauth.test_chat_history_routes import store as store_fixture
from tests.agentauth.test_chat_history_routes import sts as sts_fixture
from tests.agentauth.test_chat_user_turn import accept, prepare
from tests.agentauth.test_chat_user_turn import retained_input_table as retained_input_table_fixture

retained_input_table = retained_input_table_fixture
client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
APPEND = "/v1/chat/data/history/append"
HEADER = {"PK": "session#session-a", "SK": "header"}
USER_REF = "user_" + hashlib.sha256(b"run-write").hexdigest()


@pytest.fixture
async def capability(client, runtime, retained_input_table):
    envelope = prepare(runtime, message_id="run-write")
    response = await accept(client, runtime, envelope)
    assert response.status_code == 200, response.text
    return (await exchange(client)).json()["capability"]


async def read(client, capability, operation="read", **fields):
    return await history_read(client, capability, operation, run_id="run-write", **fields)


async def append(client, capability, **changes):
    return await client.post(
        APPEND,
        json={
            "run_id": "run-write",
            "session_id": "session-a",
            "idempotency_key": "append-a",
            "expected_version": 1,
            "user_turn_id": USER_REF,
            "content": "An assistant response",
            "tokens": 4,
            **changes,
        },
        headers={"Authorization": f"Bearer {capability}", "X-User-Id": "other-user", "X-Tenant-Id": "other-tenant"},
    )


def rows(runtime):
    return sorted(runtime[2].scan()["Items"], key=lambda row: row["SK"])


async def test_append_preserves_schema_and_server_owned_fields(client, runtime, capability, monkeypatch):
    monkeypatch.setenv("SESSION_TTL_SECONDS", "600")
    runtime[2].update_item(
        Key=HEADER,
        UpdateExpression="SET #ttl = :ttl",
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={":ttl": runtime[-1] + 100},
    )
    assert (await read(client, capability)).json()["version"] == 1
    response = await append(client, capability, content="Key: " + "AKIA" + "A" * 16)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert (result["ordinal"], result["version"]) == (2, 2)
    records = rows(runtime)
    assert len(records) == 7
    for record in records:
        assert (record["orgId"], record["tenantId"], record["teamId"], record["ownerUserId"]) == ("tenant", "tenant", "team", "human")
    assert records[0]["historyVersion"] == 2
    assert records[0]["ttl"] == runtime[-1] + 600
    for child in records[1:]:
        assert child["runId"] == "run-write"
        assert child["leaseGeneration"] == 1
        assert "ttl" not in child
    page = await read(client, capability)
    assert page.json()["version"] == 2
    assert page.json()["entries"][0]["ref"] == USER_REF
    assert page.json()["entries"][1] == {"ordinal": 2, "type": "msg", "ref": result["message_id"], "tokens": 4}
    assert next(row for row in records if row.get("role") == "assistant")["userTurnId"] == USER_REF
    message = (await read(client, capability, "messages", ids=[result["message_id"]])).json()["entries"][0]["message"]
    assert message["role"] == "assistant"
    assert message["ts"] == records[0]["lastActivityAt"]
    assert "AKIA" not in message["content"]


async def test_retries_have_one_effect_even_after_later_writes(client, runtime, capability):
    first = await append(client, capability)
    assert first.status_code == 200, first.text
    second = await append(client, capability, idempotency_key="append-b", expected_version=2)
    assert second.status_code == 200, second.text
    assert (second.json()["ordinal"], second.json()["version"]) == (3, 3)
    before = rows(runtime)
    retry = await append(client, capability)
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert rows(runtime) == before
    assert (await append(client, capability, content="Changed payload")).status_code == 409
    assert (await append(client, capability, expected_version=2)).status_code == 409
    assert (await append(client, capability, idempotency_key="append-c", expected_version=0)).status_code == 409
    assert rows(runtime) == before


async def test_paging_continues_across_a_concurrent_owner_append_without_gaps_or_duplicates(client, runtime, capability):
    """Cursors bind launch, resource, filters and limit, not the history version; appends land after every issued page."""
    assert (await append(client, capability)).status_code == 200
    assert (await append(client, capability, idempotency_key="append-b", expected_version=2)).status_code == 200
    first = await read(client, capability, limit=1)
    assert first.status_code == 200, first.text
    assert (first.json()["status"], first.json()["version"]) == ("partial", 3)
    landed = await append(client, capability, idempotency_key="append-c", expected_version=3)
    assert landed.status_code == 200, landed.text
    assert runtime[2].get_item(Key=HEADER)["Item"]["historyVersion"] == 4
    seen, cursor = list(first.json()["entries"]), first.json()["next_cursor"]
    while cursor:
        page = await read(client, capability, limit=1, cursor=cursor)
        assert page.status_code == 200, page.text
        assert page.json()["version"] == 4
        seen.extend(page.json()["entries"])
        cursor = page.json()["next_cursor"]
    assert [entry["ordinal"] for entry in seen] == [1, 2, 3, 4]
    assert seen[3]["ref"] == landed.json()["message_id"]
    assert len({entry["ref"] for entry in seen}) == 4
    # Scope guarantees are unchanged: another run, page size or owner cannot reuse the cursor.
    assert (await history_read(client, capability, run_id="other-run", limit=1, cursor=first.json()["next_cursor"])).status_code == 404
    assert (await read(client, capability, limit=2, cursor=first.json()["next_cursor"])).status_code == 404
    header = runtime[2].get_item(Key=HEADER)["Item"]
    runtime[2].put_item(Item={**header, "aclUserIds": ["human"], "ownerUserId": "replacement-owner"})
    assert (await read(client, capability, limit=1, cursor=first.json()["next_cursor"])).status_code == 404


async def test_ordinals_start_after_existing_history_and_are_not_reused(client, runtime, capability):
    seed_history(runtime)
    runtime[2].update_item(Key=HEADER, UpdateExpression="REMOVE historyNextOrdinal")
    first = await append(client, capability)
    assert first.status_code == 200, first.text
    assert first.json()["ordinal"] == 4
    runtime[2].delete_item(Key={"PK": HEADER["PK"], "SK": "item#00000004"})
    second = await append(client, capability, idempotency_key="append-b", expected_version=2)
    assert second.status_code == 200, second.text
    assert second.json()["ordinal"] == 5
    assert runtime[2].get_item(Key=HEADER)["Item"]["historyNextOrdinal"] == 6


@pytest.mark.parametrize("same_key", [False, True])
async def test_concurrent_appenders_cannot_overwrite_or_duplicate_ordinals(client, runtime, capability, monkeypatch, same_key):
    """Race requests before commit; serialize Moto's non-thread-safe snapshot/rollback."""
    barrier, lock = Barrier(2, timeout=10), Lock()
    original = runtime[1].store.client.transact_write_items

    def commit(**kwargs):
        barrier.wait()
        with lock:
            return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", commit)
    responses = await asyncio.gather(append(client, capability), append(client, capability, idempotency_key="append-a" if same_key else "append-b"))
    assert sorted(response.status_code for response in responses) == ([200, 200] if same_key else [200, 409]), [
        response.text for response in responses
    ]
    if same_key:
        assert responses[0].json() == responses[1].json()
    assert len(rows(runtime)) == 7
    assert (await read(client, capability)).json()["version"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "user"),
        ("role", "system"),
        ("ownerUserId", "other-user"),
        ("ttl", 9999999999),
        ("ts", "2000-01-01T00:00:00Z"),
        ("parts", [{"type": "file", "url": "https://object.test/private"}]),
        ("expected_version", True),
        ("expected_version", -1),
        ("tokens", -1),
        ("content", ""),
        ("content", "🙂" * 32769),
    ],
)
async def test_untrusted_metadata_roles_and_oversize_writes_are_rejected(client, runtime, capability, field, value):
    assert (await append(client, capability, **{field: value})).status_code == 422
    assert len(rows(runtime)) == 4


@pytest.mark.parametrize("tenant,acl", [("tenant", ()), ("tenant", ("human",)), ("other-tenant", ("human",))])
async def test_other_sessions_cannot_be_written_even_when_shared(client, runtime, capability, tenant, acl):
    seed_history(runtime, "other-session", owner="other-user", tenant=tenant, acl=acl)
    before = rows(runtime)
    assert (await append(client, capability, session_id="other-session")).status_code == 404
    assert rows(runtime) == before


async def test_missing_forged_wrong_run_expired_and_disabled_authority(client, runtime, capability, monkeypatch):
    assert (await append(client, "forged")).status_code == 401
    assert (await append(client, capability, run_id="other-run")).status_code == 404
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 300)
    assert (await append(client, capability)).status_code == 401
    monkeypatch.delenv("ADP_CHAT_DATA_ENABLED")
    assert (await append(client, capability)).status_code == 503
    assert len(rows(runtime)) == 4


@pytest.mark.parametrize("failure", ["before_commit", "lost_response"])
async def test_outage_and_lost_response_are_recoverable_without_duplicate_writes(client, runtime, capability, monkeypatch, failure):
    original = runtime[1].store.client.transact_write_items

    def failed(**kwargs):
        if failure == "lost_response":
            original(**kwargs)
            raise EndpointConnectionError(endpoint_url="https://storage.test")
        raise ClientError({"Error": {"Code": "InternalServerError", "Message": "private diagnostics"}}, "TransactWriteItems")

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", failed)
    response = await append(client, capability)
    assert response.status_code == 503
    assert "private diagnostics" not in response.text
    assert len(rows(runtime)) == (7 if failure == "lost_response" else 4)
    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original)
    first = await append(client, capability)
    assert first.status_code == 200, first.text
    retry = await append(client, capability)
    assert retry.json() == first.json()
    assert len(rows(runtime)) == 7


@pytest.mark.parametrize("race", ["owner", "lease", "abort", "epoch", "root", "grant"])
async def test_transaction_fences_revocation_between_authorization_and_commit(client, runtime, capability, monkeypatch, race):
    authority = runtime[1]
    original = authority.store.client.transact_write_items

    def changed(**kwargs):
        if race in {"owner", "lease"}:
            header = runtime[2].get_item(Key=HEADER)["Item"]
            if race == "owner":
                header["ownerUserId"] = "other-user"
            else:
                header["chatLease"]["generation"] += 1
            runtime[2].put_item(Item=header)
        elif race == "abort":
            authority.store.authority.record_abort_intent(
                invocation_id="run-write", tenant_id="tenant", attempt=1, command_id="abort-a", body_digest="a" * 64
            )
        else:
            key = {"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-write"}}
            name, value = "current_credential_epoch", {"N": "2"}
            if race == "root":
                grant = authority.store.authority.load_grant(principal="run-write#1", tenant_id="tenant")
                key["sk"] = {"S": f"AUTHORITY#{grant.authority.reference_id}"}
                name, value = "status", {"S": "revoked"}
            if race == "grant":
                key["sk"] = {"S": "GRANT#run-write#1"}
                name, value = "revoked", {"BOOL": True}
            authority.store.client.update_item(
                TableName=authority.store.table,
                Key=key,
                UpdateExpression="SET #field = :value",
                ExpressionAttributeNames={"#field": name},
                ExpressionAttributeValues={":value": value},
            )
        return original(**kwargs)

    monkeypatch.setattr(authority.store.client, "transact_write_items", changed)
    assert (await append(client, capability)).status_code == 404
    assert len(rows(runtime)) == 4
    assert rows(runtime)[0]["historyVersion"] == 1


async def test_receipt_replay_still_requires_current_authority(client, runtime, capability):
    assert (await append(client, capability)).status_code == 200
    runtime[2].update_item(
        Key=HEADER,
        UpdateExpression="SET #status = :closed",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":closed": "closed"},
    )
    assert (await append(client, capability)).status_code == 404
    assert len(rows(runtime)) == 7


async def test_missing_legacy_child_ownership_is_not_adopted(client, runtime, capability):
    runtime[2].put_item(Item={"PK": HEADER["PK"], "SK": "item#00000001", "ordinal": 1, "type": "msg", "ref": "legacy"})
    runtime[2].update_item(Key=HEADER, UpdateExpression="REMOVE historyNextOrdinal")
    before = rows(runtime)
    assert (await append(client, capability)).status_code == 404
    assert rows(runtime) == before


async def test_active_refresh_renews_retention_without_shortening_it(client, runtime, capability, monkeypatch):
    monkeypatch.setenv("SESSION_TTL_SECONDS", "600")
    runtime[2].update_item(
        Key=HEADER,
        UpdateExpression="SET #ttl = :ttl",
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={":ttl": runtime[-1] + 100},
    )
    assert (await exchange(client)).status_code == 200
    assert runtime[2].get_item(Key=HEADER)["Item"]["ttl"] == runtime[-1] + 600
    monkeypatch.setenv("SESSION_TTL_SECONDS", "100")
    assert (await exchange(client)).status_code == 200
    assert runtime[2].get_item(Key=HEADER)["Item"]["ttl"] == runtime[-1] + 600
    assert (await append(client, capability)).status_code == 200
    assert runtime[2].get_item(Key=HEADER)["Item"]["ttl"] == runtime[-1] + 600


@pytest.mark.parametrize("retention", ["0", "-1", "invalid"])
async def test_invalid_retention_fails_without_writing(client, runtime, capability, monkeypatch, retention):
    monkeypatch.setenv("SESSION_TTL_SECONDS", retention)
    before = rows(runtime)
    assert (await append(client, capability)).status_code == 503
    assert (await exchange(client)).status_code == 503
    assert rows(runtime) == before


@pytest.mark.parametrize("target", ["message", "receipt", "metadata"])
@pytest.mark.parametrize("replay", [False, True])
async def test_missing_accepted_source_blocks_new_writes_and_receipt_replays(client, runtime, capability, target, replay):
    if replay:
        assert (await append(client, capability)).status_code == 200
    if target == "metadata":
        runtime[1].store.client.update_item(
            TableName=runtime[1].store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-write"}},
            UpdateExpression="REMOVE chat_user_turn",
        )
    else:
        sort_key = f"msg#{USER_REF}" if target == "message" else "turn#run-write"
        runtime[2].delete_item(Key={"PK": HEADER["PK"], "SK": sort_key})
    before = rows(runtime)
    assert (await append(client, capability)).status_code == 503
    assert (await read(client, capability, "turn")).status_code == 503
    assert rows(runtime) == before


@pytest.mark.parametrize("target", ["message", "receipt", "metadata"])
async def test_accepted_input_is_fenced_against_concurrent_mutation(client, runtime, capability, monkeypatch, target):
    original = runtime[1].store.client.transact_write_items

    def changed(**kwargs):
        if target == "metadata":
            runtime[1].store.client.update_item(
                TableName=runtime[1].store.table,
                Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-write"}},
                UpdateExpression="REMOVE chat_user_turn",
            )
        else:
            sort_key = f"msg#{USER_REF}" if target == "message" else "turn#run-write"
            runtime[2].delete_item(Key={"PK": HEADER["PK"], "SK": sort_key})
        return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", changed)
    assert (await append(client, capability)).status_code == 503
    assert not any(row.get("role") == "assistant" or row["SK"].startswith("write#") for row in rows(runtime))
    assert runtime[2].get_item(Key=HEADER)["Item"]["historyVersion"] == 1


async def test_assistant_cannot_choose_another_user_turn(client, runtime, capability):
    before = rows(runtime)
    assert (await append(client, capability, user_turn_id="guessed-turn")).status_code == 404
    assert rows(runtime) == before


async def test_legacy_launch_without_trusted_user_input_cannot_append(client, runtime):
    from tests.agentauth.test_chat_data_routes import admit

    assert (await admit(client, runtime)).status_code == 200
    capability = (await exchange(client)).json()["capability"]
    assert (await append(client, capability, run_id="run-a", expected_version=0)).status_code == 404
    assert len(rows(runtime)) == 1
