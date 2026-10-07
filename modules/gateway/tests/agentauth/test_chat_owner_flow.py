"""One admitted turn through real routes, emulated stores and the owner consumer.

Provider inference, Kubernetes observations, SQS and WebSocket transport are
substituted. This suite does not establish running-sandbox or deployed isolation.
"""

import base64
import hashlib
import json
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from botocore.exceptions import EndpointConnectionError
from sqlalchemy import select

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_model_execution import record_chat_accounting_gap
from src.budget.enforcement_settings import BudgetAccountingGap
from src.shared.models.usage import UsageLog
from src.usage.service import UsageService
from tests.agentauth import test_chat_cancellation as cancellation
from tests.agentauth import test_chat_delivery as delivery
from tests.agentauth import test_chat_model_execution as execution
from tests.agentauth import test_chat_terminal_publication as publication
from tests.agentauth.test_chat_artifact import artifacts as artifacts_fixture
from tests.agentauth.test_chat_sandbox_exit import supervisor
from tests.agentauth.test_work_producer import proof

pytestmark = pytest.mark.integration
client = delivery.client
model = delivery.model
protected_root = delivery.protected_root
retained_input_table = delivery.retained_input_table
runtime = delivery.runtime
store = delivery.store
sts = delivery.sts
transport = delivery.transport
consumer = publication.consumer
artifacts = artifacts_fixture
owner = cancellation.owner
RUN = "run-user"
SCOPE = {"run_id": RUN, "session_id": "session-a"}


@pytest.fixture
async def ledger(model, db_session):
    model.service.db = db_session
    model.service.usage_writer = AsyncMock(wraps=UsageService(db_session).log_request)
    model.service.gap_writer = AsyncMock(wraps=record_chat_accounting_gap)
    return db_session


@pytest.fixture
def lifecycle(model, sts, monkeypatch):
    supervisor(sts, monkeypatch)
    workloads = model.runtime[1].workloads
    monkeypatch.setattr(
        workloads,
        "observation_scope",
        {"api_server": "https://kubernetes.example.test", "namespace": "adp-gateway-agents", "service_account": "adp-chat-sandbox"},
        raising=False,
    )
    monkeypatch.setattr(workloads, "has_exited", Mock(return_value=True), raising=False)
    monkeypatch.setattr(workloads, "is_absent", Mock(return_value=True), raising=False)
    return workloads


async def data(model, operation, **fields):
    response = await model.client.post(f"/v1/chat/data/{operation}", json={**SCOPE, **fields})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def trusted(model, operation):
    dispatch = model.runtime[1].store._read(f"INVOCATION#{RUN}", "DISPATCH")
    document = {
        "run_id": RUN,
        "envelope_digest": dispatch["envelope_digest"]["S"],
        "pod_name": "chat-turn-" + hashlib.sha256(RUN.encode()).hexdigest()[:12] + "-abcde",
        "pod_uid": "chat-pod",
    }
    return await model.client.post(
        f"/internal/v1/agent/chat/data/{operation}",
        json=document,
        headers={"X-Adp-Producer-Proof": proof(envelope_digest(document))},
    )


async def finish(model, lifecycle):
    response = await trusted(model, "finalize")
    assert response.status_code == 404
    assert (await trusted(model, "exit")).json()["terminated"] is True
    assert (await trusted(model, "teardown")).json()["removed"] is True
    response = await trusted(model, "finalize")
    assert response.status_code == 200, response.text
    lifecycle.has_exited.assert_called_once()
    lifecycle.is_absent.assert_called_once()
    return response.json()


def queued(transport):
    return [json.loads(call.kwargs["MessageBody"]) for call in transport.client.send_message.call_args_list]


def deliver(transport, consumer):
    for message in queued(transport):
        consumer._process_response(message)
    return [json.loads(call.kwargs["Data"]) for call in consumer.ws_router._client.post_to_connection.call_args_list]


async def usage_rows(ledger):
    return list((await ledger.scalars(select(UsageLog).where(UsageLog.agent_run_id == RUN))).all())


async def assert_accounted(model, ledger, operations):
    logs = await usage_rows(ledger)
    assert len(logs) == len(operations)
    assert sum(row.cost_usd for row in logs) == Decimal("0.01") * len(operations)
    for operation_id in operations:
        record = model.service.journal._read(RUN, operation_id)
        assert record["usage_logged"] is True and record["reservation_status"] == "settled"
        log = next(row for row in logs if row.request_id == record["accounting_id"])
        assert (log.org_id, log.user_id, log.team_id, log.model) == ("tenant", "human", "team", execution.MODEL)
        assert (log.input_tokens, log.output_tokens) == (4, 2)
        reservation = await model.redis.hget(model.target.key(), log.request_id)
        assert Decimal(reservation.decode().split(":")[0]) == log.cost_usd
    assert model.service.event_writer.await_count == len(operations)
    model.service.gap_writer.assert_not_awaited()


