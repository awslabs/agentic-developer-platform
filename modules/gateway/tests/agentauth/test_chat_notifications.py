"""Storage-to-FIFO recovery with protected routing and real emulator transactions."""

import json
from datetime import UTC, datetime

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.chat_notifications import maintain_chat_notifications, publish_notification, recover_notification_page
from src.agentauth.chat_session_mailbox import AcceptedTurn, ChatSessionMailbox
from src.agentauth.external_roots import provision_root
from src.agentauth.model_policy import canonical_json
from tests.agentauth import test_chat_pending_handoff as handoff
from tests.agentauth import test_chat_persistent_completion as fixtures
from tests.agentauth.test_chat_sandbox_exit import supervisor
from tests.agentauth.test_chat_session_recovery import call
from tests.agentauth.test_work_producer import proof

client = fixtures.client
runtime = fixtures.runtime
store = fixtures.store
sts = fixtures.sts
retained_input_table = fixtures.retained_input_table
mailbox = fixtures.mailbox
transport = fixtures.transport
consumer = fixtures.consumer
registered_owner = fixtures.registered_owner
ingest = handoff.ingest
pytestmark = pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)


@pytest.fixture
def wakeup(mailbox):
    client = boto3.client("sqs", region_name="us-east-1")
    queue = client.create_queue(QueueName="chat-wakeups.fifo", Attributes={"FifoQueue": "true"})["QueueUrl"]
    return lambda: (client, queue)


def notification(mailbox):
    return mailbox[1][2].get_item(Key={"PK": "chat-notifications", "SK": "turn#run-user"}, ConsistentRead=True)["Item"]


def receive(wakeup):
    client, queue = wakeup()
    messages = client.receive_message(QueueUrl=queue).get("Messages", [])
    for message in messages:
        client.delete_message(QueueUrl=queue, ReceiptHandle=message["ReceiptHandle"])
    return [json.loads(message["Body"]) for message in messages]


async def test_lost_notification_and_supervisor_restart_recover_same_registered_binding(mailbox, transport, wakeup, sts, monkeypatch):
    _, runtime, _, state, _ = mailbox
    authority, now = runtime[1], runtime[-1]
    before = authority.store._read("CHAT-LAUNCH#run-user", "LAUNCH")
    assert recover_notification_page(authority, transport.sessions, now=now, transport=wakeup) is None
    notice = receive(wakeup)[0]
    assert notice == {
        "notification_version": 1,
        "message_id": "run-user",
        "session_id": "session-a",
        "task_id": "task-a",
        "session_generation": 1700000000,
        "session_mode": "persistent",
        "envelope_digest": authority.store._read("INVOCATION#run-user", "DISPATCH")["envelope_digest"]["S"],
    }
    assert publish_notification(authority, transport.sessions, notification(mailbox), now=now + 10, transport=wakeup) == "waiting"
    assert publish_notification(authority, transport.sessions, notification(mailbox), now=now + 60, transport=wakeup) == "published"
    assert receive(wakeup) == [notice]
    supervisor(sts, monkeypatch)
    recovered = await call(mailbox, "resume")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["sandbox_uid"] == state["pod"].uid
    assert authority.store._read("CHAT-LAUNCH#run-user", "LAUNCH") == before


