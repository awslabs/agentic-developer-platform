"""Moto-backed pending-operation claims; these never authorize provider spending."""

import hashlib
import json

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, ChatLaunchStore
from src.agentauth.chat_model_journal import ChatModelJournal
from src.agentauth.store import AuthorityStoreError
from tests.agentauth.test_chat_user_turn import accept, prepare
from tests.agentauth.test_chat_user_turn import client as client_fixture
from tests.agentauth.test_chat_user_turn import retained_input_table as retained_input_table_fixture
from tests.agentauth.test_chat_user_turn import runtime as runtime_fixture
from tests.agentauth.test_chat_user_turn import store as store_fixture
from tests.agentauth.test_chat_user_turn import sts as sts_fixture

client = client_fixture
runtime = runtime_fixture
store = store_fixture
sts = sts_fixture
retained_input_table = retained_input_table_fixture
DIGEST = hashlib.sha256(b"sample model request").hexdigest()
MODEL_ID = "anthropic.claude-test-v1"


async def admitted(client, runtime):
    envelope = prepare(runtime)
    response = await accept(client, runtime, envelope)
    assert response.status_code == 200, response.text
    return ChatLaunchStore(runtime[1].store).load(envelope["message_id"])


def claim(journal, launch, runtime, **changes):
    return journal.claim(
        launch,
        operation_id=changes.get("operation_id", "call-1"),
        request_digest=changes.get("request_digest", DIGEST),
        model_id=changes.get("model_id", MODEL_ID),
        now=runtime[-1],
    )


async def test_claim_records_pending_once_and_never_reports_budget_or_replay_authority(client, runtime):
    launch = await admitted(client, runtime)
    journal = ChatModelJournal(runtime[1])
    result = claim(journal, launch, runtime)
    assert result["status"] == "pending" and result["handoff"] == "not_started"
    assert result["reservation_status"] == "unknown" and result["usage"] is None
    assert result["automatic_replay_permitted"] is False
    assert claim(journal, launch, runtime) == result
    assert journal._read("run-user", "call-1") == result
    with pytest.raises(ChatAuthorizationRefusedError):
        claim(journal, launch, runtime, request_digest="f" * 64)
    with pytest.raises(ChatAuthorizationRefusedError):
        claim(journal, launch, runtime, model_id="different-model")
    assert claim(journal, launch, runtime, operation_id="call-2")["operation_id"] == "call-2"


@pytest.mark.parametrize("field,value", [("operation_id", "../bad"), ("operation_id", ""), ("request_digest", "wrong"), ("model_id", "*")])
async def test_claim_rejects_invalid_operation_metadata(client, runtime, field, value):
    launch = await admitted(client, runtime)
    with pytest.raises(ChatAuthorizationRefusedError):
        claim(ChatModelJournal(runtime[1]), launch, runtime, **{field: value})


@pytest.mark.parametrize("target", ["lease", "receipt", "launch", "input_digest", "message"])
async def test_concurrent_lease_turn_or_launch_change_cancels_pending_claim(client, runtime, monkeypatch, target):
    launch = await admitted(client, runtime)
    journal = ChatModelJournal(runtime[1])
    original = runtime[1].store.client.transact_write_items

    def mutate(**kwargs):
        if target == "lease":
            row = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]
            row["chatLease"]["generation"] += 1
            runtime[2].put_item(Item=row)
        elif target in {"receipt", "input_digest", "message"}:
            row_key = "msg#user_" + hashlib.sha256(b"run-user").hexdigest() if target == "message" else "turn#run-user"
            row = runtime[2].get_item(Key={"PK": "session#session-a", "SK": row_key})["Item"]
            field = {"receipt": "status", "input_digest": "inputDigest", "message": "content"}[target]
            row[field] = {"receipt": "interrupted", "input_digest": "forged", "message": "other content"}[target]
            runtime[2].put_item(Item=row)
        else:
            runtime[1].store.client.delete_item(
                TableName=runtime[1].store.table,
                Key={"pk": {"S": "CHAT-LAUNCH#run-user"}, "sk": {"S": "LAUNCH"}},
            )
        return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", mutate)
    with pytest.raises(ChatAuthorizationRefusedError):
        claim(journal, launch, runtime)
    assert journal._read("run-user", "call-1") is None


@pytest.mark.parametrize("target", ["receipt", "launch"])
async def test_duplicate_cannot_reuse_pending_claim_after_turn_or_launch_changes(client, runtime, target):
    launch = await admitted(client, runtime)
    journal = ChatModelJournal(runtime[1])
    claim(journal, launch, runtime)
    if target == "receipt":
        row = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "turn#run-user"})["Item"]
        row["status"] = "interrupted"
        runtime[2].put_item(Item=row)
    else:
        runtime[1].store.client.delete_item(
            TableName=runtime[1].store.table,
            Key={"pk": {"S": "CHAT-LAUNCH#run-user"}, "sk": {"S": "LAUNCH"}},
        )
    with pytest.raises(ChatAuthorizationRefusedError):
        claim(journal, launch, runtime)


