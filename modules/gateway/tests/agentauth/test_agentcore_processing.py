"""Durable SCN-04 gateway capture processing after mounted owner delivery."""

from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from starlette.concurrency import run_in_threadpool

from src.agentauth.agentcore_processing import AgentCoreCaptureProcessor
from src.agentauth.agentcore_recall import AgentCoreRecall
from tests.agentauth import test_agentcore_capture as captured

client = captured.client
runtime = captured.runtime
store = captured.store
sts = captured.sts
capability = captured.capability
retained_input_table = captured.retained_input_table
ready = captured.ready
registered_owner = captured.registered_owner
transport = captured.transport
consumer = captured.consumer
plain_user_turn = captured.plain_user_turn


@pytest.fixture
async def pending(client, runtime, ready, transport, consumer):
    table = runtime[2]
    header_key = {"PK": "session#session-a", "SK": "header"}
    header = table.get_item(Key=header_key, ConsistentRead=True)["Item"]
    table.put_item(Item={**header, "memoryPrivacy": "shared", "memoryConsent": True})
    result = await captured.deliver(client, runtime, ready, transport, consumer)
    assert result.status_code == 200, result.text
    row = captured.captures(runtime)[0]
    return row, table, header_key, result.json()["memory_capture"]


@pytest.fixture
def worker(runtime):
    capabilities, authority = runtime[:2]
    provider = AgentCoreRecall(Mock(), memory_id="configured-memory", strategy_id="semantic", namespace_key=b"t" * 32)
    return AgentCoreCaptureProcessor(authority, capabilities, provider)


def memory_row(table, receipt):
    return table.get_item(Key={"PK": f"memory-capture#{receipt}", "SK": "capture"}, ConsistentRead=True)["Item"]


async def test_mounted_capture_replays_after_worker_crashes_using_one_stable_event_token(pending, worker, runtime, monkeypatch):
    _, table, _, receipt = pending
    receipt_id = receipt["receipt_id"]
    now = runtime[-1]
    calls = []

    def accepted(**kwargs):
        calls.append(kwargs)
        return {"event": {"eventId": "service-event", "memoryId": kwargs["memoryId"], "sessionId": kwargs["sessionId"], "actorId": kwargs["actorId"]}}

    worker.provider.client.create_event.side_effect = accepted

    def crash_after_service(*args, **kwargs):
        raise SystemExit("synthetic worker crash")

    monkeypatch.setattr(worker, "_finish", crash_after_service)
    with pytest.raises(SystemExit, match="synthetic worker crash"):
        await run_in_threadpool(worker.process, receipt_id, now=now)
    assert memory_row(table, receipt_id)["status"] == "processing"
    assert await run_in_threadpool(worker.process, receipt_id, now=now + 1) is None
    resumed = AgentCoreCaptureProcessor(worker.authority, worker.capabilities, worker.provider)
    assert await run_in_threadpool(resumed.process, receipt_id, now=now + 31) == {"status": "pending", "receipt_id": receipt_id}
    stored = memory_row(table, receipt_id)
    assert stored["status"] == "submitted"
    assert stored["attempts"] == 2
    assert stored["submittedAt"] == now + 31
    assert stored["serviceEventId"] == "service-event"
    assert all(call["clientToken"] == receipt_id for call in calls)
    assert calls[-1]["metadata"] == {"adp_receipt": {"stringValue": receipt_id}, "adp_epoch": {"stringValue": "0"}}
    assert calls[-1]["payload"][0]["conversational"]["content"]["text"] == "Remember my preference"
    assert await run_in_threadpool(worker.process, receipt_id, now=now + 62) is None
    assert len(calls) == 2


async def test_retry_backoff_and_durable_failure_status(pending, worker, runtime):
    _, table, _, receipt = pending
    receipt_id = receipt["receipt_id"]
    now = runtime[-1]
    worker.provider.client.create_event.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailableException", "Message": "down"}}, "CreateEvent"
    )
    for attempt in range(1, 6):
        result = await run_in_threadpool(worker.process, receipt_id, now=now)
        assert result["status"] == ("failed" if attempt == 5 else "pending")
        stored = memory_row(table, receipt_id)
        assert stored["attempts"] == attempt
        assert await run_in_threadpool(worker.process, receipt_id, now=now) is None
        now += min(2**attempt, 300)
    assert stored["failureCode"] == "service_unavailable"
    assert worker.provider.client.create_event.call_count == 5


