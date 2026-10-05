"""Trusted dispatch input and atomic context materialization against Moto storage."""

import json
from datetime import UTC, datetime

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth import chat_admission, chat_data_routes
from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.external_roots import provision_root
from src.orchestration import intake_wiring
from tests.agentauth.test_chat_data_routes import admit, exchange
from tests.agentauth.test_chat_data_routes import client as client_fixture
from tests.agentauth.test_chat_data_routes import runtime as runtime_fixture
from tests.agentauth.test_chat_data_routes import store as store_fixture
from tests.agentauth.test_chat_data_routes import sts as sts_fixture
from tests.agentauth.test_chat_history_routes import read

client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
HEADER = {"PK": "session#session-a", "SK": "header"}
INPUT = {"PK": "chat-input#run-user", "SK": "input"}


@pytest.fixture(autouse=True)
def retained_input_table(runtime, monkeypatch):
    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "true")
    monkeypatch.setenv("BG_INTAKE_CONTEXT_TABLE", runtime[2].name)
    monkeypatch.setattr(intake_wiring, "_context_table", runtime[2])
    runtime[2].meta.client.update_time_to_live(TableName=runtime[2].name, TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"})


def prepare(runtime, **changes):
    envelope = {
        "message_id": "run-user",
        "tenant_id": "tenant",
        "persona": "developer",
        "session_id": "session-a",
        "source_ref": {"repo": "chat/session-a"},
        "arrived_at": datetime.fromtimestamp(runtime[-1], UTC).isoformat(),
        "message": "Please inspect this attachment",
        "attachments": ["art_0123456789ab"],
        **changes,
    }
    provision_root(runtime[1].store, envelope, source="chat", human_id="human", now=datetime.fromtimestamp(runtime[-1], UTC))
    return envelope


async def accept(client, runtime, envelope, **changes):
    return await admit(client, runtime, run_id=envelope["message_id"], envelope_digest=envelope_digest(envelope), **changes)


def rows(runtime):
    return sorted((row for row in runtime[2].scan()["Items"] if row["PK"] == HEADER["PK"]), key=lambda row: row["SK"])


async def test_empty_session_accepts_authenticated_input_and_attachments_once(client, runtime):
    assert rows(runtime) == []
    envelope = prepare(runtime)
    response = await accept(client, runtime, envelope)
    assert response.status_code == 200, response.text
    original = rows(runtime)
    assert "Item" not in runtime[2].get_item(Key=INPUT)
    assert len(original) == 4
    header = runtime[2].get_item(Key=HEADER)["Item"]
    assert (header["historyVersion"], header["historyNextOrdinal"]) == (1, 2)
    message = next(row for row in original if row["SK"].startswith("msg#"))
    receipt = next(row for row in original if row["SK"].startswith("turn#"))
    assert message["role"] == "user" and message["content"] == envelope["message"]
    assert message["parts"] == [{"type": "file", "artifactId": "art_0123456789ab"}]
    assert message["runId"] == "run-user" and message["ownerUserId"] == "human" and message["tenantId"] == "tenant"
    assert receipt["ref"] == message["SK"].removeprefix("msg#") and receipt["ordinal"] == 1
    prepare(runtime)
    assert "Item" not in runtime[2].get_item(Key=INPUT)
    assert (await accept(client, runtime, envelope)).status_code == 200
    assert rows(runtime) == original
    token = (await exchange(client)).json()["capability"]
    page = (await read(client, token, run_id="run-user")).json()
    assert page["version"] == 1 and page["entries"][0]["ref"] == receipt["ref"]
    sources = await read(client, token, "messages", run_id="run-user", ids=[receipt["ref"]])
    assert sources.json()["entries"][0]["message"]["parts"] == message["parts"]


async def test_changed_root_input_cannot_replace_an_accepted_user_turn(client, runtime):
    envelope = prepare(runtime)
    assert (await accept(client, runtime, envelope)).status_code == 200
    original = rows(runtime)
    with pytest.raises(BootstrapRefusedError):
        prepare(runtime, message="Replacement input")
    assert rows(runtime) == original


@pytest.mark.parametrize("removed", [False, True])
async def test_missing_or_modified_protected_input_is_not_treated_as_a_legacy_run(client, runtime, removed):
    envelope = prepare(runtime)
    protected = runtime[1].store._read("TENANT#tenant", "EXEC#run-user")
    if removed:
        del protected["chat_user_turn"]
    else:
        protected["chat_user_turn"]["M"]["input_digest"] = {"S": "0" * 64}
    runtime[1].store.client.put_item(TableName=runtime[1].store.table, Item=protected)
    assert (await accept(client, runtime, envelope)).status_code == 503
    assert rows(runtime) == []


async def test_sandbox_cannot_supply_user_input_to_admission(client, runtime):
    envelope = prepare(runtime)
    response = await accept(client, runtime, envelope, message="Forged user input", attachments=[])
    assert response.status_code == 422
    assert rows(runtime) == []
    response = await client.post(
        "/internal/v1/agent/chat/data/admit",
        json={"run_id": envelope["message_id"], "envelope_digest": envelope_digest(envelope), "pod_name": "chat-a", "pod_uid": "chat-pod"},
        headers={"X-Adp-Workload-Token": "chat-token"},
    )
    assert response.status_code in {401, 403}
    assert rows(runtime) == []


@pytest.mark.parametrize("lost_response", [False, True])
async def test_failed_admission_and_retry_preserve_one_user_turn(client, runtime, monkeypatch, lost_response):
    envelope = prepare(runtime)
    original = runtime[1].store.client.transact_write_items

    def commit(**kwargs):
        if any(operation.get("Put", {}).get("TableName") == runtime[2].name for operation in kwargs["TransactItems"]):
            if lost_response:
                original(**kwargs)
            raise EndpointConnectionError(endpoint_url="https://storage.test")
        return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", commit)
    assert (await accept(client, runtime, envelope)).status_code == 503
    assert len(rows(runtime)) == (4 if lost_response else 0)
    assert ("Item" in runtime[2].get_item(Key=INPUT)) is not lost_response
    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original)
    assert (await accept(client, runtime, envelope)).status_code == 200
    assert len(rows(runtime)) == 4
    assert runtime[2].get_item(Key=HEADER)["Item"]["historyVersion"] == 1