@pytest.mark.parametrize("lost_queue_reply", [False, True])
async def test_context_summary_artifact_model_and_owner_share_one_accounted_turn(
    model, ledger, lifecycle, artifacts, transport, consumer, lost_queue_reply
):
    accepted = await data(model, "history/turn")
    page = await data(model, "history/read")
    assert page["version"] == 1 and page["entries"][0]["ref"] == accepted["message_id"]
    messages = await data(model, "history/messages", ids=[accepted["message_id"]])
    user = messages["entries"][0]["message"]
    assert user["role"] == "user" and user["content"] == "Please inspect this attachment"
    assert user["parts"] == [{"type": "file", "artifactId": "art_0123456789ab"}]
    private_summary = "Private context summary; never a streamed owner answer"
    model.provider.return_value = {**model.provider.return_value, "content": [{"type": "text", "text": private_summary}]}
    summary_request = {"messages": [{"role": "user", "content": user["content"]}], "max_tokens": 64}
    receipt = await delivery.invoke(model, operation_id="summarize", deliver_response=False, request=summary_request)
    assert receipt.status_code == 200 and receipt.json()["reservation_status"] == "settled"
    transport.client.send_message.assert_not_called()
    summary = await data(
        model,
        "history/compact",
        idempotency_key="summary",
        expected_version=page["version"],
        content=receipt.json()["content"][0]["text"],
        tokens=2,
        source_ids=[accepted["message_id"]],
        from_ordinal=1,
        to_ordinal=1,
    )
    compacted = await data(model, "history/read")
    assert compacted["entries"] == [{"ordinal": 1, "type": "sum", "ref": summary["summary_id"], "tokens": 2}]
    context = (await data(model, "history/summary", summary_id=summary["summary_id"]))["entries"][0]["summary"]
    assert context["content"] == private_summary and context["sourceIds"] == [accepted["message_id"]]
    assert (await data(model, "history/messages", ids=context["sourceIds"]))["entries"] == messages["entries"]
    content = b"Owner-only tool result"
    artifact_request = {
        "idempotency_key": "report",
        "filename": "report.txt",
        "content_type": "text/plain",
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "content_base64": base64.b64encode(content).decode(),
    }
    artifact = await data(model, "artifact/create", **artifact_request)
    assert await data(model, "artifact/create", **artifact_request) == artifact
    assert (await data(model, "artifact/list"))["entries"] == [artifact]
    downloaded = await model.client.get(artifact["url"])
    assert downloaded.status_code == 200 and downloaded.content == content
    assert "s3Key" not in artifact and "signature" not in artifact["url"].lower()
    reply = "Report ready: " + artifact["url"]
    model.provider.return_value = {**model.provider.return_value, "content": [{"type": "text", "text": reply}]}

    async def provider(**kwargs):
        await kwargs["on_event"]({"type": "text_delta", "index": 0, "text": reply})
        return model.provider.return_value

    model.provider.side_effect = provider
    request = {"messages": [{"role": "user", "content": context["content"] + "\n" + downloaded.text}], "max_tokens": 64}
    result = await delivery.invoke(model, request=request)
    assert result.status_code == 200 and result.json()["reservation_status"] == "settled"
    assert model.provider.await_args.kwargs["request"] == request
    assert (await delivery.invoke(model, request=request)).json() == result.json()
    saved = await data(
        model,
        "history/append",
        idempotency_key="answer",
        expected_version=compacted["version"],
        user_turn_id=accepted["message_id"],
        content=result.json()["content"][0]["text"],
        tokens=2,
    )
    candidate = await data(model, "turn/result", outcome="completed", message_id=saved["message_id"])
    assert candidate["terminal"] is False and not any(message.get("terminal_delivery") for message in queued(transport))
    if lost_queue_reply:
        transport.client.send_message.side_effect = EndpointConnectionError(endpoint_url="https://queue.example.test")
        assert (await trusted(model, "exit")).json()["terminated"] is True
        assert (await trusted(model, "teardown")).json()["removed"] is True
        assert (await trusted(model, "finalize")).status_code == 503
        transport.client.send_message.side_effect = None
        terminal = (await trusted(model, "finalize")).json()
    else:
        terminal = await finish(model, lifecycle)
    assert terminal["outcome"] == "completed" and terminal["accounting_status"] == "settled"
    assert terminal["message_id"] == saved["message_id"] and terminal["automatic_replay_permitted"] is False
    frames = deliver(transport, consumer)
    assert frames[-1]["terminal_delivery"] is True and frames[-1]["content"] == reply
    assert any(frame.get("event", {}).get("delta") == reply for frame in frames)
    assert private_summary not in json.dumps(frames)
    assert {call.kwargs["ConnectionId"] for call in consumer.ws_router._client.post_to_connection.call_args_list} == {"owner-connection"}
    assert (await trusted(model, "finalize")).json() == terminal
    consumer._process_response(queued(transport)[-1])
    assert len(publication.session(transport)["messages"]) == 1
    assert model.provider.await_count == 2
    await assert_accounted(model, ledger, ["summarize", "call-1"])
    assert (await delivery.invoke(model, operation_id="after-terminal")).status_code == 404


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "interrupted"])
async def test_accounted_call_reaches_each_non_success_owner_outcome(model, ledger, lifecycle, owner, transport, consumer, outcome):
    delivery.streaming_provider(model)
    result = await delivery.invoke(model)
    assert result.status_code == 200 and result.json()["reservation_status"] == "settled"
    if outcome == "failed":
        assert (await data(model, "turn/result", outcome="failed"))["terminal"] is False
    elif outcome == "cancelled":
        cancelled = await owner.client.post(cancellation.PATH, json=cancellation.BODY)
        assert cancelled.status_code == 202
        assert (await delivery.invoke(model, operation_id="after-cancel")).status_code == 404
    terminal = await finish(model, lifecycle)
    assert terminal["outcome"] == outcome and terminal["accounting_status"] == "settled"
    assert terminal["retryable"] is False and terminal["automatic_replay_permitted"] is False
    frames = deliver(transport, consumer)
    assert frames[-1]["status"] == outcome and frames[-1]["accounting_status"] == "settled"
    assert frames[-1]["content"] != delivery.TEXT["text"]
    assert (await trusted(model, "finalize")).json() == terminal
    consumer._process_response(queued(transport)[-1])
    assert len(publication.session(transport)["messages"]) == 1
    model.provider.assert_awaited_once()
    await assert_accounted(model, ledger, ["call-1"])


