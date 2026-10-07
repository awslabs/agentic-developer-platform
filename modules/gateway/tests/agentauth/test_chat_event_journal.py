"""Durable replay against DynamoDB emulation and the authenticated human route."""

import time

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.chat_delivery import ChatDelivery
from src.agentauth.chat_event_journal import ChatEventJournal
from src.agentauth.chat_session_mailbox import ChatSessionMailbox
from src.orchestration import chat_history as chat
from tests.orchestration import test_chat_history as history

api = history.api
row = history.row
user = history.user

OWNER = ("tenant", "team", "user")
GENERATION = 1_700_000_000


@pytest.fixture
def journal():
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="chat-events",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        )
        yield ChatEventJournal(table), boto3.client("dynamodb", region_name="us-east-1")


def delivery(**changes):
    return ChatDelivery(
        **{
            "run_id": "run-a",
            "session_id": "sess-123",
            "task_id": "run-a",
            "thread_id": "thread",
            "tenant_id": "tenant",
            "team_id": "team",
            "user_id": "user",
            "owner_principal": "owner",
            "session_generation": GENERATION,
            **changes,
        }
    )


def append(journal, event_id, payload=None, *, target=None, now=None):
    store, client = journal
    for _attempt in range(30):
        event, writes = store.prepare(target or delivery(), event_id, "ag_ui", payload or {"text": event_id}, now=now or int(time.time()))
        if not writes:
            return event
        try:
            client.transact_write_items(TransactItems=writes)
            return event
        except ClientError as error:
            if error.response["Error"]["Code"] != "TransactionCanceledException":
                raise
    pytest.fail("journal contention did not resolve")


def replay(journal, **changes):
    return journal[0].replay(
        **{**dict(session_id="sess-123", owner=OWNER, generation=GENERATION, cursor=None, limit=100, now=int(time.time())), **changes}
    )


def test_competing_append_and_duplicate_retry_have_one_contiguous_sequence(journal):
    store, client = journal
    identifiers = [f"event-{index}" for index in range(6)]
    intents = [store.prepare(delivery(), event_id, "ag_ui", {"text": event_id}, now=int(time.time())) for event_id in identifiers]
    client.transact_write_items(TransactItems=intents[0][1])
    for _, writes in intents[1:]:
        with pytest.raises(ClientError) as refused:
            client.transact_write_items(TransactItems=writes)
        assert refused.value.response["Error"]["Code"] == "TransactionCanceledException"
    for event_id in identifiers + identifiers:
        append(journal, event_id)
    page = replay(journal)
    assert page["status"] == "ok"
    assert [event["sequence"] for event in page["events"]] == list(range(1, 7))
    assert {event["event_id"] for event in page["events"]} == set(identifiers)
    assert page["has_more"] is False


def test_receipt_created_during_retry_read_does_not_use_stale_counter(journal, monkeypatch):
    store, client = journal
    original = store._get
    first, writes = store.prepare(delivery(), "same", "ag_ui", {"text": "same"}, now=int(time.time()))
    committed = False

    def commit_before_receipt_read(session_id, key):
        nonlocal committed
        if key.startswith("output-id#") and not committed:
            committed = True
            client.transact_write_items(TransactItems=writes)
        return original(session_id, key)

    monkeypatch.setattr(store, "_get", commit_before_receipt_read)
    assert append(journal, "same") == first
    assert len(replay(journal)["events"]) == 1


def test_retry_recovers_original_cursor_and_rejects_changed_payload(journal):
    first = append(journal, "same")
    before = journal[0].table.scan()["Items"]
    assert append(journal, "same") == first
    assert journal[0].table.scan()["Items"] == before
    with pytest.raises(ChatAuthorizationRefusedError):
        append(journal, "same", {"text": "changed"})


def test_event_retention_never_exceeds_configured_history_retention(journal, monkeypatch):
    monkeypatch.setenv("SESSION_TTL_SECONDS", "60")
    now = int(time.time())
    event = append(journal, "unicode", {"text": "界" * 40_000}, now=now)
    saved = journal[0]._get("sess-123", f"output#{event['cursor'].split(':')[0]}#00000001")
    assert saved["ttl"] == now + 60
    assert replay(journal)["retention_seconds"] == 60
    assert replay(journal, now=now + 61)["reason"] == "retention_gap"


