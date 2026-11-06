"""SCN-04: mounted delivery receipt and AgentCore processing status stay distinct."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from starlette.concurrency import run_in_threadpool

from src.agentauth import agentcore_processing, chat_data_routes, chat_model
from src.agentauth.agentcore_governance import advance_owner_state, owner_state
from src.agentauth.agentcore_processing import AgentCoreCaptureProcessor
from src.agentauth.agentcore_recall import AgentCoreRecall
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.orchestration import chat_history
from src.shared.schemas.auth import TokenContext
from tests.agentauth import test_agentcore_capture as capture

client = capture.client
runtime = capture.runtime
store = capture.store
sts = capture.sts
capability = capture.capability
retained_input_table = capture.retained_input_table
ready = capture.ready
registered_owner = capture.registered_owner
transport = capture.transport
consumer = capture.consumer
plain_user_turn = capture.plain_user_turn


@pytest.fixture(autouse=True)
def initial_governance(runtime, request):
    if getattr(request, "param", False):
        advance_owner_state(runtime[2], SimpleNamespace(tenant_id="tenant", user_id="human"), "forgotten-record", "a" * 64, "forget")


@pytest.mark.parametrize("permanent_failure", [False, True])
async def test_mounted_capture_status_never_claims_extraction_visibility(client, runtime, ready, transport, consumer, monkeypatch, permanent_failure):
    now = runtime[-1]
    monkeypatch.setenv("FEATURE_CHAT_ENABLED", "true")
    user = TokenContext(
        org_id="tenant",
        team_id="team",
        user_id="human",
        department_id="",
        account_type="human",
        expires_at=datetime.fromtimestamp(now + 600, UTC),
    )
    sessions = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="memory-status-sessions",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
    )
    sessions.put_item(
        Item={
            "session_id": "session-a",
            "owner_principal": chat_history.owner(user),
            "owner_user_id": "human",
            "user_workspace": "human#webchat",
            "tenant_id": "tenant",
            "org_id": "tenant",
            "channel": "webchat",
            "expires_at": now + 600,
        }
    )
    app = client._transport.app
    app.dependency_overrides[chat_data_routes.get_current_user] = lambda: user
    app.dependency_overrides[chat_history.store] = lambda: sessions
    header_key = {"PK": "session#session-a", "SK": "header"}
    header = runtime[2].get_item(Key=header_key, ConsistentRead=True)["Item"]
    runtime[2].put_item(Item={**header, "memoryPrivacy": "shared", "memoryConsent": True})
    delivered = await capture.deliver(client, runtime, ready, transport, consumer)
    assert delivered.status_code == 200, delivered.text
    receipt = delivered.json()["memory_capture"]["receipt_id"]
    url = f"/chat/sessions/session-a/agentcore-memory/captures/{receipt}"
    pending = await client.get(url)
    assert pending.status_code == 200, pending.text
    assert pending.headers["Cache-Control"] == "no-store"
    assert pending.json() == {
        "receipt_id": receipt,
        "status": "pending",
        "phase": "captured",
        "service_event_id": None,
        "record_id": None,
        "coverage": "unverified",
    }
    app.dependency_overrides[chat_data_routes.get_current_user] = lambda: user.model_copy(update={"user_id": "foreign"})
    foreign = await client.get(url)
    assert foreign.status_code == 404
    assert receipt not in foreign.text
    app.dependency_overrides[chat_data_routes.get_current_user] = lambda: user
    assert (await client.get(url.replace(receipt, "f" * 64))).status_code == 404
    provider = AgentCoreRecall(Mock(), memory_id="configured-memory", strategy_id="semantic", namespace_key=b"t" * 32)
    provider.client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    monkeypatch.setattr(chat_model, "agentcore_recall", lambda: provider)
    processor = AgentCoreCaptureProcessor(runtime[1], runtime[0], provider)
    provider.client.create_event.side_effect = ClientError({"Error": {"Code": "ServiceUnavailableException", "Message": "down"}}, "CreateEvent")
    assert (await run_in_threadpool(processor.process, receipt, now=now))["status"] == "pending"
    retry = await client.get(url)
    assert retry.status_code == 200
    assert retry.json()["status"] == "pending"
    assert retry.json()["service_event_id"] is None
    if permanent_failure:
        monkeypatch.setattr(agentcore_processing, "MAX_ATTEMPTS", 2)
        assert (await run_in_threadpool(processor.process, receipt, now=now + 2))["status"] == "failed"
        failed = await client.get(url)
        assert failed.status_code == 200
        assert failed.json()["status"] == "failed"
        assert failed.json()["phase"] == "failed"
        assert failed.json()["service_event_id"] is None
        assert failed.json()["coverage"] == "unverified"
        return
    provider.client.create_event.side_effect = None
    namespace = provider.owner_namespace(SimpleNamespace(tenant_id="tenant", user_id="human"))
    provider.client.create_event.return_value = {
        "event": {"memoryId": "configured-memory", "sessionId": "session-a", "actorId": namespace.rsplit("/", 1)[-1], "eventId": "service-event"}
    }
    assert (await run_in_threadpool(processor.process, receipt, now=now + 2))["status"] == "pending"
    accepted = await client.get(url)
    assert accepted.status_code == 200
    assert accepted.json()["phase"] == "event_accepted"
    assert accepted.json()["status"] == "pending"
    assert accepted.json()["service_event_id"] == "service-event"
    assert accepted.json()["coverage"] == "unverified"
    runtime[2].update_item(Key={"PK": f"memory-capture#{receipt}", "SK": "capture"}, UpdateExpression="REMOVE serviceEventId")
    assert (await client.get(url)).status_code == 503
    runtime[2].update_item(Key=header_key, UpdateExpression="SET memoryConsent = :disabled", ExpressionAttributeValues={":disabled": False})
    revoked = await client.get(url)
    assert revoked.status_code == 404
    assert "service-event" not in revoked.text
    assert (await capture.completion.complete(client, runtime)).json()["memory_capture"]["status"] == "captured_for_processing"


@pytest.fixture
async def prepared(client, runtime, ready, transport, consumer, monkeypatch):
    now = runtime[-1]
    monkeypatch.setenv("FEATURE_CHAT_ENABLED", "true")
    user = TokenContext(
        org_id="tenant",
        team_id="team",
        user_id="human",
        department_id="",
        account_type="human",
        expires_at=datetime.fromtimestamp(now + 600, UTC),
    )
    sessions = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="visibility-sessions",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
    )
    sessions.put_item(
        Item={
            "session_id": "session-a",
            "owner_principal": chat_history.owner(user),
            "owner_user_id": "human",
            "user_workspace": "human#webchat",
            "tenant_id": "tenant",
            "org_id": "tenant",
            "channel": "webchat",
            "expires_at": now + 600,
        }
    )
    app = client._transport.app
    app.dependency_overrides[chat_data_routes.get_current_user] = lambda: user
    app.dependency_overrides[chat_history.store] = lambda: sessions
    header_key = {"PK": "session#session-a", "SK": "header"}
    header = runtime[2].get_item(Key=header_key, ConsistentRead=True)["Item"]
    runtime[2].put_item(Item={**header, "memoryPrivacy": "shared", "memoryConsent": True})
    delivered = await capture.deliver(client, runtime, ready, transport, consumer)
    assert delivered.status_code == 200, delivered.text
    receipt = delivered.json()["memory_capture"]["receipt_id"]
    provider = AgentCoreRecall(Mock(), memory_id="configured-memory", strategy_id="semantic", namespace_key=b"t" * 32)
    namespace = provider.owner_namespace(SimpleNamespace(tenant_id="tenant", user_id="human"))
    provider.client.create_event.return_value = {
        "event": {"memoryId": "configured-memory", "sessionId": "session-a", "actorId": namespace.rsplit("/", 1)[-1], "eventId": "service-event"}
    }
    monkeypatch.setattr(chat_model, "agentcore_recall", lambda: provider)
    url = f"/chat/sessions/session-a/agentcore-memory/captures/{receipt}"
    return client, provider, namespace, receipt, url, runtime


@pytest.fixture
async def submitted(prepared):
    client, provider, namespace, receipt, url, runtime = prepared
    processor = AgentCoreCaptureProcessor(runtime[1], runtime[0], provider)
    assert (await run_in_threadpool(processor.process, receipt, now=runtime[-1]))["status"] == "pending"
    return prepared


@pytest.mark.parametrize("initial_governance", [True], indirect=True)
@pytest.mark.parametrize("service_epoch", [None, "0", "1"])
async def test_capture_visibility_agrees_with_recall_at_nonzero_epoch(submitted, service_epoch):
    client, provider, namespace, receipt, url, runtime = submitted
    stored = runtime[2].get_item(Key={"PK": f"memory-capture#{receipt}", "SK": "capture"})["Item"]
    assert stored["ownerEpoch"] == 1
    assert provider.client.create_event.call_args.kwargs["metadata"]["adp_epoch"] == {"stringValue": "1"}
    metadata = {"adp_receipt": {"stringValue": receipt}}
    if service_epoch is not None:
        metadata["adp_epoch"] = {"stringValue": service_epoch}
    record = {
        "memoryRecordId": "visible-record",
        "namespaces": [namespace],
        "memoryStrategyId": "semantic",
        "metadata": metadata,
        "content": {"text": "Remember my preference"},
    }
    provider.client.get_memory_record.return_value = {"memoryRecord": record}
    provider.client.list_memory_records.return_value = {"memoryRecordSummaries": [record]}
    provider.client.retrieve_memory_records.return_value = {"memoryRecordSummaries": [record]}
    capabilities = Mock()
    capabilities.verify_run.return_value = SimpleNamespace(tenant_id="tenant", team_id="team", user_id="human", session_id="session-a")
    recalled = provider.retrieve(capabilities, "verified", authority=runtime[1], run_id="run-write", query="preferences", now=runtime[-1])
    response = await client.get(url)
    assert response.status_code == 200, response.text
    if service_epoch == "1":
        assert recalled["record_ids"] == ["visible-record"]
        assert response.json()["status"] == "available"
        assert response.json()["coverage"] == "verified"
    else:
        assert recalled["record_ids"] == []
        assert response.json()["status"] == "pending"
        assert response.json()["phase"] == "event_accepted"
        assert response.json()["coverage"] == "unverified"
        assert response.json()["record_id"] is None


@pytest.mark.parametrize("epoch", [None, True, "0", -1, Decimal("0.5")])
async def test_invalid_capture_epoch_fails_before_service_readback(submitted, epoch):
    client, provider, _, receipt, url, runtime = submitted
    key = {"PK": f"memory-capture#{receipt}", "SK": "capture"}
    stored = runtime[2].get_item(Key=key)["Item"]
    stored.pop("ownerEpoch", None)
    if epoch is not None:
        stored["ownerEpoch"] = epoch
    runtime[2].put_item(Item=stored)
    response = await client.get(url)
    assert response.status_code == 503, response.text
    provider.client.list_memory_records.assert_not_called()
    provider.client.get_memory_record.assert_not_called()


async def test_corrected_revision_supports_later_edits_and_rejects_stale_mutations(submitted):
    client, provider, namespace, _, _, runtime = submitted
    record = {"memoryRecordId": "record-a", "namespaces": [namespace], "memoryStrategyId": "semantic", "content": {"text": "Original preference"}}
    provider.client.get_memory_record.return_value = {"memoryRecord": record}
    provider.client.list_memory_records.return_value = {"memoryRecordSummaries": [record]}
    base = "/chat/sessions/session-a/agentcore-memory"
    original = (await client.get(base)).json()["records"][0]["revision"]
    first = await client.post(base + "/record-a/correct", json={"revision": original, "text": "First correction"})
    assert first.status_code == 200, first.text
    inspection = (await client.get(base)).json()["records"][0]
    assert inspection["text"] == "First correction"
    assert inspection["revision"] != original
    assert inspection["revision"] == first.json()["revision"]
    assert (await client.get(base + "/export")).json()["records"] == [inspection]
    second = await client.post(base + "/record-a/correct", json={"revision": inspection["revision"], "text": "Second correction"})
    assert second.status_code == 200, second.text
    current = (await client.get(base)).json()["records"][0]
    assert current["text"] == "Second correction"
    assert current["revision"] == second.json()["revision"]
    assert current["revision"] not in (original, inspection["revision"])
    repeated = await client.post(base + "/record-a/correct", json={"revision": inspection["revision"], "text": "Second correction"})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == second.json()
    owner = SimpleNamespace(tenant_id="tenant", user_id="human")
    assert owner_state(runtime[2], owner)["epoch"] == 2
    events = provider.client.create_event.call_count
    for revision in (original, inspection["revision"]):
        stale_edit = await client.post(base + "/record-a/correct", json={"revision": revision, "text": "Stale correction"})
        stale_delete = await client.request("DELETE", base + "/record-a", json={"revision": revision})
        assert stale_edit.status_code == 409, stale_edit.text
        assert stale_delete.status_code == 409, stale_delete.text
    provider.client.delete_memory_record.assert_not_called()
    assert provider.client.create_event.call_count == events
    assert owner_state(runtime[2], owner)["epoch"] == 2
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]["memoryEpoch"] == 2
    deleted = await client.request("DELETE", base + "/record-a", json={"revision": current["revision"]})
    assert deleted.status_code == 200, deleted.text
    assert (await client.get(base)).json()["records"] == []
    assert owner_state(runtime[2], owner)["epoch"] == 3
    provider.client.delete_memory_record.assert_called_once()


@pytest.mark.parametrize("readback", ["delayed", "foreign", "wrong_marker", "get_foreign", "get_outage", "outage", "available", "fenced"])
async def test_scoped_readback_distinguishes_visibility_failure_and_outage(submitted, readback):
    from src.agentauth.agentcore_management import AgentCoreManagement

    client, provider, namespace, receipt, url, runtime = submitted
    record = {
        "memoryRecordId": "visible-record",
        "namespaces": [namespace],
        "memoryStrategyId": "semantic",
        "metadata": {"adp_receipt": {"stringValue": receipt}, "adp_epoch": {"stringValue": "0"}},
        "content": {"text": "Remember my preference"},
    }
    provider.client.get_memory_record.return_value = {"memoryRecord": record}
    provider.client.list_memory_records.return_value = {"memoryRecordSummaries": [record]}
    if readback == "delayed":
        provider.client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    elif readback == "foreign":
        provider.client.list_memory_records.return_value = {"memoryRecordSummaries": [{**record, "namespaces": ["/adp/owners/other"]}]}
    elif readback == "wrong_marker":
        provider.client.list_memory_records.return_value = {
            "memoryRecordSummaries": [{**record, "metadata": {"adp_receipt": {"stringValue": "f" * 64}}}]
        }
    elif readback == "get_foreign":
        provider.client.get_memory_record.return_value = {"memoryRecord": {**record, "namespaces": ["/adp/owners/other"]}}
    elif readback == "get_outage":
        provider.client.get_memory_record.side_effect = ClientError(
            {"Error": {"Code": "ServiceUnavailableException", "Message": "down"}}, "GetMemoryRecord"
        )
    elif readback == "outage":
        provider.client.list_memory_records.side_effect = ClientError(
            {"Error": {"Code": "ServiceUnavailableException", "Message": "down"}}, "ListMemoryRecords"
        )
    result = await client.get(url)
    assert result.status_code == 200, result.text
    assert result.headers["Cache-Control"] == "no-store"
    if readback in ("outage", "get_outage"):
        assert result.json()["status"] == "unavailable"
        assert result.json()["coverage"] == "unavailable"
        assert result.json()["service_event_id"] == "service-event"
        if readback == "outage":
            provider.client.get_memory_record.assert_not_called()
    elif readback in ("available", "fenced"):
        assert result.json()["status"] == "available"
        assert result.json()["phase"] == "available_for_recall"
        assert result.json()["record_id"] == "visible-record"
        assert result.json()["coverage"] == "verified"
        provider.client.get_memory_record.assert_called_once_with(memoryId="configured-memory", namespace=namespace, memoryRecordId="visible-record")
        if readback == "fenced":
            revision = AgentCoreManagement.public(record)["revision"]
            correction = await client.post(
                "/chat/sessions/session-a/agentcore-memory/visible-record/correct",
                json={"revision": revision, "text": "Use a different preference"},
            )
            assert correction.status_code == 200, correction.text
            fenced = await client.get(url)
            assert fenced.status_code == 200
            assert fenced.json()["phase"] == "fenced"
            assert fenced.json()["status"] != "available"
            assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]["memoryEpoch"] == 1
    else:
        assert result.json()["status"] == "pending"
        assert result.json()["phase"] == "event_accepted"
        assert result.json()["record_id"] is None
        assert result.json()["coverage"] == "unverified"
        if readback != "get_foreign":
            provider.client.get_memory_record.assert_not_called()
    assert provider.client.list_memory_records.call_args.kwargs["namespace"] == namespace
    assert provider.client.list_memory_records.call_args.kwargs["memoryStrategyId"] == "semantic"


async def test_mounted_operation_receipts_require_service_readback_and_owner_fence(submitted):
    from src.agentauth.agentcore_management import AgentCoreManagement

    client, provider, namespace, _, _, runtime = submitted
    record = {
        "memoryRecordId": "record-a",
        "namespaces": [namespace],
        "memoryStrategyId": "semantic",
        "content": {"text": "Use short responses"},
    }
    provider.client.get_memory_record.return_value = {"memoryRecord": record}
    provider.client.list_memory_records.return_value = {"memoryRecordSummaries": []}
    revision = AgentCoreManagement.public(record)["revision"]
    base = "/chat/sessions/session-a/agentcore-memory/record-a"
    correction = await client.post(base + "/correct", json={"revision": revision, "text": "Use detailed responses"})
    assert correction.status_code == 200, correction.text
    assert correction.json()["status"] == "pending"
    assert correction.json()["service_event_id"] == "service-event"
    correction_receipt = correction.json()["receipt_id"]
    assert provider.client.create_event.call_args.kwargs["metadata"]["adp_receipt"] == {"stringValue": correction_receipt}
    status_url = base + "/operations/" + correction_receipt
    pending = await client.get(status_url)
    assert pending.json()["status"] == "pending" and pending.json()["coverage"] == "unverified"
    assert pending.json()["service_record_id"] is None

    extracted = {
        **record,
        "memoryRecordId": "corrected-record",
        "metadata": {"adp_receipt": {"stringValue": correction_receipt}, "adp_epoch": {"stringValue": "1"}},
        "content": {"text": "Use detailed responses"},
    }
    provider.client.list_memory_records.return_value = {
        "memoryRecordSummaries": [{**extracted, "metadata": {**extracted["metadata"], "adp_epoch": {"stringValue": "0"}}}]
    }
    provider.client.get_memory_record.side_effect = lambda **kwargs: {
        "memoryRecord": {**extracted, "metadata": {**extracted["metadata"], "adp_epoch": {"stringValue": "0"}}}
        if kwargs["memoryRecordId"] == "corrected-record"
        else record
    }
    wrong_epoch = await client.get(status_url)
    assert wrong_epoch.json()["status"] == "pending" and wrong_epoch.json()["coverage"] == "unverified"
    provider.client.list_memory_records.return_value = {"memoryRecordSummaries": [extracted]}
    provider.client.get_memory_record.side_effect = lambda **kwargs: {
        "memoryRecord": extracted if kwargs["memoryRecordId"] == "corrected-record" else record
    }
    available = await client.get(status_url)
    assert available.json()["status"] == "available" and available.json()["coverage"] == "verified"
    assert available.json()["service_record_id"] == "corrected-record"
    assert available.headers["cache-control"] == "no-store"
    previous_reads = provider.client.list_memory_records.call_count
    forged = await client.get(base + "/operations/" + "a" * 64)
    assert forged.status_code == 404 and provider.client.list_memory_records.call_count == previous_reads

    deletion = await client.request("DELETE", base, json={"revision": correction.json()["revision"]})
    assert deletion.status_code == 200, deletion.text
    assert deletion.json()["receipt_id"] != correction_receipt
    deletion_url = base + "/operations/" + deletion.json()["receipt_id"]
    still_present = await client.get(deletion_url)
    assert still_present.json()["status"] == "pending" and still_present.json()["phase"] == "deletion_requested"
    provider.client.get_memory_record.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "GetMemoryRecord"
    )
    absent = await client.get(deletion_url)
    assert absent.json()["status"] == "pending" and absent.json()["phase"] == "target_absent"
    assert absent.json()["coverage"] == "partial"
    provider.client.get_memory_record.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailableException", "Message": "down"}}, "GetMemoryRecord"
    )
    unavailable = await client.get(deletion_url)
    assert unavailable.json()["status"] == "unavailable" and unavailable.json()["coverage"] == "unavailable"
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]["memoryEpoch"] == 2


async def test_correction_missing_service_receipt_is_unavailable_and_retryable(submitted):
    from src.agentauth.agentcore_governance import owner_state
    from src.agentauth.agentcore_management import AgentCoreManagement

    client, provider, namespace, _, _, runtime = submitted
    record = {"memoryRecordId": "record-a", "namespaces": [namespace], "memoryStrategyId": "semantic", "content": {"text": "Use short responses"}}
    provider.client.get_memory_record.return_value = {"memoryRecord": record}
    provider.client.create_event.return_value = {}
    revision = AgentCoreManagement.public(record)["revision"]
    base = "/chat/sessions/session-a/agentcore-memory/record-a"
    failed = await client.post(base + "/correct", json={"revision": revision, "text": "Use detailed responses"})
    assert failed.status_code == 503
    owner = SimpleNamespace(tenant_id="tenant", team_id="team", user_id="human", session_id="session-a")
    assert owner_state(runtime[2], owner)["epoch"] == 1
    provider.client.create_event.return_value = {
        "event": {"memoryId": "configured-memory", "sessionId": "session-a", "actorId": namespace.rsplit("/", 1)[-1], "eventId": "service-retry"}
    }
    retried = await client.post(base + "/correct", json={"revision": revision, "text": "Use detailed responses"})
    assert retried.status_code == 200, retried.text
    assert retried.json()["service_event_id"] == "service-retry"
    assert retried.json()["receipt_id"] == AgentCoreManagement.operation_receipt(namespace, "record-a", revision, "correct", "Use detailed responses")
    assert owner_state(runtime[2], owner)["epoch"] == 1

    app = client._transport.app
    foreign = TokenContext(
        org_id="tenant",
        team_id="team",
        user_id="other",
        department_id="",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    app.dependency_overrides[chat_data_routes.get_current_user] = lambda: foreign
    denied = await client.get(base + "/operations/" + retried.json()["receipt_id"])
    assert denied.status_code == 404
    provider.client.list_memory_records.assert_not_called()


async def test_mounted_correction_precedes_stale_inference_and_forget_fences_old_capture(submitted):
    from src.agentauth.agentcore_governance import owner_state
    from src.agentauth.agentcore_management import AgentCoreManagement

    client, provider, namespace, receipt, url, runtime = submitted
    record = {
        "memoryRecordId": "record-a",
        "namespaces": [namespace],
        "memoryStrategyId": "semantic",
        "content": {"text": "Use short responses"},
    }
    provider.client.get_memory_record.return_value = {"memoryRecord": record}
    provider.client.list_memory_records.return_value = {"memoryRecordSummaries": [record]}
    provider.client.retrieve_memory_records.return_value = {"memoryRecordSummaries": [record]}
    revision = AgentCoreManagement.public(record)["revision"]
    owner = SimpleNamespace(tenant_id="tenant", team_id="team", user_id="human", session_id="session-a")
    assert owner_state(runtime[2], owner)["epoch"] == 0
    base = "/chat/sessions/session-a/agentcore-memory"

    correction = await client.post(base + "/record-a/correct", json={"revision": revision, "text": "Use detailed responses"})
    assert correction.status_code == 200, correction.text
    assert correction.json()["status"] == "pending"
    assert owner_state(runtime[2], owner)["epoch"] == 1
    assert provider.client.create_event.call_args.kwargs["metadata"]["adp_epoch"] == {"stringValue": "1"}
    repeated = await client.post(base + "/record-a/correct", json={"revision": revision, "text": "Use detailed responses"})
    assert repeated.status_code == 200
    assert repeated.json()["receipt_id"] == correction.json()["receipt_id"]
    assert owner_state(runtime[2], owner)["epoch"] == 1
    assert runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]["memoryEpoch"] == 1
    inspection = await client.get(base)
    assert inspection.status_code == 200, inspection.text
    assert [record["text"] for record in inspection.json()["records"]] == ["Use detailed responses"]
    assert inspection.json()["records"][0]["source"] == "Explicit correction"
    assert "Use short responses" not in (await client.get(base + "/export")).text
    capabilities = Mock()
    capabilities.verify_run.return_value = owner
    recalled = provider.retrieve(capabilities, "verified", authority=runtime[1], run_id="run-a", query="response preference", now=runtime[-1])
    assert "Use detailed responses" in recalled["context"]
    assert "Use short responses" not in recalled["context"]
    assert (await client.get(url)).json()["phase"] == "fenced"

    deletion = await client.request("DELETE", base + "/record-a", json={"revision": inspection.json()["records"][0]["revision"]})
    assert deletion.status_code == 200, deletion.text
    assert deletion.json()["status"] == "pending"
    assert owner_state(runtime[2], owner)["epoch"] == 2
    assert (await client.get(base)).json()["records"] == []
    stale = await client.post(base + "/record-a/correct", json={"revision": revision, "text": "Use short responses"})
    assert stale.status_code == 409
    assert owner_state(runtime[2], owner)["epoch"] == 2
    recalled = provider.retrieve(capabilities, "verified", authority=runtime[1], run_id="run-a", query="response preference", now=runtime[-1])
    assert recalled["record_ids"] == []
    assert recalled["context"] == ""
    assert (await client.get(url)).json()["status"] != "available"


@pytest.mark.parametrize("mutation", ["correct", "forget"])
async def test_mounted_mutation_rejects_delayed_capture_and_cached_old_recall(prepared, mutation, monkeypatch):
    from src.agentauth.agentcore_governance import owner_state
    from src.agentauth.agentcore_management import AgentCoreManagement

    client, provider, namespace, receipt, url, runtime = prepared
    record = {
        "memoryRecordId": "record-a",
        "namespaces": [namespace],
        "memoryStrategyId": "semantic",
        "content": {"text": "Use short responses"},
    }
    provider.client.get_memory_record.return_value = {"memoryRecord": record}
    provider.client.retrieve_memory_records.return_value = {"memoryRecordSummaries": [record]}
    owner = SimpleNamespace(tenant_id="tenant", team_id="team", user_id="human", session_id="session-a")
    revision = AgentCoreManagement.public(record)["revision"]
    capabilities = Mock()
    capabilities.verify_run.return_value = owner
    cached = provider.retrieve(capabilities, "verified", authority=runtime[1], run_id="run-a", query="preferences", now=runtime[-1])
    assert "Use short responses" in cached["context"]
    assert owner_state(runtime[2], owner)["epoch"] == 0
    pending = runtime[2].get_item(Key={"PK": f"memory-capture#{receipt}", "SK": "capture"}, ConsistentRead=True)["Item"]
    assert pending["status"] == "pending" and pending["ownerEpoch"] == 0
    monkeypatch.setattr(runtime[0], "member", lambda tenant, user, team: (tenant, user, team) == ("tenant", "human", "team"))
    processor = AgentCoreCaptureProcessor(runtime[1], runtime[0], provider)
    assert processor._authorized(pending, runtime[-1]) == namespace

    base = "/chat/sessions/session-a/agentcore-memory/record-a"
    if mutation == "correct":
        changed = await client.post(base + "/correct", json={"revision": revision, "text": "Use detailed responses"})
    else:
        changed = await client.request("DELETE", base, json={"revision": revision})
    assert changed.status_code == 200, changed.text
    assert owner_state(runtime[2], owner)["epoch"] == 1
    provider.client.create_event.reset_mock()
    with pytest.raises(ChatAuthorizationRefusedError, match="memory capture superseded"):
        processor._authorized(pending, runtime[-1])
    assert (await run_in_threadpool(processor.process, receipt, now=runtime[-1]))["status"] == "failed"
    assert runtime[2].get_item(Key={"PK": f"memory-capture#{receipt}", "SK": "capture"})["Item"]["failureCode"] == "authority_refused"
    provider.client.create_event.assert_not_called()
    status = await client.get(url)
    assert status.status_code == 200 and status.json()["status"] == "failed"
    recalled = provider.retrieve(capabilities, "verified", authority=runtime[1], run_id="run-a", query="preferences", now=runtime[-1])
    assert "Use short responses" not in recalled["context"]
    if mutation == "correct":
        assert "Use detailed responses" in recalled["context"]
    else:
        assert recalled["record_ids"] == []
        assert recalled["context"] == ""


@pytest.mark.parametrize("mutation", ["correct", "forget"])
async def test_recall_discards_old_service_result_when_owner_is_fenced_during_retrieval(prepared, mutation):
    from src.agentauth.agentcore_governance import advance_owner_state, owner_state

    _, provider, namespace, _, _, runtime = prepared
    owner = SimpleNamespace(tenant_id="tenant", team_id="team", user_id="human", session_id="session-a")
    capabilities = Mock()
    capabilities.verify_run.return_value = owner

    def return_stale_result(**kwargs):
        assert kwargs["namespace"] == namespace
        advance_owner_state(runtime[2], owner, "record-a", "a" * 64, mutation, "Use detailed responses" if mutation == "correct" else None)
        return {
            "memoryRecordSummaries": [
                {
                    "memoryRecordId": "record-a",
                    "namespaces": [namespace],
                    "memoryStrategyId": "semantic",
                    "content": {"text": "Use short responses"},
                }
            ]
        }

    provider.client.retrieve_memory_records.side_effect = return_stale_result
    result = provider.retrieve(capabilities, "verified", authority=runtime[1], run_id="run-a", query="preferences", now=runtime[-1])
    assert owner_state(runtime[2], owner)["epoch"] == 1
    assert result["status"] == "partial"
    assert "Use short responses" not in result["context"]
    if mutation == "correct":
        assert "Use detailed responses" in result["context"]
    else:
        assert result["context"] == ""
        assert result["record_ids"] == []


async def test_mounted_deleted_crew_turn_replay_never_creates_a_new_capture(prepared, ready, monkeypatch):
    from src.agentauth.agentcore_governance import owner_state
    from src.agentauth.agentcore_management import AgentCoreManagement

    client, provider, namespace, receipt, url, runtime = prepared
    original = runtime[2].get_item(Key={"PK": f"memory-capture#{receipt}", "SK": "capture"}, ConsistentRead=True)["Item"]
    record = {
        "memoryRecordId": "record-a",
        "namespaces": [namespace],
        "memoryStrategyId": "semantic",
        "content": {"text": original["userText"]},
    }
    provider.client.get_memory_record.return_value = {"memoryRecord": record}
    revision = AgentCoreManagement.public(record)["revision"]
    deleted = await client.request("DELETE", "/chat/sessions/session-a/agentcore-memory/record-a", json={"revision": revision})
    assert deleted.status_code == 200, deleted.text
    owner = SimpleNamespace(tenant_id="tenant", user_id="human")
    assert owner_state(runtime[2], owner)["epoch"] == 1

    replay = await capture.completion.complete(client, runtime)
    assert replay.status_code == 200, replay.text
    assert replay.json()["memory_capture"] == {"status": "captured_for_processing", "receipt_id": receipt}
    old_append = await capture.history.append(client, ready, idempotency_key="replayed-old-crew-prompt", content=original["userText"])
    assert old_append.status_code == 404, old_append.text
    old_result = await capture.completion.fixtures.results.commit(client, ready, outcome="completed", message_id=original["messageId"])
    assert old_result.status_code == 404, old_result.text
    assert [row["receiptId"] for row in capture.captures(runtime)] == [receipt]
    monkeypatch.setattr(runtime[0], "member", lambda tenant, user, team: (tenant, user, team) == ("tenant", "human", "team"))
    provider.client.create_event.reset_mock()
    processor = AgentCoreCaptureProcessor(runtime[1], runtime[0], provider)
    assert (await run_in_threadpool(processor.process, receipt, now=runtime[-1]))["status"] == "failed"
    provider.client.create_event.assert_not_called()
    assert (await client.get(url)).json()["status"] == "failed"