@pytest.mark.parametrize("failure", ["provider", "ledger", "cancel-during-stream"])
async def test_uncertain_spend_survives_terminal_owner_delivery_without_replay(model, ledger, lifecycle, owner, transport, consumer, failure):
    async def provider(**kwargs):
        await kwargs["on_event"](delivery.TEXT)
        if failure == "provider":
            raise TimeoutError("private provider diagnostics")
        if failure == "cancel-during-stream":
            assert (await owner.client.post(cancellation.PATH, json=cancellation.BODY)).status_code == 202
            await kwargs["on_event"]({**delivery.TEXT, "text": "must not reach the owner"})
        return model.provider.return_value

    model.provider.side_effect = provider
    if failure == "ledger":
        model.service.usage_writer.side_effect = RuntimeError("private ledger diagnostics")
    result = await delivery.invoke(model)
    assert result.status_code == (404 if failure == "cancel-during-stream" else 200), result.text
    if failure != "cancel-during-stream":
        assert result.json()["automatic_replay_permitted"] is False
        assert (await delivery.invoke(model)).json() == result.json()
    terminal = await finish(model, lifecycle)
    assert terminal["outcome"] == ("cancelled" if failure == "cancel-during-stream" else "interrupted")
    assert terminal["accounting_status"] == "unresolved" and terminal["retryable"] is False
    frames = deliver(transport, consumer)
    assert frames[-1]["accounting_status"] == "unresolved"
    assert "private" not in json.dumps(frames) and "must not reach the owner" not in json.dumps(frames)
    assert (await trusted(model, "finalize")).json() == terminal
    assert (await delivery.invoke(model)).status_code == 404
    model.provider.assert_awaited_once()
    assert await usage_rows(ledger) == []
    record = model.service.journal._read(RUN, "call-1")
    assert await ledger.get(BudgetAccountingGap, record["accounting_id"]) is not None
    assert await execution.reserved_amount(model) == Decimal("0.1")
    model.service.event_writer.assert_not_awaited()


async def test_exhausted_owner_budget_delivers_failure_without_provider_or_usage(model, ledger, lifecycle, transport, consumer):
    reserved = await model.reservations.reserve("other-owner-operation", Decimal("1"), [model.target])
    assert reserved.admitted
    result = await delivery.invoke(model)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "rejected" and result.json()["handoff"] == "not_started"
    assert (await delivery.invoke(model)).json() == result.json()
    assert (await data(model, "turn/result", outcome="failed"))["terminal"] is False
    terminal = await finish(model, lifecycle)
    assert terminal["outcome"] == "failed" and terminal["accounting_status"] == "settled"
    frames = deliver(transport, consumer)
    assert len(frames) == 1 and frames[0]["status"] == "failed"
    model.provider.assert_not_awaited()
    assert await usage_rows(ledger) == []
    model.service.event_writer.assert_not_awaited()
    model.service.gap_writer.assert_not_awaited()
    assert await model.redis.hlen(model.target.key()) == 1