def test_failed_transaction_cannot_leave_counter_receipt_or_event(journal):
    store, client = journal
    _, writes = store.prepare(delivery(), "event", "ag_ui", {"text": "hello"}, now=int(time.time()))
    writes.append(
        {
            "ConditionCheck": {
                "TableName": store.table.name,
                "Key": {"PK": {"S": "guard"}, "SK": {"S": "guard"}},
                "ConditionExpression": "attribute_exists(PK)",
            }
        }
    )
    with pytest.raises(ClientError):
        client.transact_write_items(TransactItems=writes)
    assert store.table.scan()["Items"] == []
    assert append(journal, "event")["sequence"] == 1


@pytest.mark.parametrize("missing", [1, 2, 3])
def test_missing_first_interior_or_tail_event_requires_history_refresh(journal, missing):
    for index in range(3):
        append(journal, str(index))
    state = journal[0]._get("sess-123", "output-state")
    journal[0].table.delete_item(Key={"PK": "session#sess-123", "SK": f"output#{state['journalId']}#{missing:08d}"})
    page = replay(journal)
    assert page["status"] == "history_refresh_required" and page["reason"] == "retention_gap"
    assert page["events"] == [] and page["cursor"].endswith(":3")


@pytest.mark.parametrize("expired", ["output-state", "event"])
def test_expired_rows_are_not_served_while_ttl_deletion_is_pending(journal, expired):
    first = append(journal, "event")
    key = "output-state" if expired == "output-state" else f"output#{first['cursor'].split(':')[0]}#00000001"
    journal[0].table.update_item(
        Key={"PK": "session#sess-123", "SK": key},
        UpdateExpression="SET #ttl = :expired",
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={":expired": int(time.time()) - 1},
    )
    assert replay(journal)["reason"] == "retention_gap"


def test_paging_is_byte_bounded_and_metadata_recreation_invalidates_old_cursor(journal):
    first = append(journal, "one", {"text": "x" * 150_000})
    append(journal, "two", {"text": "y" * 150_000})
    page = replay(journal)
    assert len(page["events"]) == 1 and page["has_more"]
    assert (
        journal[0].replay(session_id="sess-123", owner=OWNER, generation=GENERATION, cursor=page["cursor"], limit=100, now=int(time.time()))[
            "events"
        ][0]["sequence"]
        == 2
    )
    journal[0].table.delete_item(Key={"PK": "session#sess-123", "SK": "output-state"})
    assert replay(journal)["reason"] == "journal_unavailable"
    replacement = append(journal, "three")
    assert replacement["cursor"] != first["cursor"]
    page = journal[0].replay(session_id="sess-123", owner=OWNER, generation=GENERATION, cursor=first["cursor"], limit=100, now=int(time.time()))
    assert page["reason"] == "cursor_changed" and not page["events"]


@pytest.fixture
def replay_api(api, row, journal, monkeypatch):
    client, _table = api
    row["created_at"] = GENERATION
    access = {"allowed": True}

    async def membership(*_):
        return access["allowed"]

    monkeypatch.setattr(chat, "current_chat_member", membership)
    client.app.dependency_overrides[chat.mode_store] = lambda: ChatSessionMailbox(journal[0].table)
    return client, access


PATH = "/chat/sessions/sess-123/events"


def test_disconnect_reconnect_replays_owned_events_once_in_order(replay_api, journal):
    client, _access = replay_api
    for index in range(5):
        append(journal, str(index))
        append(journal, str(index), target=delivery(session_id="other-session", user_id="other-user"))
    seen, cursor = [], None
    for _page in range(3):
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        response = client.get(PATH, params=params)
        assert response.status_code == 200, response.text
        assert response.headers["Cache-Control"] == "no-store"
        page = response.json()
        seen.extend(page["events"])
        cursor = page["cursor"]
    assert [event["sequence"] for event in seen] == [1, 2, 3, 4, 5]
    assert len({event["cursor"] for event in seen}) == 5
    assert client.get(PATH, params={"cursor": cursor}).json()["events"] == []