async def test_lost_commit_response_is_uncertain_not_automatically_replayed(client, runtime, monkeypatch):
    launch = await admitted(client, runtime)
    journal = ChatModelJournal(runtime[1])
    original = runtime[1].store.client.transact_write_items

    def lose_reply(**kwargs):
        original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://storage.test")

    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", lose_reply)
    with pytest.raises(ChatAuthorizationUnavailableError):
        claim(journal, launch, runtime)
    monkeypatch.setattr(runtime[1].store.client, "transact_write_items", original)
    assert claim(journal, launch, runtime)["automatic_replay_permitted"] is False


async def test_storage_unavailable_during_replay_fails_closed(client, runtime, monkeypatch):
    launch = await admitted(client, runtime)
    journal = ChatModelJournal(runtime[1])
    claim(journal, launch, runtime)

    def fail_read(*args, **kwargs):
        raise EndpointConnectionError(endpoint_url="https://storage.test")

    monkeypatch.setattr(runtime[1].store.client, "get_item", fail_read)
    with pytest.raises(ChatAuthorizationUnavailableError):
        claim(journal, launch, runtime)


async def test_claim_accepts_scoped_inference_profile_id_without_authorizing_it(client, runtime):
    launch = await admitted(client, runtime)
    result = claim(
        ChatModelJournal(runtime[1]),
        launch,
        runtime,
        model_id="arn:aws:bedrock:us-west-2:123456789012:inference-profile/us.anthropic.claude-v1:0",
    )
    assert result["status"] == "pending" and result["reservation_status"] == "unknown"


async def test_forged_principal_does_not_claim_accepted_turn(client, runtime):
    launch = await admitted(client, runtime)
    with pytest.raises(ChatAuthorizationRefusedError):
        claim(ChatModelJournal(runtime[1]), launch.model_copy(update={"user_id": "other-user"}), runtime)


async def test_journal_read_outage_on_duplicate_is_unavailable(client, runtime, monkeypatch):
    launch = await admitted(client, runtime)
    journal = ChatModelJournal(runtime[1])
    claim(journal, launch, runtime)
    original = runtime[1].store._read

    def unavailable(pk, sk):
        if pk == "CHAT-MODEL#run-user":
            raise AuthorityStoreError("storage unavailable")
        return original(pk, sk)

    monkeypatch.setattr(runtime[1].store, "_read", unavailable)
    with pytest.raises(ChatAuthorizationUnavailableError):
        claim(journal, launch, runtime)


@pytest.mark.parametrize("target", ["input_digest", "message"])
async def test_existing_trusted_turn_change_refuses_model_claim(client, runtime, target):
    launch = await admitted(client, runtime)
    key = "turn#run-user" if target == "input_digest" else "msg#user_" + hashlib.sha256(b"run-user").hexdigest()
    row = runtime[2].get_item(Key={"PK": "session#session-a", "SK": key})["Item"]
    row["inputDigest" if target == "input_digest" else "content"] = "other content"
    runtime[2].put_item(Item=row)
    with pytest.raises(ChatAuthorizationUnavailableError):
        claim(ChatModelJournal(runtime[1]), launch, runtime)


async def test_expired_launch_refuses_even_when_lease_callback_is_stale(client, runtime, monkeypatch):
    launch = await admitted(client, runtime)
    monkeypatch.setattr(runtime[1], "current", lambda *_: True)
    with pytest.raises(ChatAuthorizationRefusedError):
        ChatModelJournal(runtime[1]).claim(launch, operation_id="call-1", request_digest=DIGEST, model_id=MODEL_ID, now=launch.expires_at)


@pytest.mark.parametrize("malformed", ['{"status":"dispatched"}', '["not an operation"]'])
async def test_duplicate_rejects_mutated_or_malformed_pending_record(client, runtime, malformed):
    launch = await admitted(client, runtime)
    journal = ChatModelJournal(runtime[1])
    claim(journal, launch, runtime)
    item = runtime[1].store._read("CHAT-MODEL#run-user", "OP#call-1")
    if malformed.startswith("{"):
        record = json.loads(item["document"]["S"])
        record["status"] = "dispatched"
        malformed = json.dumps(record)
    item["document"] = {"S": malformed}
    runtime[1].store.client.put_item(TableName=runtime[1].store.table, Item=item)
    with pytest.raises((ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError)):
        claim(journal, launch, runtime)


async def test_protected_input_store_outage_refuses_claim(client, runtime, monkeypatch):
    launch = await admitted(client, runtime)
    original = runtime[1].store.authority.load_execution
    calls = 0

    def lookup(**kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AuthorityStoreError("storage unavailable")
        return original(**kwargs)

    monkeypatch.setattr(runtime[1].store.authority, "load_execution", lookup)
    with pytest.raises(ChatAuthorizationUnavailableError):
        claim(ChatModelJournal(runtime[1]), launch, runtime)
    assert runtime[1].store._read("CHAT-MODEL#run-user", "OP#call-1") is None
