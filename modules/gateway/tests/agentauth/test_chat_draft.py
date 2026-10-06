"""Draft routes with real DynamoDB emulation and SQL membership checks."""

import asyncio
from threading import Barrier, Lock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import delete

from src.agentauth import chat_data_routes
from src.orchestration.chat_data_migration import _owner_fields
from src.shared.models.organization import TeamMembership
from tests.agentauth.test_chat_history_routes import capability as capability_fixture
from tests.agentauth.test_chat_history_routes import client as client_fixture
from tests.agentauth.test_chat_history_routes import runtime as runtime_fixture
from tests.agentauth.test_chat_history_routes import seed_history
from tests.agentauth.test_chat_history_routes import store as store_fixture
from tests.agentauth.test_chat_history_routes import sts as sts_fixture
from tests.agentauth.test_chat_history_write import HEADER, rows

capability = capability_fixture
client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
DRAFT = {"PK": HEADER["PK"], "SK": "draft"}


async def call(client, capability, operation="write", **changes):
    body = {"run_id": "run-a", "session_id": "session-a"}
    if operation == "write":
        body.update({"idempotency_key": "draft-a", "expected_version": 0, "draft": {"intent": "Build a useful feature"}})
    return await client.post(
        f"/v1/chat/data/draft/{operation}",
        json={**body, **changes},
        headers={"Authorization": f"Bearer {capability}", "X-User-Id": "other-user", "X-Tenant-Id": "other-tenant"},
    )


async def create(client, capability, **changes):
    response = await call(client, capability, **changes)
    assert response.status_code == 200, response.text
    return response.json()


def legacy(runtime, **changes):
    row = {
        **DRAFT,
        **_owner_fields(("tenant", "team", "human")),
        "draft": {"intent": "An owned legacy draft", "updatedAt": "2026-10-03T10:30:00.123Z"},
        "ttl": runtime[-1] + 100,
        **changes,
    }
    runtime[2].put_item(Item=row)
    return row


async def test_whole_draft_replacement_clear_and_server_owned_retention(client, capability, runtime, monkeypatch):
    empty = await call(client, capability, "read")
    assert empty.status_code == 200
    assert empty.json()["status"] == "empty" and empty.json()["entries"] == [] and empty.json()["version"] == 0
    assert empty.json()["coverage"] == {"source": "session_draft", "complete": True, "missing_source_ids": []}
    monkeypatch.setenv("SESSION_TTL_SECONDS", "600")
    runtime[2].update_item(
        Key=HEADER,
        UpdateExpression="SET #ttl = :ttl",
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={":ttl": runtime[-1] + 100},
    )
    draft = {
        "intent": "A feature",
        "motivation": "A useful outcome",
        "outcomes": ["Observable result"],
        "constraints": ["No rollout yet"],
        "openQuestions": ["Which environment?"],
        "waveDisplay": {"title": "First wave", "description": "Evaluation scope"},
        "epicDisplay": {"title": "Feature", "description": "Delivery purpose"},
    }
    stored = await create(client, capability, draft=draft)
    assert stored["version"] == 1
    assert {field: value for field, value in stored["draft"].items() if field != "updatedAt"} == draft
    read = await call(client, capability, "read")
    assert read.headers["cache-control"] == "no-store"
    assert read.json()["entries"] == [{"draft": stored["draft"]}]
    assert read.json()["version"] == 1
    for row in rows(runtime):
        assert (row["tenantId"], row["teamId"], row["ownerUserId"]) == ("tenant", "team", "human")
        if row["SK"] != "header":
            assert "ttl" not in row
            assert (row["runId"], row["leaseGeneration"]) == ("run-a", 1)
    header = runtime[2].get_item(Key=HEADER)["Item"]
    assert header["ttl"] == runtime[-1] + 600
    assert "historyVersion" not in header and "historyNextOrdinal" not in header
    monkeypatch.setenv("SESSION_TTL_SECONDS", "100")
    replaced = await create(client, capability, draft={"intent": "Changed"}, expected_version=1, idempotency_key="replace")
    assert set(replaced["draft"]) == {"intent", "updatedAt"}
    cleared = await create(client, capability, draft={}, expected_version=2, idempotency_key="clear")
    assert set(cleared["draft"]) == {"updatedAt"}
    assert (await call(client, capability, "read")).json()["status"] == "ok"
    assert runtime[2].get_item(Key=HEADER)["Item"]["ttl"] == header["ttl"]