async def test_revocation_before_processing_fails_closed_without_service_access(pending, worker, runtime):
    _, table, header_key, receipt = pending
    table.update_item(Key=header_key, UpdateExpression="SET memoryConsent = :disabled", ExpressionAttributeValues={":disabled": False})
    result = await run_in_threadpool(worker.process, receipt["receipt_id"], now=runtime[-1])
    assert result["status"] == "failed"
    assert memory_row(table, receipt["receipt_id"])["failureCode"] == "authority_refused"
    worker.provider.client.create_event.assert_not_called()


async def test_poll_finds_existing_outbox_item_without_a_crew_worker(pending, worker, runtime):
    _, table, _, receipt = pending
    worker.provider.client.create_event.return_value = {
        "event": {
            "eventId": "service-event",
            "memoryId": "configured-memory",
            "sessionId": "session-a",
            "actorId": worker.provider.owner_namespace(type("Owner", (), {"tenant_id": "tenant", "user_id": "human"})()).rsplit("/", 1)[-1],
        }
    }
    cursor = await run_in_threadpool(worker.poll, now=runtime[-1])
    assert cursor is None
    assert memory_row(table, receipt["receipt_id"])["status"] == "submitted"


async def test_corrupt_capture_never_reaches_agentcore(pending, worker, runtime):
    _, table, _, receipt = pending
    key = {"PK": f"memory-capture#{receipt['receipt_id']}", "SK": "capture"}
    table.update_item(Key=key, UpdateExpression="SET requestDigest = :invalid", ExpressionAttributeValues={":invalid": True})
    result = await run_in_threadpool(worker.process, receipt["receipt_id"], now=runtime[-1])
    assert result == {"status": "failed", "receipt_id": receipt["receipt_id"]}
    assert memory_row(table, receipt["receipt_id"])["failureCode"] == "authority_refused"
    worker.provider.client.create_event.assert_not_called()


async def test_repeated_crash_claims_stop_before_extra_service_call(pending, worker, runtime, monkeypatch):
    _, table, _, receipt = pending
    receipt_id = receipt["receipt_id"]
    now = runtime[-1]

    def accepted(**kwargs):
        return {
            "event": {
                "eventId": "service-event",
                "memoryId": kwargs["memoryId"],
                "sessionId": kwargs["sessionId"],
                "actorId": kwargs["actorId"],
            }
        }

    worker.provider.client.create_event.side_effect = accepted
    original_finish = worker._finish

    def crash_after_service(*args, **kwargs):
        raise SystemExit("synthetic worker crash")

    monkeypatch.setattr(worker, "_finish", crash_after_service)
    for attempt in range(5):
        with pytest.raises(SystemExit, match="synthetic worker crash"):
            await run_in_threadpool(worker.process, receipt_id, now=now + attempt * 31)
    monkeypatch.setattr(worker, "_finish", original_finish)
    result = await run_in_threadpool(worker.process, receipt_id, now=now + 5 * 31)
    assert result == {"status": "failed", "receipt_id": receipt_id}
    assert memory_row(table, receipt_id)["failureCode"] == "processing_exhausted"
    assert worker.provider.client.create_event.call_count == 5


async def test_poll_does_not_follow_corrupt_rows_to_another_receipt(pending, worker, runtime):
    _, table, _, receipt = pending
    key = {"PK": f"memory-capture#{receipt['receipt_id']}", "SK": "capture"}
    table.update_item(Key=key, UpdateExpression="SET receiptId = :other", ExpressionAttributeValues={":other": "f" * 64})
    await run_in_threadpool(worker.poll, now=runtime[-1])
    assert memory_row(table, receipt["receipt_id"])["failureCode"] == "invalid_capture"
    worker.provider.client.create_event.assert_not_called()
