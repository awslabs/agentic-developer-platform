"""Server-owned chat mode and durable per-session turn ordering in DynamoDB."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock, local

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_session_mailbox import AcceptedTurn, ChatSessionMailbox
from src.orchestration.chat_data_migration import _owner_fields

OWNER = ("tenant", "team", "alice")
NOW = 1_800_000_000


@pytest.fixture
def mailbox():
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="chat-context",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        )
        yield ChatSessionMailbox(table), table


def turn(identifier, message="Hello"):
    return AcceptedTurn(turn_id=identifier, message=message)


def test_interleaved_sessions_retain_mode_and_separate_order_on_reload(mailbox):
    store, table = mailbox
    assert store.state(session_id="a", owner=OWNER, now=NOW) == {"mode": "ephemeral", "sequence": 0, "health": "idle"}
    assert store.select_mode(session_id="a", owner=OWNER, mode="persistent", now=NOW) == "persistent"
    assert store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW) == {"sequence": 1, "mode": "persistent"}
    assert store.accept(session_id="b", owner=OWNER, turn=turn("b1"), now=NOW) == {"sequence": 1, "mode": "ephemeral"}
    assert store.accept(session_id="a", owner=OWNER, turn=turn("a2"), now=NOW) == {"sequence": 2, "mode": "persistent"}
    reloaded = ChatSessionMailbox(table)
    assert reloaded.state(session_id="a", owner=OWNER, now=NOW) == {"mode": "persistent", "sequence": 2, "health": "idle"}
    assert reloaded.state(session_id="b", owner=OWNER, now=NOW) == {"mode": "ephemeral", "sequence": 1, "health": "idle"}
    assert [table.get_item(Key={"PK": "session#a", "SK": f"mailbox#{number:08d}"})["Item"]["turnId"] for number in (1, 2)] == ["a1", "a2"]


def test_new_user_turn_advances_authoritative_activity_without_retry_or_clock_rollback(mailbox):
    store, table = mailbox
    store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW)
    store.accept(session_id="b", owner=OWNER, turn=turn("b1"), now=NOW)
    original = table.get_item(Key={"PK": "session#a", "SK": "header"})["Item"]["lastActivityAt"]
    store.accept(session_id="a", owner=OWNER, turn=turn("a2"), now=NOW + 60)
    latest = table.get_item(Key={"PK": "session#a", "SK": "header"})["Item"]["lastActivityAt"]
    assert latest > original
    assert table.get_item(Key={"PK": "session#b", "SK": "header"})["Item"]["lastActivityAt"] == original
    store.accept(session_id="a", owner=OWNER, turn=turn("a2"), now=NOW + 90)
    assert table.get_item(Key={"PK": "session#a", "SK": "header"})["Item"]["lastActivityAt"] == latest
    store.accept(session_id="a", owner=OWNER, turn=turn("a3"), now=NOW + 30)
    assert table.get_item(Key={"PK": "session#a", "SK": "header"})["Item"]["lastActivityAt"] == latest
    assert store.state(session_id="a", owner=OWNER, now=NOW + 90)["sequence"] == 3


def test_recovery_marker_cannot_match_the_session_header_expiry_stream_filter(mailbox):
    store, table = mailbox
    store.select_mode(session_id="a", owner=OWNER, mode="persistent", now=NOW)
    store.accept(session_id="a", owner=OWNER, turn=turn("header"), now=NOW)
    assert table.get_item(Key={"PK": "chat-notifications", "SK": "turn#header"})["Item"]["sessionId"] == "a"
    assert "Item" not in table.get_item(Key={"PK": "chat-notifications", "SK": "header"})


@pytest.mark.parametrize("invalid_activity", ["not-a-timestamp", "2024-01-01T00:00:00", 3])
def test_corrupt_activity_cannot_advance_turn_or_hide_idle_clock(mailbox, invalid_activity):
    store, table = mailbox
    store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW)
    table.update_item(
        Key={"PK": "session#a", "SK": "header"},
        UpdateExpression="SET lastActivityAt = :value",
        ExpressionAttributeValues={":value": invalid_activity},
    )
    with pytest.raises(ChatAuthorizationUnavailableError):
        store.accept(session_id="a", owner=OWNER, turn=turn("a2"), now=NOW + 60)
    assert store.state(session_id="a", owner=OWNER, now=NOW + 60)["sequence"] == 1
    assert table.get_item(Key={"PK": "session#a", "SK": "mailbox-id#a2"}).get("Item") is None


def test_concurrent_turns_get_unique_sequence_in_each_session(mailbox, monkeypatch):
    store, table = mailbox
    store.select_mode(session_id="a", owner=OWNER, mode="persistent", now=NOW)
    store.select_mode(session_id="b", owner=OWNER, mode="persistent", now=NOW)
    original = table.meta.client.transact_write_items
    barrier, lock, participant = Barrier(4), Lock(), local()
    conflicts = []

    def transact(**request):
        # Force both writers for each session to observe the same sequence.
        # Moto's table snapshot/rollback is not thread-safe; serialize only
        # the atomic storage operation, leaving the competing reads intact.
        if not getattr(participant, "started", False):
            participant.started = True
            barrier.wait(timeout=10)
        with lock:
            try:
                return original(**request)
            except ClientError as error:
                if error.response["Error"]["Code"] == "TransactionCanceledException":
                    conflicts.append(error)
                raise

    monkeypatch.setattr(table.meta.client, "transact_write_items", transact)
    with ThreadPoolExecutor(max_workers=4) as executor:
        receipts = list(
            executor.map(
                lambda item: (item[0], store.accept(session_id=item[0], owner=OWNER, turn=turn(item[1]), now=NOW)["sequence"]),
                [("a", "a1"), ("b", "b1"), ("a", "a2"), ("b", "b2")],
            )
        )
    assert sorted(receipts) == [("a", 1), ("a", 2), ("b", 1), ("b", 2)]
    assert len(conflicts) == 2
    for session_id in ("a", "b"):
        assert store.state(session_id=session_id, owner=OWNER, now=NOW)["sequence"] == 2
        assert {table.get_item(Key={"PK": f"session#{session_id}", "SK": f"mailbox#{sequence:08d}"})["Item"]["turnId"] for sequence in (1, 2)} == {
            f"{session_id}1",
            f"{session_id}2",
        }


def test_legacy_header_without_mode_or_sequence_can_be_extended(mailbox):
    store, table = mailbox
    table.put_item(Item={"PK": "session#a", "SK": "header", **_owner_fields(OWNER), "status": "active", "ttl": NOW + 600})
    assert store.state(session_id="a", owner=OWNER, now=NOW)["mode"] == "ephemeral"
    assert store.select_mode(session_id="a", owner=OWNER, mode="persistent", now=NOW) == "persistent"
    assert store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW) == {"sequence": 1, "mode": "persistent"}
    table.put_item(Item={"PK": "session#b", "SK": "header", **_owner_fields(OWNER), "status": "active", "ttl": NOW + 600})
    assert store.accept(session_id="b", owner=OWNER, turn=turn("b1"), now=NOW) == {"sequence": 1, "mode": "ephemeral"}


def test_retry_returns_original_receipt_without_rewriting_turn(mailbox):
    store, table = mailbox
    first = store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW)
    assert store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW) == first
    assert store.state(session_id="a", owner=OWNER, now=NOW)["sequence"] == 1
    with pytest.raises(ChatAuthorizationRefusedError):
        store.accept(session_id="a", owner=OWNER, turn=turn("a1", "changed"), now=NOW)
    assert table.get_item(Key={"PK": "session#a", "SK": "mailbox#00000002"}).get("Item") is None


@pytest.mark.parametrize("status", ["result_recorded", "completed", "failed", "cancelled", "interrupted"])
def test_duplicate_after_mode_or_turn_state_change_keeps_original_receipt(mailbox, status):
    store, table = mailbox
    receipt = store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW)
    store.select_mode(session_id="a", owner=OWNER, mode="persistent", now=NOW)
    key = {"PK": "session#a", "SK": "mailbox#00000001"}
    table.put_item(Item={**table.get_item(Key=key)["Item"], "status": status})
    before = table.scan(ConsistentRead=True)["Items"]
    assert store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW + 1) == receipt
    assert table.scan(ConsistentRead=True)["Items"] == before


@pytest.mark.parametrize("same_turn", [True, False])
def test_concurrent_duplicate_acceptance_keeps_one_sequence(mailbox, monkeypatch, same_turn):
    store, table = mailbox
    store.select_mode(session_id="a", owner=OWNER, mode="persistent", now=NOW)
    original = table.meta.client.transact_write_items
    barrier, lock = Barrier(2), Lock()

    def transact(**request):
        barrier.wait(timeout=10)
        with lock:
            return original(**request)

    monkeypatch.setattr(table.meta.client, "transact_write_items", transact)

    def accept(message):
        try:
            return store.accept(session_id="a", owner=OWNER, turn=turn("a1", message), now=NOW)
        except ChatAuthorizationRefusedError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(accept, ["Hello", "Hello" if same_turn else "Different"]))
    receipt = {"sequence": 1, "mode": "persistent"}
    assert results.count(receipt) == (2 if same_turn else 1)
    assert results.count(None) == (0 if same_turn else 1)
    assert store.state(session_id="a", owner=OWNER, now=NOW)["sequence"] == 1
    assert table.get_item(Key={"PK": "session#a", "SK": "mailbox#00000002"}).get("Item") is None


def test_lost_acceptance_response_replays_durable_receipt_after_restart(mailbox, monkeypatch):
    store, table = mailbox
    original = table.meta.client.transact_write_items

    def transact(**request):
        original(**request)
        raise EndpointConnectionError(endpoint_url="https://storage.example.test")

    monkeypatch.setattr(table.meta.client, "transact_write_items", transact)
    with pytest.raises(ChatAuthorizationUnavailableError):
        store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW)
    before = table.scan(ConsistentRead=True)["Items"]
    monkeypatch.setattr(table.meta.client, "transact_write_items", original)
    restarted = ChatSessionMailbox(table)
    assert restarted.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW + 1) == {"sequence": 1, "mode": "ephemeral"}
    assert table.scan(ConsistentRead=True)["Items"] == before


@pytest.mark.parametrize(
    "record,changes",
    [
        ("header", None),
        ("header", {"sessionTurnSequence": 0}),
        ("mailbox-id#a1", {"sequence": True}),
        ("mailbox-id#a1", {"sequence": "1"}),
        ("mailbox-id#a1", {"mode": "unknown"}),
        ("mailbox-id#a1", {"ttl": NOW}),
        ("mailbox#00000001", None),
        ("mailbox#00000001", {"ownerUserId": "bob"}),
        ("mailbox#00000001", {"turnId": "different"}),
        ("mailbox#00000001", {"message": "different"}),
        ("mailbox#00000001", {"mode": "persistent"}),
        ("mailbox#00000001", {"ttl": NOW}),
    ],
)
def test_duplicate_acceptance_requires_intact_durable_turn(mailbox, record, changes):
    store, table = mailbox
    store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW)
    key = {"PK": "session#a", "SK": record}
    if changes is None:
        table.delete_item(Key=key)
    else:
        table.put_item(Item={**table.get_item(Key=key)["Item"], **changes})
    before = table.scan(ConsistentRead=True)["Items"]
    with pytest.raises(ChatAuthorizationRefusedError):
        store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW)
    assert table.scan(ConsistentRead=True)["Items"] == before


def test_cross_owner_read_mode_and_write_fail_closed(mailbox):
    store, _ = mailbox
    store.select_mode(session_id="a", owner=OWNER, mode="persistent", now=NOW)
    other = ("tenant", "team", "bob")
    for operation in (
        lambda: store.state(session_id="a", owner=other, now=NOW),
        lambda: store.select_mode(session_id="a", owner=other, mode="ephemeral", now=NOW),
        lambda: store.accept(session_id="a", owner=other, turn=turn("a1"), now=NOW),
    ):
        with pytest.raises(ChatAuthorizationRefusedError):
            operation()
    assert store.state(session_id="a", owner=OWNER, now=NOW)["mode"] == "persistent"


def test_active_lease_blocks_mode_change_until_sandbox_ends(mailbox):
    store, table = mailbox
    store.select_mode(session_id="a", owner=OWNER, mode="persistent", now=NOW)
    table.update_item(
        Key={"PK": "session#a", "SK": "header"},
        UpdateExpression="SET chatLease = :lease",
        ExpressionAttributeValues={":lease": {"expires_at": NOW + 90}},
    )
    with pytest.raises(ChatAuthorizationRefusedError, match="must end"):
        store.select_mode(session_id="a", owner=OWNER, mode="ephemeral", now=NOW)
    assert store.select_mode(session_id="a", owner=OWNER, mode="persistent", now=NOW) == "persistent"


def test_mode_switch_waits_for_bound_turn_and_verified_cleanup(mailbox):
    store, table = mailbox
    store.select_mode(session_id="a", owner=OWNER, mode="ephemeral", now=NOW)
    store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW)
    lease = {"run_id": "a1", "sandbox_uid": "pod-a", "generation": 1, "expires_at": NOW + 90}
    table.update_item(Key={"PK": "session#a", "SK": "header"}, UpdateExpression="SET chatLease = :lease", ExpressionAttributeValues={":lease": lease})
    assert store.select_mode(session_id="a", owner=OWNER, mode="persistent", now=NOW) == "ephemeral"
    assert store.state(session_id="a", owner=OWNER, now=NOW)["pending_mode"] == "persistent"
    with pytest.raises(ChatAuthorizationRefusedError, match="pending"):
        store.accept(session_id="a", owner=OWNER, turn=turn("a2"), now=NOW)
    assert store.state(session_id="a", owner=OWNER, now=NOW)["sequence"] == 1
    with pytest.raises(ChatAuthorizationRefusedError):
        store.finish_pending_mode(session_id="a", owner=("tenant", "team", "other"), lease=lease, now=NOW)
    store.finish_pending_mode(session_id="a", owner=OWNER, lease=lease, now=NOW)
    assert store.state(session_id="a", owner=OWNER, now=NOW) == {"mode": "persistent", "sequence": 1, "health": "idle"}
    with pytest.raises(ChatAuthorizationRefusedError):
        store.finish_pending_mode(session_id="a", owner=OWNER, lease=lease, now=NOW)


def test_delayed_cleanup_reports_elapsed_time_without_ever_claiming_removal(mailbox):
    store, table = mailbox
    store.select_mode(session_id="a", owner=OWNER, mode="persistent", now=NOW)
    table.update_item(
        Key={"PK": "session#a", "SK": "header"},
        UpdateExpression="SET sessionState = :ending, sessionEndReason = :reason, sessionCleanupStartedAt = :started",
        ExpressionAttributeValues={":ending": "ending", ":reason": "user", ":started": NOW},
    )
    assert store.state(session_id="a", owner=OWNER, now=NOW + 119)["health"] == "ending"
    delayed = store.state(session_id="a", owner=OWNER, now=NOW + 120)
    assert delayed["health"] == "cleanup_delayed"
    assert delayed["cleanup_elapsed_seconds"] == 120
    assert delayed["mode"] == "persistent"


def test_ending_session_refuses_new_turns_until_cleanup_confirms_mode_change(mailbox):
    store, table = mailbox
    store.select_mode(session_id="a", owner=OWNER, mode="persistent", now=NOW)
    original = store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW)
    lease = {"run_id": "a1", "sandbox_uid": "pod-a", "generation": 1, "expires_at": 1}
    table.update_item(
        Key={"PK": "session#a", "SK": "header"},
        UpdateExpression="SET chatLease = :lease, sessionState = :ending",
        ExpressionAttributeValues={":lease": lease, ":ending": "ending"},
    )
    assert store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW) == original
    with pytest.raises(ChatAuthorizationRefusedError, match="ended"):
        store.accept(session_id="a", owner=OWNER, turn=turn("a2"), now=NOW)
    table.update_item(
        Key={"PK": "session#a", "SK": "header"}, UpdateExpression="SET sessionState = :ended", ExpressionAttributeValues={":ended": "ended"}
    )
    with pytest.raises(ChatAuthorizationRefusedError, match="cleanup"):
        store.select_mode(session_id="a", owner=OWNER, mode="ephemeral", now=NOW)
    table.update_item(
        Key={"PK": "session#a", "SK": "header"}, UpdateExpression="SET sessionCleanupFinishedAt = :now", ExpressionAttributeValues={":now": NOW}
    )
    assert store.select_mode(session_id="a", owner=OWNER, mode="ephemeral", now=NOW) == "ephemeral"
    assert store.state(session_id="a", owner=OWNER, now=NOW) == {"mode": "ephemeral", "sequence": 1, "health": "idle"}


def test_session_bound_pod_reads_only_its_next_accepted_turn(mailbox):
    store, table = mailbox
    for session_id in ("a", "b"):
        store.select_mode(session_id=session_id, owner=OWNER, mode="persistent", now=NOW)
        table.update_item(
            Key={"PK": f"session#{session_id}", "SK": "header"},
            UpdateExpression="SET chatLease = :lease",
            ExpressionAttributeValues={
                ":lease": {"run_id": f"run-{session_id}", "sandbox_uid": f"pod-{session_id}", "generation": 1, "expires_at": NOW + 90}
            },
        )
    store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW)
    store.accept(session_id="b", owner=OWNER, turn=turn("b1"), now=NOW)
    store.accept(session_id="a", owner=OWNER, turn=turn("a2"), now=NOW)
    scope = {"session_id": "a", "owner": OWNER, "run_id": "run-a", "sandbox_uid": "pod-a", "generation": 1, "now": NOW}
    assert store.next_turn(after=0, **scope) == {"sequence": 1, "turn_id": "a1", "message": "Hello"}
    with pytest.raises(ChatAuthorizationRefusedError, match="not committed"):
        store.next_turn(after=1, **scope)
    table.update_item(
        Key={"PK": "session#a", "SK": "mailbox#00000001"},
        UpdateExpression="SET #status = :completed",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":completed": "completed"},
    )
    assert store.next_turn(after=1, **scope) == {"sequence": 2, "turn_id": "a2", "message": "Hello"}
    with pytest.raises(ChatAuthorizationRefusedError, match="not committed"):
        store.next_turn(after=2, **scope)
    table.update_item(
        Key={"PK": "session#a", "SK": "mailbox#00000002"},
        UpdateExpression="SET #status = :completed",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":completed": "failed"},
    )
    assert store.next_turn(after=2, **scope) is None
    with pytest.raises(ChatAuthorizationRefusedError, match="ahead"):
        store.next_turn(after=3, **scope)
    for changes in ({"owner": ("tenant", "team", "bob")}, {"run_id": "run-b"}, {"sandbox_uid": "pod-b"}, {"generation": 2}, {"now": NOW + 90}):
        with pytest.raises(ChatAuthorizationRefusedError):
            store.next_turn(after=0, **{**scope, **changes})
    with pytest.raises(ChatAuthorizationRefusedError):
        store.next_turn(after=0, **{**scope, "session_id": "b"})


def test_fence_rechecked_after_mailbox_read(mailbox, monkeypatch):
    store, table = mailbox
    store.select_mode(session_id="a", owner=OWNER, mode="persistent", now=NOW)
    table.update_item(
        Key={"PK": "session#a", "SK": "header"},
        UpdateExpression="SET chatLease = :lease",
        ExpressionAttributeValues={":lease": {"run_id": "run-a", "sandbox_uid": "pod-a", "generation": 1, "expires_at": NOW + 90}},
    )
    store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW)
    original = table.get_item

    def replace_after_read(**kwargs):
        result = original(**kwargs)
        if kwargs["Key"]["SK"].startswith("mailbox#"):
            table.update_item(
                Key={"PK": "session#a", "SK": "header"},
                UpdateExpression="SET chatLease.generation = :generation",
                ExpressionAttributeValues={":generation": 2},
            )
        return result

    monkeypatch.setattr(table, "get_item", replace_after_read)
    with pytest.raises(ChatAuthorizationRefusedError):
        store.next_turn(session_id="a", owner=OWNER, run_id="run-a", sandbox_uid="pod-a", generation=1, after=0, now=NOW)


def test_expired_session_cannot_accept_or_switch_mode(mailbox):
    store, _ = mailbox
    store.accept(session_id="a", owner=OWNER, turn=turn("a1"), now=NOW)
    with pytest.raises(ChatAuthorizationRefusedError):
        store.accept(session_id="a", owner=OWNER, turn=turn("a2"), now=NOW + 90 * 24 * 60 * 60)
    with pytest.raises(ChatAuthorizationRefusedError):
        store.select_mode(session_id="a", owner=OWNER, mode="persistent", now=NOW + 90 * 24 * 60 * 60)
