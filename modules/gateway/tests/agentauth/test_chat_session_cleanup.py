"""Durable owner-scoped session cleanup wakeups and lease fencing."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatLaunchStore
from src.agentauth.chat_session_cleanup import publish_session_cleanup, request_session_end
from src.agentauth.chat_session_mailbox import AcceptedTurn, ChatSessionMailbox
from tests.agentauth import test_chat_notifications as notifications

mailbox = notifications.mailbox
runtime = notifications.runtime
client = notifications.client
sts = notifications.sts
store = notifications.store
retained_input_table = notifications.retained_input_table
transport = notifications.transport
wakeup = notifications.wakeup
registered_owner = notifications.registered_owner

OWNER = ("tenant", "team", "human")


def marker(runtime):
    return runtime[2].get_item(Key={"PK": "chat-notifications", "SK": "cleanup#run-user"}, ConsistentRead=True)["Item"]


def service(authority, *, member=True):
    def check(*_):
        if not member:
            raise ChatAuthorizationRefusedError("membership revoked")

    return SimpleNamespace(launches=ChatLaunchStore(authority.store), _member=check)


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_explicit_end_fences_pod_and_recovers_lost_cleanup_wakeup(mailbox, wakeup, sts, monkeypatch):
    client, runtime, token, _, _ = mailbox
    authority, now = runtime[1], runtime[-1]
    original = marker(runtime)
    assert publish_session_cleanup(authority, service(authority), original, now=now, transport=wakeup) == "active"
    with pytest.raises(ChatAuthorizationRefusedError):
        request_session_end(runtime[2], session_id="session-a", owner=("tenant", "team", "intruder"), now=now, reason="user")
    ended = request_session_end(runtime[2], session_id="session-a", owner=OWNER, now=now, reason="user")
    assert ended["chatLease"]["expires_at"] == 1
    assert request_session_end(runtime[2], session_id="session-a", owner=OWNER, now=now, reason="user") == ended
    assert not authority.current(service(authority).launches.load("run-user"), now)
    assert publish_session_cleanup(authority, service(authority), original, now=now, transport=wakeup) == "notified"
    assert notifications.receive(wakeup)[0]["message_id"] == "run-user"
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]["sessionState"] == "recovering"
    assert authority.store._read("TENANT#tenant", "EXEC#run-user")["chat_session_lost"]
    assert publish_session_cleanup(authority, service(authority), marker(runtime), now=now + 1, transport=wakeup) == "waiting"
    assert marker(runtime)["dispatchCount"] == 1
    denied = await client.post(
        "/v1/chat/data/turn/result",
        json={"run_id": "run-user", "session_id": "session-a", "outcome": "failed"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert denied.status_code == 404


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_cleanup_snapshot_cannot_fence_a_followup_lease(mailbox, wakeup, monkeypatch):
    _, runtime, _, _, _ = mailbox
    authority, now, table = runtime[1], runtime[-1], runtime[2]
    original = marker(runtime)
    key = {"PK": "session#session-a", "SK": "header"}
    replacement = {"run_id": "run-followup", "sandbox_uid": "chat-pod", "generation": 2, "expires_at": now + 60}

    def advance_before_authority_check(*_):
        table.update_item(Key=key, UpdateExpression="SET chatLease = :lease", ExpressionAttributeValues={":lease": replacement})
        return False

    monkeypatch.setattr(authority, "current", advance_before_authority_check)
    with pytest.raises(ChatAuthorizationRefusedError, match="chat session lease changed"):
        publish_session_cleanup(authority, service(authority), original, now=now, transport=wakeup)
    header = table.get_item(Key=key, ConsistentRead=True)["Item"]
    assert header["chatLease"] == replacement
    assert "sessionEndReason" not in header and "sessionCleanupStartedAt" not in header
    assert marker(runtime) == original
    assert notifications.receive(wakeup) == []


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("race_window", ["authority_check", "fence_write"])
@pytest.mark.parametrize("activity_update", ["accepted_turn", "same_timestamp_turn", "activity_only"])
async def test_idle_cleanup_cannot_fence_new_activity(mailbox, wakeup, monkeypatch, race_window, activity_update):
    _, runtime, _, _, _ = mailbox
    authority, now, table = runtime[1], runtime[-1], runtime[2]
    original = marker(runtime)
    key = {"PK": "session#session-a", "SK": "header"}
    table.update_item(Key=key, UpdateExpression="SET chatLease.expires_at = :future", ExpressionAttributeValues={":future": now + 500})
    table.update_item(
        Key={"PK": key["PK"], "SK": "mailbox#00000001"},
        UpdateExpression="SET #status = :complete",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":complete": "completed"},
    )
    previous = table.get_item(Key=key, ConsistentRead=True)["Item"]
    execution = authority.store._read("TENANT#tenant", "EXEC#run-user")
    original_current = authority.current
    original_update = table.update_item
    changed_headers = []

    def accept_activity():
        if changed_headers:
            return
        if activity_update == "activity_only":
            original_update(
                Key=key,
                UpdateExpression="SET lastActivityAt = :activity",
                ExpressionAttributeValues={":activity": datetime.fromtimestamp(now + 301, UTC).isoformat()},
            )
        else:
            accepted = ChatSessionMailbox(table).accept(
                session_id="session-a",
                owner=OWNER,
                turn=AcceptedTurn(turn_id="run-followup", message="Continue the same session"),
                now=now if activity_update == "same_timestamp_turn" else now + 301,
            )
            assert accepted == {"sequence": 2, "mode": "persistent"}
        changed_headers.append(table.get_item(Key=key, ConsistentRead=True)["Item"])

    def advance_before_authority_check(*args):
        accept_activity()
        return original_current(*args)

    def advance_before_fence(**kwargs):
        if kwargs["Key"] == key and ":fenced" in kwargs.get("ExpressionAttributeValues", {}):
            accept_activity()
        return original_update(**kwargs)

    if race_window == "authority_check":
        monkeypatch.setattr(authority, "current", advance_before_authority_check)
    else:
        monkeypatch.setattr(table, "update_item", advance_before_fence)
    with pytest.raises(ChatAuthorizationRefusedError, match="chat session end changed"):
        publish_session_cleanup(authority, service(authority), original, now=now + 301, transport=wakeup)
    assert len(changed_headers) == 1
    header = table.get_item(Key=key, ConsistentRead=True)["Item"]
    assert header == changed_headers[0]
    assert header["chatLease"] == previous["chatLease"]
    assert authority.store._read("TENANT#tenant", "EXEC#run-user") == execution
    assert marker(runtime) == original
    assert notifications.receive(wakeup) == []
    if activity_update != "activity_only":
        assert table.get_item(Key={"PK": key["PK"], "SK": "mailbox#00000002"}, ConsistentRead=True)["Item"]["status"] == "queued"
    assert publish_session_cleanup(authority, service(authority), original, now=now + 301, transport=wakeup) == "active"
    assert marker(runtime) == original
    assert notifications.receive(wakeup) == []


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
@pytest.mark.parametrize("revocation", ["lease_expiry", "membership"])
async def test_new_turn_cannot_delay_authority_cleanup(mailbox, wakeup, monkeypatch, revocation):
    _, runtime, _, _, _ = mailbox
    authority, now, table = runtime[1], runtime[-1], runtime[2]
    key = {"PK": "session#session-a", "SK": "header"}
    table.update_item(
        Key=key,
        UpdateExpression="SET chatLease.expires_at = :expiry",
        ExpressionAttributeValues={":expiry": now + (300 if revocation == "lease_expiry" else 500)},
    )
    original_current = authority.current

    def accept_before_authority_check(*args):
        assert ChatSessionMailbox(table).accept(
            session_id="session-a",
            owner=OWNER,
            turn=AcceptedTurn(turn_id="run-followup", message="Continue the same session"),
            now=now + 301,
        ) == {"sequence": 2, "mode": "persistent"}
        return original_current(*args)

    monkeypatch.setattr(authority, "current", accept_before_authority_check)
    assert (
        publish_session_cleanup(authority, service(authority, member=revocation != "membership"), marker(runtime), now=now + 301, transport=wakeup)
        == "notified"
    )
    header = table.get_item(Key=key, ConsistentRead=True)["Item"]
    assert header["chatLease"]["expires_at"] == 1
    assert header["sessionState"] == "recovering"
    assert header["sessionEndReason"] == "authority"
    assert notifications.receive(wakeup)[0]["message_id"] == "run-user"


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_idle_and_revoked_membership_reconcile_without_new_turn(mailbox, wakeup):
    _, runtime, _, _, _ = mailbox
    authority, now = runtime[1], runtime[-1]
    table = runtime[2]
    item = marker(runtime)
    table.update_item(
        Key={"PK": "session#session-a", "SK": "header"},
        UpdateExpression="SET chatLease.expires_at = :future",
        ExpressionAttributeValues={":future": now + 500},
    )
    table.update_item(
        Key={"PK": "session#session-a", "SK": "mailbox#00000001"},
        UpdateExpression="SET #status = :complete",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":complete": "completed"},
    )
    assert publish_session_cleanup(authority, service(authority), item, now=now + 299, transport=wakeup) == "active"
    assert publish_session_cleanup(authority, service(authority), item, now=now + 301, transport=wakeup) == "notified"
    assert table.get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]["sessionEndReason"] == "idle"


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_revocation_and_failed_dispatch_preserve_cleanup_marker(mailbox, wakeup):
    _, runtime, _, _, _ = mailbox
    authority, now = runtime[1], runtime[-1]
    original = marker(runtime)
    client, queue = wakeup()

    class Offline:
        def send_message(self, **_):
            raise EndpointConnectionError(endpoint_url="https://queue.example.test")

    with pytest.raises(EndpointConnectionError):
        publish_session_cleanup(authority, service(authority, member=False), original, now=now, transport=lambda: (Offline(), queue))
    assert "publishedAt" not in marker(runtime)
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]["sessionEndReason"] == "authority"
    assert publish_session_cleanup(authority, service(authority, member=False), marker(runtime), now=now + 1, transport=wakeup) == "notified"
    assert client.receive_message(QueueUrl=queue).get("Messages")
    with pytest.raises(ChatAuthorizationRefusedError):
        publish_session_cleanup(authority, service(authority), {**original, "sandboxUid": "other-pod"}, now=now + 2, transport=wakeup)


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_confirmed_removal_marks_session_ended_and_retires_wakeup(mailbox, wakeup, monkeypatch):
    _, runtime, _, _, _ = mailbox
    authority, now = runtime[1], runtime[-1]
    request_session_end(runtime[2], session_id="session-a", owner=OWNER, now=now, reason="user")
    assert publish_session_cleanup(authority, service(authority), marker(runtime), now=now, transport=wakeup) == "notified"
    original = authority.store._read

    def committed(partition, key):
        if (partition, key) == ("CHAT-LAUNCH#run-user", "TEARDOWN"):
            return {"removed_at": {"N": str(now + 1)}}
        if (partition, key) == ("CHAT-DELIVERY#run-user", "TERMINAL"):
            return {"completion_receipt": {"M": {}}}
        return original(partition, key)

    monkeypatch.setattr(authority.store, "_read", committed)
    assert publish_session_cleanup(authority, service(authority), marker(runtime), now=now + 1, transport=wakeup) == "removed"
    header = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"}, ConsistentRead=True)["Item"]
    assert header["sessionState"] == "ended"
    assert header["sessionCleanupFinishedAt"] == now + 1
    assert runtime[2].get_item(Key={"PK": "chat-notifications", "SK": "cleanup#run-user"}).get("Item") is None


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_switch_to_ephemeral_waits_for_committed_turn_and_removed_sandbox(mailbox, wakeup, monkeypatch):
    _, runtime, _, _, _ = mailbox
    authority, now, table = runtime[1], runtime[-1], runtime[2]
    original = marker(runtime)
    mailbox_store = ChatSessionMailbox(table)
    assert mailbox_store.select_mode(session_id="session-a", owner=OWNER, mode="ephemeral", now=now) == "persistent"
    assert publish_session_cleanup(authority, service(authority), original, now=now, transport=wakeup) == "active"
    table.update_item(
        Key={"PK": "session#session-a", "SK": "mailbox#00000001"},
        UpdateExpression="SET #status = :complete",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":complete": "completed"},
    )
    assert publish_session_cleanup(authority, service(authority), original, now=now, transport=wakeup) == "notified"
    assert mailbox_store.state(session_id="session-a", owner=OWNER, now=now)["pending_mode"] == "ephemeral"
    original_read = authority.store._read

    def committed(partition, key):
        if (partition, key) == ("CHAT-LAUNCH#run-user", "TEARDOWN"):
            return {"removed_at": {"N": str(now + 1)}}
        if (partition, key) == ("CHAT-DELIVERY#run-user", "TERMINAL"):
            return {"completion_receipt": {"M": {}}}
        return original_read(partition, key)

    monkeypatch.setattr(authority.store, "_read", committed)
    assert publish_session_cleanup(authority, service(authority), marker(runtime), now=now + 1, transport=wakeup) == "removed"
    assert mailbox_store.state(session_id="session-a", owner=OWNER, now=now + 1) == {"mode": "ephemeral", "sequence": 1, "health": "idle"}


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_notification_scan_replays_cleanup_after_end_without_another_turn(mailbox, transport, wakeup):
    _, runtime, _, _, _ = mailbox
    authority, now = runtime[1], runtime[-1]
    request_session_end(runtime[2], session_id="session-a", owner=OWNER, now=now, reason="user")
    notifications.recover_notification_page(authority, transport.sessions, now=now, capabilities=service(authority), transport=wakeup)
    assert marker(runtime)["publishedAt"] == now
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]["sessionState"] == "recovering"


@pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)
async def test_expired_root_cleans_original_pod_without_minting_new_authority(mailbox, wakeup):
    _, runtime, _, _, _ = mailbox
    authority = runtime[1]
    launch = service(authority).launches.load("run-user")
    expiry = launch.expires_at + 1
    assert publish_session_cleanup(authority, service(authority), marker(runtime), now=expiry, transport=wakeup) == "notified"
    header = runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"}, ConsistentRead=True)["Item"]
    assert header["chatLease"]["expires_at"] == 1
    assert header["sessionEndReason"] == "authority"
    assert not authority.current(launch, expiry)