async def test_initial_accepted_turn_is_discovered_before_any_pod_launch(mailbox, transport, wakeup, sts, monkeypatch):
    client, runtime, _, _, _ = mailbox
    authority, now = runtime[1], runtime[-1]
    owner = ("tenant", "team", "human")
    ledger = ChatSessionMailbox(runtime[2])
    ledger.select_mode(session_id="session-b", owner=owner, mode="persistent", now=now)
    envelope = fixtures.fixtures.prepare(
        runtime, session_id="session-b", message_id="run-new", task_id="task-b", source_ref={"repo": "chat/session-b"}
    )
    ledger.accept(session_id="session-b", owner=owner, turn=AcceptedTurn(turn_id="run-new", message=envelope["message"]), now=now)
    transport.sessions.put_item(Item={**transport.row, "session_id": "session-b", "threads": {"thread-a": {"processing_task_id": "task-b"}}})
    recover_notification_page(authority, transport.sessions, now=now, transport=wakeup)
    notices = receive(wakeup) + receive(wakeup)
    notice = next(item for item in notices if item["message_id"] == "run-new")
    assert authority.store._read("CHAT-LAUNCH#run-new", "LAUNCH") is None
    supervisor(sts, monkeypatch)
    request = {"run_id": notice["message_id"], "envelope_digest": notice["envelope_digest"]}
    response = await client.post(
        "/internal/v1/agent/chat/data/resume", json=request, headers={"X-Adp-Producer-Proof": proof(envelope_digest(request))}
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "unstarted"
    assert response.json()["task_id"] == "task-b"
    assert ledger.state(session_id="session-b", owner=owner, now=now)["sequence"] == 1


@pytest.mark.parametrize("lost_response", [False, True])
async def test_queue_outage_and_lost_publish_response_keep_recovery_intent(mailbox, transport, wakeup, lost_response):
    authority, now = mailbox[1][1], mailbox[1][-1]
    item = notification(mailbox)
    client, queue = wakeup()

    class Outage:
        def send_message(self, **kwargs):
            if lost_response:
                client.send_message(**kwargs)
            raise EndpointConnectionError(endpoint_url="https://queue.example.test")

    recover_notification_page(authority, transport.sessions, now=now, transport=lambda: (Outage(), queue))
    assert notification(mailbox) == item
    assert publish_notification(authority, transport.sessions, item, now=now, transport=wakeup) == "published"
    assert len(receive(wakeup)) == 1


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("notification", "ownerUserId", "intruder"),
        ("notification", "sessionId", "session-b"),
        ("notification", "sequence", 2),
        ("header", "ownerUserId", "intruder"),
        ("mailbox#00000001", "message", "substituted"),
        ("mailbox-id#run-user", "sequence", 2),
        ("session", "created_at", 123),
        ("session", "owner_principal", "intruder"),
    ],
)
async def test_mutated_discovery_or_owner_cannot_publish(mailbox, transport, wakeup, target, field, value):
    authority, now = mailbox[1][1], mailbox[1][-1]
    if target == "session":
        table, key = transport.sessions, {"session_id": "session-a"}
    else:
        table = authority.context_table
        key = {"PK": "chat-notifications", "SK": "turn#run-user"} if target == "notification" else {"PK": "session#session-a", "SK": target}
    table.update_item(
        Key=key, UpdateExpression="SET #field = :value", ExpressionAttributeNames={"#field": field}, ExpressionAttributeValues={":value": value}
    )
    with pytest.raises(ChatAuthorizationRefusedError):
        publish_notification(authority, transport.sessions, notification(mailbox), now=now, transport=wakeup)
    assert receive(wakeup) == []


async def test_future_turn_waits_for_processing_lock_promotion(mailbox, transport, wakeup):
    authority, now = mailbox[1][1], mailbox[1][-1]
    transport.sessions.update_item(
        Key={"session_id": "session-a"},
        UpdateExpression="SET threads.#thread.processing_task_id = :task",
        ExpressionAttributeNames={"#thread": "thread-a"},
        ExpressionAttributeValues={":task": "predecessor"},
    )
    with pytest.raises(ChatAuthorizationRefusedError):
        publish_notification(authority, transport.sessions, notification(mailbox), now=now, transport=wakeup)
    assert receive(wakeup) == []
    transport.sessions.update_item(
        Key={"session_id": "session-a"},
        UpdateExpression="SET threads.#thread.processing_task_id = :task",
        ExpressionAttributeNames={"#thread": "thread-a"},
        ExpressionAttributeValues={":task": "task-a"},
    )
    assert publish_notification(authority, transport.sessions, notification(mailbox), now=now, transport=wakeup) == "published"


