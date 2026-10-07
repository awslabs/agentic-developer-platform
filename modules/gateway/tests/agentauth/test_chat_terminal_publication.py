"""Supervisor HTTP, transactional storage, response queue and WebSocket handoff."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import EndpointConnectionError

from tests.agentauth import test_chat_terminal_delivery as fixtures

client = fixtures.client
runtime = fixtures.runtime
store = fixtures.store
sts = fixtures.sts
capability = fixtures.capability
retained_input_table = fixtures.retained_input_table
ready = fixtures.ready
registered_owner = fixtures.registered_owner
transport = fixtures.transport


@pytest.fixture
def consumer(transport, monkeypatch):
    directory = Path(__file__).resolve().parents[4] / "modules/agent-factory/gateway/lambdas/response"
    monkeypatch.syspath_prepend(str(directory))
    for name in list(sys.modules):
        if name == "routers" or name.startswith("routers.") or name == "terminal_delivery":
            monkeypatch.delitem(sys.modules, name)
    spec = importlib.util.spec_from_file_location("terminal_response_handler", directory / "handler.py")
    handler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handler)
    handler.sessions_table = transport.sessions
    handler.ws_router = handler.WebSocketRouter("https://websocket.example.test", transport.sessions)
    handler.ws_router._client = Mock()
    handler.sqs = Mock()
    return handler


def payload(transport):
    return json.loads(transport.client.send_message.call_args.kwargs["MessageBody"])


def session(transport):
    return transport.sessions.get_item(Key={"session_id": "session-a"}, ConsistentRead=True)["Item"]


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "interrupted"])
async def test_finalization_reaches_owner_websocket_once_without_unlock_or_model_replay(client, runtime, ready, transport, consumer, outcome):
    if outcome == "completed":
        await fixtures.save_reply(client, ready)
    elif outcome == "failed":
        assert (await fixtures.results.commit(client, ready)).status_code == 200
    elif outcome == "cancelled":
        fixtures.results.cancel(runtime)
    result = await fixtures.finalization.request(client, runtime)
    assert result.status_code == 200, result.text
    queued = payload(transport)
    sent = transport.client.send_message.call_args.kwargs
    assert sent["QueueUrl"] == transport.url and sent["MessageGroupId"] == "session-a"
    assert sent["MessageDeduplicationId"] == queued["delivery_id"]
    assert queued["status"] == outcome and queued["strict_delivery"] is True
    consumer._process_response(queued)
    consumer._process_response(queued)
    assert (await fixtures.finalization.request(client, runtime)).json() == result.json()
    assert transport.client.send_message.call_count == 1
    assert consumer.ws_router._client.post_to_connection.call_count == 1
    sent = consumer.ws_router._client.post_to_connection.call_args.kwargs
    frame = json.loads(sent["Data"])
    assert sent["ConnectionId"] == "owner-connection"
    assert frame["status"] == outcome and frame["content"] == queued["text"]
    assert frame["terminal_delivery"] is True and frame["session_id"] == "session-a"
    assert frame["delivery_id"] == queued["delivery_id"] and frame["retryable"] == result.json()["retryable"]
    row = session(transport)
    assert len(row["messages"]) == len(row["threads"]["thread-a"]["messages"]) == 1
    assert row["threads"]["thread-a"]["terminal_delivery"]["status"] == "sent"
    assert row["threads"]["thread-a"]["processing_task_id"] == "task-a"
    consumer.sqs.send_message.assert_not_called()


@pytest.mark.parametrize("failure", ["queue", "missing-receipt", "receipt-write", "receipt-response"])
async def test_uncertain_queue_handoff_retries_same_delivery_id(client, runtime, ready, transport, monkeypatch, failure):
    protected = runtime[1].store
    update = protected.client.update_item
    if failure == "queue":
        transport.client.send_message.side_effect = EndpointConnectionError(endpoint_url="https://queue.example.test")
    elif failure == "missing-receipt":
        transport.client.send_message.return_value = {}
    else:

        def unavailable(**kwargs):
            if kwargs.get("Key") == fixtures.KEY:
                if failure == "receipt-response":
                    update(**kwargs)
                raise EndpointConnectionError(endpoint_url="https://storage.example.test")
            return update(**kwargs)

        monkeypatch.setattr(protected.client, "update_item", unavailable)
    assert (await fixtures.finalization.request(client, runtime)).status_code == 503
    original = transport.client.send_message.call_args.kwargs
    assert fixtures.outbox(runtime)["status"] == {"S": "queued" if failure == "receipt-response" else "pending"}
    assert fixtures.finalization.execution(runtime)["chat_terminal"]
    transport.client.send_message.side_effect = None
    transport.client.send_message.return_value = {"MessageId": "accepted"}
    monkeypatch.setattr(protected.client, "update_item", update)
    assert (await fixtures.finalization.request(client, runtime)).status_code == 200
    assert transport.client.send_message.call_args.kwargs == original
    assert fixtures.outbox(runtime)["status"] == {"S": "queued"}
    assert transport.client.send_message.call_count == (1 if failure == "receipt-response" else 2)


@pytest.mark.parametrize("field,value", [("owner_principal", "other"), ("created_at", 1), ("expires_at", 1), ("channel", "slack"), ("threads", {})])
@pytest.mark.parametrize("stage", ["publisher", "consumer"])
async def test_replaced_owner_session_is_never_redirected(client, runtime, ready, transport, consumer, field, value, stage):
    if stage == "consumer":
        assert (await fixtures.finalization.request(client, runtime)).status_code == 200
    transport.sessions.put_item(Item={**transport.row, field: value})
    if stage == "publisher":
        assert (await fixtures.finalization.request(client, runtime)).status_code == 404
        transport.client.send_message.assert_not_called()
    else:
        with pytest.raises(ValueError):
            consumer._process_response(payload(transport))
    assert "messages" not in session(transport)
    consumer.ws_router._client.post_to_connection.assert_not_called()
    consumer.sqs.send_message.assert_not_called()


async def test_websocket_failure_retries_without_duplicate_history_or_unlock(client, runtime, ready, transport, consumer, monkeypatch):
    assert (await fixtures.finalization.request(client, runtime)).status_code == 200
    route = consumer.ws_router.route
    monkeypatch.setattr(consumer.ws_router, "route", lambda *args: False)
    event = {"Records": [{"messageId": "notification-a", "body": json.dumps(payload(transport))}]}
    assert consumer.lambda_handler(event, None) == {"batchItemFailures": [{"itemIdentifier": "notification-a"}]}
    assert session(transport)["threads"]["thread-a"]["terminal_delivery"]["status"] == "persisted"
    assert session(transport)["threads"]["thread-a"]["processing_task_id"] == "task-a"
    monkeypatch.setattr(consumer.ws_router, "route", route)
    assert consumer.lambda_handler(event, None) == {"statusCode": 200}
    assert len(session(transport)["messages"]) == 1


async def test_fifo_batch_stops_after_failed_terminal_delivery(client, runtime, ready, transport, consumer, monkeypatch):
    assert (await fixtures.finalization.request(client, runtime)).status_code == 200
    route = Mock(return_value=False)
    monkeypatch.setattr(consumer.ws_router, "route", route)
    event = {"Records": [{"messageId": name, "body": json.dumps(payload(transport))} for name in ("first", "later")]}
    assert consumer.lambda_handler(event, None) == {"batchItemFailures": [{"itemIdentifier": "first"}, {"itemIdentifier": "later"}]}
    assert route.call_count == 1
    assert len(session(transport)["messages"]) == 1


async def test_lost_websocket_receipt_resends_same_frame_identity_without_duplicate_history(client, runtime, ready, transport, consumer, monkeypatch):
    assert (await fixtures.finalization.request(client, runtime)).status_code == 200
    update = transport.sessions.update_item

    def unavailable(**kwargs):
        if ":sent" in kwargs.get("ExpressionAttributeValues", {}):
            raise EndpointConnectionError(endpoint_url="https://storage.example.test")
        return update(**kwargs)

    monkeypatch.setattr(transport.sessions, "update_item", unavailable)
    with pytest.raises(EndpointConnectionError):
        consumer._process_response(payload(transport))
    monkeypatch.setattr(transport.sessions, "update_item", update)
    consumer._process_response(payload(transport))
    frames = [json.loads(call.kwargs["Data"]) for call in consumer.ws_router._client.post_to_connection.call_args_list]
    assert len(frames) == 2 and frames[0]["delivery_id"] == frames[1]["delivery_id"]
    assert len(session(transport)["messages"]) == 1


@pytest.mark.parametrize("changes", [{"text": "replacement"}, {"delivery_id": "chat-terminal-" + "a" * 64}, {"status": "completed"}])
async def test_conflicting_duplicate_cannot_replace_reply(client, runtime, ready, transport, consumer, changes):
    assert (await fixtures.finalization.request(client, runtime)).status_code == 200
    consumer._process_response(payload(transport))
    before = session(transport)
    with pytest.raises(ValueError):
        consumer._process_response({**payload(transport), **changes})
    assert session(transport) == before
    assert consumer.ws_router._client.post_to_connection.call_count == 1


async def test_old_terminal_cannot_complete_newer_processing_task(client, runtime, ready, transport, consumer):
    assert (await fixtures.finalization.request(client, runtime)).status_code == 200
    transport.sessions.put_item(Item={**transport.row, "threads": {"thread-a": {"processing_task_id": "newer-task"}}})
    with pytest.raises(ValueError):
        consumer._process_response(payload(transport))
    assert session(transport)["threads"]["thread-a"]["processing_task_id"] == "newer-task"
    consumer.ws_router._client.post_to_connection.assert_not_called()


async def test_missing_queue_receipt_never_claims_confirmed_handoff(client, runtime, ready, transport):
    assert (await fixtures.finalization.request(client, runtime)).status_code == 200
    item = fixtures.outbox(runtime)
    del item["queue_message_id"]
    fixtures.write_protected(runtime, item)
    assert (await fixtures.finalization.request(client, runtime)).status_code == 503
    assert transport.client.send_message.call_count == 1


async def test_sandbox_capability_cannot_publish_terminal_output(client, runtime, ready, transport):
    response = await client.post(
        fixtures.finalization.FINALIZE,
        json=fixtures.finalization.body(runtime),
        headers={"Authorization": f"Bearer {ready}", "X-User-Id": "human", "X-Tenant-Id": "tenant"},
    )
    assert response.status_code == 403
    transport.client.send_message.assert_not_called()
    assert fixtures.outbox(runtime) is None


async def test_session_change_between_persistence_and_send_is_refused(client, runtime, ready, transport, consumer, monkeypatch):
    assert (await fixtures.finalization.request(client, runtime)).status_code == 200
    route = consumer.ws_router.route

    def replace_owner(*args):
        transport.sessions.put_item(Item={**session(transport), "owner_principal": "other-owner", "connection_id": "foreign-connection"})
        return route(*args)

    monkeypatch.setattr(consumer.ws_router, "route", replace_owner)
    with pytest.raises(RuntimeError):
        consumer._process_response(payload(transport))
    consumer.ws_router._client.post_to_connection.assert_not_called()
    assert session(transport)["threads"]["thread-a"]["terminal_delivery"]["status"] == "persisted"


@pytest.mark.parametrize("changes", [{"connection_id": "foreign"}, {"channel_metadata": {"owner_principal": "other"}}, {"strict_delivery": False}])
async def test_consumer_refuses_routing_overrides(client, runtime, ready, transport, consumer, changes):
    assert (await fixtures.finalization.request(client, runtime)).status_code == 200
    with pytest.raises(ValueError):
        consumer._process_response({**payload(transport), **changes})
    assert "messages" not in session(transport)
    consumer.ws_router._client.post_to_connection.assert_not_called()