@pytest.mark.parametrize("target", ["history", "input", "grant", "staged_input"])
async def test_concurrent_history_or_protected_input_change_rolls_back_user_turn(client, runtime, monkeypatch, target):
    envelope = prepare(runtime)
    header = {**runtime[3], "historyVersion": 5, "historyNextOrdinal": 10}
    del header["chatLease"]
    runtime[2].put_item(Item=header)
    original = runtime[1].store.client.transact_write_items

    def commit(**kwargs):
        if any(operation.get("Put", {}).get("TableName") == runtime[2].name for operation in kwargs["TransactItems"]):
            if target == "history":
                runtime[2].put_item(Item={**header, "historyVersion": 6})
            elif target == "grant":
                runtime[1].store.client.update_item(
                    TableName=runtime[1].store.table,
                    Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "GRANT#run-user#1"}},
                    UpdateExpression="SET revoked = :revoked",
                    ExpressionAttributeValues={":revoked": {"BOOL": True}},
                )
            elif target == "input":
                runtime[1].store.client.update_item(
                    TableName=runtime[1].store.table,
                    Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-user"}},
                    UpdateExpression="REMOVE chat_user_turn",
                )
            else:
                runtime[2].delete_item(Key=INPUT)
        return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", commit)
    assert (await accept(client, runtime, envelope)).status_code == 404
    assert len(rows(runtime)) == 1 and "chatLease" not in rows(runtime)[0]
    assert runtime[1].store._read("CHAT-LAUNCH#run-user", "LAUNCH") is None