async def test_completed_delivery_retires_intent_without_republishing_or_replaying(mailbox, transport, wakeup, consumer, sts, monkeypatch):
    client, runtime, token, state, _ = mailbox
    supervisor(sts, monkeypatch)
    await fixtures.fixtures.next_turn(client, token)
    result = await client.post(
        "/v1/chat/data/turn/result",
        json={"run_id": "run-user", "session_id": "session-a", "outcome": "failed"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert result.status_code == 200, result.text
    assert (await fixtures.commit(client, runtime, state)).status_code == 409
    consumer._process_response(fixtures.publication.payload(transport))
    completed = await fixtures.commit(client, runtime, state)
    assert completed.status_code == 200, completed.text
    assert publish_notification(runtime[1], transport.sessions, notification(mailbox), now=runtime[-1], transport=wakeup) == "completed"
    assert receive(wakeup) == []
    assert not runtime[2].get_item(Key={"PK": "chat-notifications", "SK": "turn#run-user"}).get("Item")


@pytest.mark.parametrize("lost_registration_response", [False, True])
async def test_persistent_pending_handoff_recovers_publication_after_lock_promotion(
    mailbox, transport, wakeup, consumer, sts, monkeypatch, ingest, lost_registration_response
):
    from unittest.mock import Mock

    from src.agentauth import chat_pending_handoff

    client, runtime, token, state, _ = mailbox
    supervisor(sts, monkeypatch)
    request = {
        **fixtures.ROUTING,
        "message_id": "run-next",
        "session_id": "session-a",
        "task_id": "task-next",
        "tenant_id": "tenant",
        "agent_type": "developer",
        "message": "Follow-up input",
        "mode": "chat",
        "arrived_at": datetime.fromtimestamp(runtime[-1], UTC).isoformat(),
    }

    def register(envelope):
        final = {
            **envelope,
            "persona": "developer",
            "session_mode": "persistent",
            "source_ref": {"repo": "chat/session-a"},
            "correlation": {"correlation_id": "run-next", "root_human_id": "human", "is_human_rooted": True},
        }
        provision_root(runtime[1].store, final, source="chat", human_id="human", now=datetime.fromtimestamp(runtime[-1], UTC))
        ChatSessionMailbox(runtime[2]).accept(
            session_id="session-a",
            owner=("tenant", "team", "human"),
            turn=AcceptedTurn(turn_id="run-next", message=envelope["message"]),
            now=runtime[-1],
        )
        if lost_registration_response:
            raise TimeoutError("registered response lost")
        return canonical_json(final).decode()

    try:
        ingest.buffer_pending_turn(transport.sessions, request, processing_task="task-a", now=runtime[-1], register=register)
    except TimeoutError:
        assert lost_registration_response
    await fixtures.fixtures.next_turn(client, token)
    result = await client.post(
        "/v1/chat/data/turn/result",
        json={"run_id": "run-user", "session_id": "session-a", "outcome": "failed"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert result.status_code == 200, result.text
    assert (await fixtures.commit(client, runtime, state)).status_code == 409
    consumer._process_response(fixtures.publication.payload(transport))
    outage = Mock()
    outage.send_message.side_effect = EndpointConnectionError(endpoint_url="https://queue.example.test")
    monkeypatch.setattr(chat_pending_handoff, "input_transport", lambda: (outage, wakeup()[1]))
    completion = await fixtures.commit(client, runtime, state)
    assert completion.status_code == 503, completion.text
    row = transport.sessions.get_item(Key={"session_id": "session-a"})["Item"]
    assert row["threads"]["thread-a"]["processing_task_id"] == "task-next"
    assert publish_notification(runtime[1], transport.sessions, notification(mailbox), now=runtime[-1], transport=wakeup) == "published"
    assert receive(wakeup)[0]["message_id"] == "run-user"
    monkeypatch.setattr(chat_pending_handoff, "input_transport", wakeup)
    completed = await fixtures.commit(client, runtime, state)
    assert completed.status_code == 200, completed.text
    published = receive(wakeup)[0]
    assert published["message_id"] == "run-next" and published["session_mode"] == "persistent"
    assert runtime[1].store._read("INVOCATION#run-next", "DISPATCH")["envelope_digest"] == {"S": envelope_digest(published)}
    assert ChatSessionMailbox(runtime[2]).state(session_id="session-a", owner=("tenant", "team", "human"), now=runtime[-1])["sequence"] == 2


async def test_recovery_continues_past_poison_marker_and_pages_bounded_partition(mailbox, transport, wakeup):
    authority, now = mailbox[1][1], mailbox[1][-1]
    for number in range(101):
        authority.context_table.put_item(Item={"PK": "chat-notifications", "SK": f"bad-{number:03d}"})
    cursor = recover_notification_page(authority, transport.sessions, now=now, transport=wakeup)
    assert cursor is not None and receive(wakeup) == []
    assert recover_notification_page(authority, transport.sessions, now=now, cursor=cursor, transport=wakeup) is None
    assert len(receive(wakeup)) == 1


async def test_mode_switch_cannot_accept_without_required_recovery_intent(mailbox):
    table, now = mailbox[1][2], mailbox[1][-1]
    table.update_item(
        Key={"PK": "session#session-a", "SK": "header"}, UpdateExpression="SET sessionMode = :mode", ExpressionAttributeValues={":mode": "ephemeral"}
    )
    with pytest.raises(ChatAuthorizationRefusedError, match="mode changed"):
        ChatSessionMailbox(table).accept(
            session_id="session-a",
            owner=("tenant", "team", "human"),
            turn=AcceptedTurn(turn_id="second", message="Follow up"),
            now=now,
            expected_mode="persistent",
        )
    assert not table.get_item(Key={"PK": "session#session-a", "SK": "mailbox#00000002"}).get("Item")


async def test_notification_is_atomic_with_acceptance_and_retry_does_not_reset_it(mailbox, transport, wakeup, monkeypatch):
    authority, now, envelope = mailbox[1][1], mailbox[1][-1], mailbox[4]
    publish_notification(authority, transport.sessions, notification(mailbox), now=now, transport=wakeup)
    previous = notification(mailbox)
    mailbox_store = ChatSessionMailbox(authority.context_table)
    mailbox_store.accept(
        session_id="session-a", owner=("tenant", "team", "human"), turn=AcceptedTurn(turn_id="run-user", message=envelope["message"]), now=now
    )
    assert notification(mailbox) == previous
    write = authority.context_table.meta.client.transact_write_items

    def reject_notification(**kwargs):
        operations = kwargs["TransactItems"]
        notice = next(operation["Put"] for operation in operations if operation.get("Put", {}).get("Item", {}).get("PK") == "chat-notifications")
        assert notice["Item"]["SK"] == "turn#second"
        notice["ConditionExpression"] = "attribute_exists(PK)"
        return write(**kwargs)

    monkeypatch.setattr(authority.context_table.meta.client, "transact_write_items", reject_notification)
    with pytest.raises(Exception, match="contention"):
        mailbox_store.accept(session_id="session-a", owner=("tenant", "team", "human"), turn=AcceptedTurn(turn_id="second", message="Hello"), now=now)
    assert mailbox_store.state(session_id="session-a", owner=("tenant", "team", "human"), now=now)["sequence"] == 1
    assert not authority.context_table.get_item(Key={"PK": "session#session-a", "SK": "mailbox-id#second"}).get("Item")


async def test_periodic_recovery_is_gated_and_cancellable(mailbox, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from src.agentauth import chat_notifications

    monkeypatch.delenv("ADP_CHAT_DATA_ENABLED", raising=False)
    work = AsyncMock()
    monkeypatch.setattr(chat_notifications, "run_in_threadpool", work)
    monkeypatch.setattr(chat_notifications.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await maintain_chat_notifications()
    work.assert_not_called()


async def test_enabled_lifecycle_resumes_pages_and_restarts_without_memory(mailbox, transport, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from src.agentauth import chat_data_routes, chat_notifications
    from src.orchestration import intake_wiring

    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "true")
    monkeypatch.setattr(chat_data_routes, "runtime", lambda: (mailbox[1][1], None))
    monkeypatch.setattr(intake_wiring, "_get_sessions_table", lambda: transport.sessions)
    marker = {"PK": "chat-notifications", "SK": "page-one"}
    pages = []

    def recover(authority, sessions, *, now, cursor, capabilities):
        assert authority is mailbox[1][1] and sessions is transport.sessions and now > 0
        pages.append(cursor)
        return marker if cursor is None else None

    monkeypatch.setattr(chat_notifications, "recover_notification_page", recover)
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError])
    monkeypatch.setattr(chat_notifications.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await maintain_chat_notifications()
    assert pages == [None, marker]
    assert [call.args for call in sleep.call_args_list] == [(1,), (30,)]
    sleep.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await maintain_chat_notifications()
    assert pages == [None, marker, None]