async def test_retries_preserve_original_result_without_overwriting_later_drafts(client, capability, runtime, monkeypatch):
    first = await create(client, capability)
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 10)
    second = await create(client, capability, expected_version=1, idempotency_key="second", draft={"intent": "A later draft"})
    assert second["draft"]["updatedAt"] != first["draft"]["updatedAt"]
    before = rows(runtime)
    assert await create(client, capability) == first
    for changes in ({"draft": {}}, {"expected_version": 2}, {"idempotency_key": "stale"}):
        assert (await call(client, capability, **changes)).status_code == 409
    assert rows(runtime) == before
    assert (await call(client, capability, "read")).json()["entries"] == [{"draft": second["draft"]}]


async def test_scrubbing_covers_lists_and_nested_display_fields(client, capability, runtime):
    secret = "AKIA" + "A" * 16
    draft = {"intent": secret, "outcomes": [secret], "openQuestions": [secret], "epicDisplay": {"title": secret, "description": secret}}
    result = await create(client, capability, draft=draft)
    assert secret not in str(result)
    assert secret not in str(rows(runtime))


@pytest.mark.parametrize("same_key", [False, True])
@pytest.mark.parametrize("version", [0, 1])
async def test_concurrent_creates_and_updates_are_atomic(client, capability, runtime, monkeypatch, same_key, version):
    if version:
        await create(client, capability)
    original = runtime[1].store.client.transact_write_items
    barrier, lock = Barrier(2, timeout=10), Lock()

    def commit(**kwargs):
        barrier.wait()
        with lock:
            return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", commit)
    responses = await asyncio.gather(
        call(client, capability, idempotency_key="race-a", expected_version=version),
        call(client, capability, idempotency_key="race-a" if same_key else "race-b", expected_version=version),
    )
    assert sorted(response.status_code for response in responses) == ([200, 200] if same_key else [200, 409])
    if same_key:
        assert responses[0].json() == responses[1].json()
    assert runtime[2].get_item(Key=DRAFT)["Item"]["draftVersion"] == version + 1
    assert len(rows(runtime)) == 3 + version


@pytest.mark.parametrize("lost_response", [False, True])
async def test_storage_failure_and_lost_response_recover_without_duplicate_effects(client, capability, runtime, monkeypatch, lost_response):
    original = runtime[1].store.client.transact_write_items
    before = rows(runtime)

    def commit(**kwargs):
        if lost_response:
            original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://storage.test")

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", commit)
    assert (await call(client, capability)).status_code == 503
    if not lost_response:
        assert rows(runtime) == before
    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original)
    result = await create(client, capability)
    assert await create(client, capability) == result
    assert len(rows(runtime)) == 3


@pytest.mark.parametrize("tenant,acl,status", [("tenant", [], 404), ("tenant", ["human"], 200), ("other-tenant", ["human"], 404)])
async def test_other_sessions_need_current_acl_and_cannot_be_written(client, capability, runtime, tenant, acl, status):
    header = seed_history(runtime, "other-session", owner="other-user", tenant=tenant, acl=acl)
    runtime[2].put_item(
        Item={
            **_owner_fields((tenant, "team", "other-user")),
            "PK": header["PK"],
            "SK": "draft",
            "draft": {"intent": "Private", "updatedAt": "2026-10-03T00:00:00Z"},
        }
    )
    before = rows(runtime)
    assert (await call(client, capability, "read", session_id="other-session")).status_code == status
    assert (await call(client, capability, session_id="other-session")).status_code == 404
    assert rows(runtime) == before
    if status == 200:
        header["aclUserIds"] = []
        runtime[2].put_item(Item=header)
        assert (await call(client, capability, "read", session_id="other-session")).status_code == 404


@pytest.mark.parametrize("ownership", ["missing", "other-user", "other-tenant", "conflicting-alias"])
async def test_legacy_ownership_is_never_inferred_from_session_key(client, capability, runtime, ownership):
    row = legacy(runtime)
    if ownership == "missing":
        del row["ownerUserId"]
    elif ownership == "other-user":
        row["ownerUserId"] = "other-user"
    elif ownership == "other-tenant":
        row["orgId"] = row["tenantId"] = "other-tenant"
    else:
        row["user_id"] = "other-user"
    runtime[2].put_item(Item=row)
    before = rows(runtime)
    assert (await call(client, capability, "read")).status_code == 404
    assert (await call(client, capability)).status_code == 404
    assert rows(runtime) == before


async def test_explicitly_owned_legacy_draft_can_be_read_and_versioned(client, capability, runtime):
    original = legacy(runtime)
    read = (await call(client, capability, "read")).json()
    assert read["entries"] == [{"draft": original["draft"]}] and read["version"] == 0
    result = await create(client, capability)
    assert result["version"] == 1
    assert "ttl" not in runtime[2].get_item(Key=DRAFT)["Item"]