async def test_following_turn_appends_after_existing_ordered_history(client, runtime, monkeypatch):
    first = prepare(runtime)
    assert (await accept(client, runtime, first)).status_code == 200
    runtime[1].store.client.update_item(
        TableName=runtime[1].store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-user"}},
        UpdateExpression="SET #status = :done",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":done": {"S": "completed"}},
    )
    runtime[4]["uid"] = "next-chat-pod"
    second = prepare(runtime, message_id="run-next", message="A following question", attachments=[])
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 10)
    response = await accept(client, runtime, second, pod_uid="next-chat-pod")
    assert response.status_code == 200, response.text
    items = [row for row in rows(runtime) if row["SK"].startswith("item#")]
    assert [row["ordinal"] for row in items] == [1, 2]
    assert len({row["ref"] for row in items}) == 2
    header = runtime[2].get_item(Key=HEADER)["Item"]
    assert header["historyVersion"] == 2
    assert header["lastActivityAt"] == datetime.fromtimestamp(runtime[-1] + 10, UTC).isoformat()
    assert header["ttl"] == runtime[-1] + 10 + 90 * 86400


@pytest.mark.parametrize("target", ["message", "receipt"])
async def test_admission_retry_does_not_claim_success_after_user_source_disappears(client, runtime, target):
    envelope = prepare(runtime)
    assert (await accept(client, runtime, envelope)).status_code == 200
    prefix = "msg#" if target == "message" else "turn#"
    row = next(row for row in rows(runtime) if row["SK"].startswith(prefix))
    runtime[2].delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
    assert (await accept(client, runtime, envelope)).status_code == 503


def assert_content_absent_from_authority(runtime, envelope):
    serialized = json.dumps(runtime[1].store.client.scan(TableName=runtime[1].store.table)["Items"])
    assert envelope["message"] not in serialized
    assert envelope["attachments"][0] not in serialized
    execution = runtime[1].store._read("TENANT#tenant", "EXEC#run-user")
    assert set(execution["chat_user_turn"]["M"]) == {"input_digest", "message_digest", "expires_at"}
    assert "ttl" not in execution


@pytest.mark.parametrize("failed_admission", [False, True])
async def test_abandoned_or_failed_input_expires_without_removing_audit_or_resurrecting_payload(client, runtime, monkeypatch, failed_admission):
    monkeypatch.setenv("SESSION_TTL_SECONDS", "5")
    envelope = prepare(runtime)
    staged = runtime[2].get_item(Key=INPUT)["Item"]
    assert staged["ttl"] == runtime[-1] + 5
    assert staged["payload"]["input"]["message"] == envelope["message"]
    assert_content_absent_from_authority(runtime, envelope)
    if failed_admission:
        original = runtime[1].store.client.transact_write_items

        def unavailable(**kwargs):
            if any(operation.get("Delete", {}).get("TableName") == runtime[2].name for operation in kwargs["TransactItems"]):
                raise EndpointConnectionError(endpoint_url="https://storage.test")
            return original(**kwargs)

        monkeypatch.setattr(runtime[1].store.client, "transact_write_items", unavailable)
        assert (await accept(client, runtime, envelope)).status_code == 503
        monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original)
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 6)
    assert (await accept(client, runtime, envelope)).status_code == 503
    assert rows(runtime) == []
    runtime[2].delete_item(Key=INPUT)
    prepare(runtime)
    assert "Item" not in runtime[2].get_item(Key=INPUT)
    assert (await accept(client, runtime, envelope)).status_code == 503
    assert_content_absent_from_authority(runtime, envelope)


async def test_admitted_input_cleanup_keeps_only_audit_digests(client, runtime, monkeypatch):
    monkeypatch.setenv("SESSION_TTL_SECONDS", "5")
    envelope = prepare(runtime)
    assert (await accept(client, runtime, envelope)).status_code == 200
    assert "Item" not in runtime[2].get_item(Key=INPUT)
    assert_content_absent_from_authority(runtime, envelope)
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 6)
    assert (await accept(client, runtime, envelope)).status_code == 404
    for row in rows(runtime):
        runtime[2].delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
    prepare(runtime)
    assert runtime[2].scan()["Items"] == []
    assert_content_absent_from_authority(runtime, envelope)