def test_replay_cursor_survives_new_commits_and_repeated_page_requests(replay_api, journal):
    client, _access = replay_api
    append(journal, "before-disconnect")
    first = client.get(PATH, params={"limit": 1}).json()
    assert [event["sequence"] for event in first["events"]] == [1]
    cursor = first["cursor"]

    append(journal, "during-disconnect")
    append(journal, "after-reconnect")
    repeated = client.get(PATH, params={"cursor": cursor, "limit": 1}).json()
    assert repeated["status"] == "ok" and repeated["has_more"]
    assert [event["payload"]["text"] for event in repeated["events"]] == ["during-disconnect"]
    final = client.get(PATH, params={"cursor": repeated["cursor"], "limit": 1}).json()
    assert [event["payload"]["text"] for event in final["events"]] == ["after-reconnect"]
    assert final["cursor"] != repeated["cursor"] != cursor
    assert client.get(PATH, params={"cursor": final["cursor"]}).json()["events"] == []


def test_replay_route_reports_missing_output_without_serving_partial_page(replay_api, journal):
    client, _access = replay_api
    first = append(journal, "first")
    append(journal, "second")
    journal[0].table.delete_item(Key={"PK": "session#sess-123", "SK": f"output#{first['cursor'].split(':')[0]}#00000001"})

    response = client.get(PATH)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["status"] == "history_refresh_required"
    assert response.json()["reason"] == "retention_gap"
    assert response.json()["events"] == []


@pytest.mark.parametrize(
    "field,value", [("owner_user_id", "other"), ("tenant_id", "other"), ("owner_principal", "other"), ("created_at", GENERATION + 1)]
)
def test_each_page_reauthorizes_owner_and_generation(replay_api, journal, row, field, value):
    client, _access = replay_api
    cursor = append(journal, "one")["cursor"]
    row[field] = value
    response = client.get(PATH, params={"cursor": cursor})
    assert response.status_code == 404 and "events" not in response.json()


def test_revocation_during_read_discards_replay_page(replay_api, journal, monkeypatch):
    client, access = replay_api
    append(journal, "one")
    original = journal[0].table.query

    def revoke(**kwargs):
        result = original(**kwargs)
        access["allowed"] = False
        return result

    monkeypatch.setattr(journal[0].table, "query", revoke)
    assert client.get(PATH).status_code == 404


@pytest.mark.parametrize("field", ["tenantId", "teamId", "ownerUserId", "sessionGeneration"])
def test_replaced_journal_owner_cannot_be_replayed(replay_api, journal, field):
    append(journal, "one")
    journal[0].table.update_item(
        Key={"PK": "session#sess-123", "SK": "output-state"},
        UpdateExpression="SET #field = :value",
        ExpressionAttributeNames={"#field": field},
        ExpressionAttributeValues={":value": "foreign"},
    )
    assert replay_api[0].get(PATH).status_code == 404


def test_replay_keeps_feature_data_and_human_gates(replay_api, journal, user, monkeypatch):
    client, _access = replay_api
    append(journal, "one")
    monkeypatch.setenv("FEATURE_CHAT_ENABLED", "false")
    assert client.get(PATH).status_code == 503
    monkeypatch.setenv("FEATURE_CHAT_ENABLED", "true")
    user.account_type = "agent"
    assert client.get(PATH).status_code == 403
    user.account_type = "human"
    del client.app.dependency_overrides[chat.mode_store]
    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "false")
    assert client.get(PATH).status_code == 503


@pytest.mark.parametrize("params", [{"cursor": "bad"}, {"cursor": "a" * 500}, {"limit": 0}, {"limit": 101}])
def test_invalid_cursor_and_unbounded_pages_rejected(replay_api, params):
    assert replay_api[0].get(PATH, params=params).status_code == 422