@pytest.mark.parametrize("race", ["lease", "owner", "grant", "epoch", "ttl", "draft-owner", "draft-version"])
async def test_authority_or_draft_changes_during_commit_roll_back_all_writes(client, capability, runtime, monkeypatch, race):
    await create(client, capability)
    original = runtime[1].store.client.transact_write_items
    observed = []

    def commit(**kwargs):
        if race in {"lease", "owner", "ttl", "draft-owner", "draft-version"}:
            row = runtime[2].get_item(Key=DRAFT if race.startswith("draft-") else HEADER)["Item"]
            if race == "lease":
                row["chatLease"]["generation"] += 1
            elif race.endswith("owner"):
                row["ownerUserId"] = "other-user"
            elif race == "ttl":
                row["ttl"] += 100
            else:
                row["draftVersion"] += 1
            runtime[2].put_item(Item=row)
        else:
            authority = runtime[1].store
            authority.client.update_item(
                TableName=authority.table,
                Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "GRANT#run-a#1" if race == "grant" else "EXEC#run-a"}},
                UpdateExpression="SET #field = :value",
                ExpressionAttributeNames={"#field": "revoked" if race == "grant" else "current_credential_epoch"},
                ExpressionAttributeValues={":value": {"BOOL": True} if race == "grant" else {"N": "2"}},
            )
        observed.extend(rows(runtime))
        return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", commit)
    response = await call(client, capability, expected_version=1, idempotency_key="update", draft={})
    assert response.status_code == (409 if race in {"ttl", "draft-owner", "draft-version"} else 404)
    assert rows(runtime) == observed


@pytest.mark.parametrize(
    "changes",
    [
        {"ownerUserId": "other-user"},
        {"ttl": 9999999999},
        {"expected_version": True},
        {"draft": {"updatedAt": "2026-01-01T00:00:00Z"}},
        {"draft": {"intent": "x" * 2001}},
        {"draft": {"outcomes": ["result"] * 21}},
        {"draft": {"waveDisplay": {"title": " ", "description": "description"}}},
        {"draft": {"epicDisplay": {"title": "title", "description": "description", "owner": "other-user"}}},
        {"draft": {"outcomes": ["🙂" * 2000] * 20}},
        {"draft": {"unknown": "not part of the interface"}},
    ],
)
async def test_untrusted_fields_types_and_payload_bounds_are_rejected(client, capability, runtime, changes):
    before = rows(runtime)
    assert (await call(client, capability, **changes)).status_code == 422
    assert rows(runtime) == before


@pytest.mark.parametrize("changes", [{"draftVersion": True}, {"draftVersion": -1}, {"draftVersion": "1"}, {"draft": {"intent": "missing timestamp"}}])
async def test_malformed_storage_is_unavailable_not_empty(client, capability, runtime, changes):
    legacy(runtime, **changes)
    before = rows(runtime)
    assert (await call(client, capability, "read")).status_code == 503
    assert (await call(client, capability)).status_code == 503
    assert rows(runtime) == before


async def test_storage_read_outage_is_not_an_empty_draft(client, capability, runtime, monkeypatch):
    original = runtime[2].get_item

    def get(**kwargs):
        if kwargs["Key"]["SK"] == "draft":
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "private diagnostics"}}, "GetItem")
        return original(**kwargs)

    monkeypatch.setattr(runtime[2], "get_item", get)
    for operation in ("read", "write"):
        response = await call(client, capability, operation)
        assert response.status_code == 503
        assert "private diagnostics" not in response.text


async def test_revocation_blocks_reads_writes_and_retry_receipts(client, capability, runtime, db_session_factory):
    await create(client, capability)
    before = rows(runtime)
    async with db_session_factory() as db:
        await db.execute(delete(TeamMembership).where(TeamMembership.id == "member"))
        await db.commit()
    assert (await call(client, capability, "read")).status_code == 404
    assert (await call(client, capability)).status_code == 404
    assert rows(runtime) == before


async def test_forged_wrong_run_expired_and_disabled_authority_fail_closed(client, capability, runtime, monkeypatch):
    before = rows(runtime)
    for operation in ("read", "write"):
        assert (await call(client, "forged", operation)).status_code == 401
        assert (await call(client, capability, operation, run_id="other-run")).status_code == 404
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 300)
    assert (await call(client, capability)).status_code == 401
    monkeypatch.delenv("ADP_CHAT_DATA_ENABLED")
    assert (await call(client, capability, "read")).status_code == 503
    assert (await call(client, capability)).status_code == 503
    assert rows(runtime) == before