@pytest.mark.parametrize("field", ["payload", "ttl", "tenantId", "ownerUserId"])
async def test_staged_input_tampering_refuses_admission(client, runtime, field):
    envelope = prepare(runtime)
    staged = runtime[2].get_item(Key=INPUT)["Item"]
    if field == "payload":
        staged["payload"]["input"]["message"] = "Modified prompt"
    elif field == "ttl":
        staged["ttl"] += 1
    else:
        staged[field] = "another-owner"
    runtime[2].put_item(Item=staged)
    assert (await accept(client, runtime, envelope)).status_code == 503
    assert rows(runtime) == []


@pytest.mark.parametrize("target", ["message", "receipt"])
async def test_consumed_input_retry_detects_changed_materialized_content(client, runtime, target):
    envelope = prepare(runtime)
    assert (await accept(client, runtime, envelope)).status_code == 200
    prefix = "msg#" if target == "message" else "turn#"
    row = next(row for row in rows(runtime) if row["SK"].startswith(prefix))
    row["content" if target == "message" else "inputDigest"] = "Modified value"
    runtime[2].put_item(Item=row)
    assert (await accept(client, runtime, envelope)).status_code == 503


def test_staged_input_retention_never_exceeds_root_lifetime(runtime):
    prepare(runtime)
    assert runtime[2].get_item(Key=INPUT)["Item"]["ttl"] == runtime[-1] + 2 * 3600


def test_unconfigured_input_storage_fails_closed(runtime, monkeypatch):
    monkeypatch.delenv("BG_INTAKE_CONTEXT_TABLE")
    with pytest.raises(BootstrapRefusedError):
        prepare(runtime)
    assert runtime[1].store._read("TENANT#tenant", "EXEC#run-user") is None
    assert runtime[2].scan()["Items"] == [runtime[3]]


@pytest.mark.parametrize("lost_response", [False, True])
def test_root_input_and_execution_commit_atomically(runtime, monkeypatch, lost_response):
    original = runtime[1].store.client.transact_write_items

    def unavailable(**kwargs):
        if lost_response:
            original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://storage.test")

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", unavailable)
    if lost_response:
        prepare(runtime)
    else:
        with pytest.raises(BootstrapRefusedError):
            prepare(runtime)
    assert (runtime[1].store._read("TENANT#tenant", "EXEC#run-user") is not None) is lost_response
    assert ("Item" in runtime[2].get_item(Key=INPUT)) is lost_response
    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original)
    envelope = prepare(runtime)
    assert_content_absent_from_authority(runtime, envelope)
    assert "Item" in runtime[2].get_item(Key=INPUT)


async def test_consumed_input_retry_survives_input_expiry_and_verifies_scrubbed_unicode_content(client, runtime, monkeypatch):
    monkeypatch.setenv("SESSION_TTL_SECONDS", "5")
    envelope = prepare(runtime, message="Inspect π password=synthetic-password")
    assert (await accept(client, runtime, envelope)).status_code == 200
    launch = chat_admission.ChatLaunchStore(runtime[1].store).load("run-user")
    chat_admission.renew_lease(runtime[1], launch, now=runtime[-1] + 3)
    monkeypatch.setattr(chat_data_routes, "clock", lambda: runtime[-1] + 6)
    provision_root(runtime[1].store, envelope, source="chat", human_id="human", now=datetime.fromtimestamp(runtime[-1] + 6, UTC))
    assert (await accept(client, runtime, envelope)).status_code == 200
    message = next(row for row in rows(runtime) if row["SK"].startswith("msg#"))
    assert message["content"] == "Inspect π [REDACTED:PASSWORD]"
    assert "Item" not in runtime[2].get_item(Key=INPUT)
    assert_content_absent_from_authority(runtime, envelope)
