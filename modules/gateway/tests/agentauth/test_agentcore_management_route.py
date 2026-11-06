"""SCN-03: mounted HTTP management uses the same trusted owner scope as recall."""

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from src.agentauth import chat_data_routes, chat_model
from src.agentauth.agentcore_recall import AgentCoreRecall
from src.auth.dependencies import get_current_user
from src.orchestration import chat_history
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext


@pytest.fixture
async def mounted(monkeypatch):
    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "true")
    monkeypatch.setattr(chat_history, "_is_enabled", lambda _: True)
    monkeypatch.setattr(chat_history, "current_chat_member", AsyncMock(return_value=True))
    user = TokenContext(
        org_id="tenant-a",
        team_id="team-a",
        user_id="owner-a",
        department_id="",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session = {
        "session_id": "session-a",
        "owner_principal": chat_history.owner(user),
        "owner_user_id": user.user_id,
        "user_workspace": "owner-a#webchat",
        "tenant_id": user.org_id,
        "org_id": user.org_id,
        "channel": "webchat",
        "expires_at": int(time.time()) + 600,
    }
    sessions = Mock()
    sessions.get_item.return_value = {"Item": session}
    header = {
        "PK": "session#session-a",
        "SK": "header",
        "orgId": "tenant-a",
        "tenantId": "tenant-a",
        "teamId": "team-a",
        "ownerUserId": "owner-a",
        "memoryPrivacy": "shared",
        "memoryConsent": True,
    }
    context = Mock()
    context.get_item.return_value = {"Item": header}
    provider = AgentCoreRecall(Mock(), memory_id="configured-memory", strategy_id="semantic", namespace_key=b"t" * 32)
    monkeypatch.setattr(chat_model, "agentcore_recall", lambda: provider)
    namespace = provider.owner_namespace(SimpleNamespace(tenant_id=user.org_id, user_id=user.user_id))
    provider.client.create_event.return_value = {
        "event": {"memoryId": "configured-memory", "sessionId": "session-a", "actorId": namespace.rsplit("/", 1)[-1], "eventId": "correction-event"}
    }
    record = {"memoryRecordId": "record-a", "namespaces": [namespace], "memoryStrategyId": "semantic", "content": {"text": "Use short responses"}}
    provider.client.list_memory_records.return_value = {"memoryRecordSummaries": [record]}
    provider.client.get_memory_record.return_value = {"memoryRecord": record}
    app = FastAPI()
    app.include_router(chat_data_routes.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[chat_history.store] = lambda: sessions
    app.dependency_overrides[chat_data_routes.runtime] = lambda: (SimpleNamespace(context_table=context), Mock())
    app.dependency_overrides[get_db] = lambda: Mock()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        yield client, app, user, provider, namespace, context


async def test_same_owner_can_inspect_export_correct_and_forget_via_mounted_routes(mounted):
    client, _, _, provider, namespace, context = mounted
    base = "/chat/sessions/session-a/agentcore-memory"
    read = await client.get(base)
    assert read.status_code == 200, read.text
    assert read.headers["Cache-Control"] == "no-store"
    assert read.json()["status"] == "ok"
    assert [record["text"] for record in read.json()["records"]] == ["Use short responses"]
    provider.client.list_memory_records.assert_called_with(
        memoryId="configured-memory", namespace=namespace, memoryStrategyId="semantic", maxResults=20
    )
    export = await client.get(base + "/export")
    assert export.status_code == 200
    assert export.json()["records"] == read.json()["records"]
    assert export.json()["format"] == "agentcore-memory-v1"
    revision = read.json()["records"][0]["revision"]
    corrected = await client.post(base + "/record-a/correct", json={"revision": revision, "text": "Use detailed responses"})
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["status"] == "pending"
    assert corrected.json()["service_event_id"] == "correction-event"
    provider.client.create_event.assert_called_once()
    event = provider.client.create_event.call_args.kwargs
    assert event["actorId"] == namespace.rsplit("/", 1)[-1]
    assert event["memoryId"] == "configured-memory"
    assert event["payload"][0]["conversational"]["content"]["text"] == "Use detailed responses"
    assert event["metadata"]["adp_receipt"] == {"stringValue": corrected.json()["receipt_id"]}
    forgotten = await client.request("DELETE", base + "/record-a", json={"revision": revision})
    assert forgotten.status_code == 200, forgotten.text
    assert forgotten.json()["status"] == "pending"
    assert forgotten.json()["phase"] == "deletion_requested"
    assert forgotten.json()["receipt_id"] != corrected.json()["receipt_id"]
    provider.client.get_memory_record.assert_called_with(memoryId="configured-memory", namespace=namespace, memoryRecordId="record-a")
    provider.client.delete_memory_record.assert_called_once_with(memoryId="configured-memory", namespace=namespace, memoryRecordId="record-a")
    assert context.get_item.call_count >= 8


async def test_foreign_user_cannot_inspect_or_mutate_mounted_controls(mounted):
    client, app, _, provider, _, _ = mounted
    other = TokenContext(
        org_id="tenant-a",
        team_id="team-a",
        user_id="owner-b",
        department_id="",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    app.dependency_overrides[get_current_user] = lambda: other
    base = "/chat/sessions/session-a/agentcore-memory"
    for response in (
        await client.get(base),
        await client.get(base + "/export"),
        await client.post(base + "/record-a/correct", json={"revision": "a" * 64, "text": "wrong"}),
        await client.request("DELETE", base + "/record-a", json={"revision": "a" * 64}),
    ):
        assert response.status_code == 404, response.text
    provider.client.list_memory_records.assert_not_called()
    provider.client.create_event.assert_not_called()
    provider.client.delete_memory_record.assert_not_called()


async def test_revision_conflict_and_untrusted_service_record_never_mutate(mounted):
    client, _, _, provider, _, _ = mounted
    base = "/chat/sessions/session-a/agentcore-memory"
    stale = await client.post(base + "/record-a/correct", json={"revision": "a" * 64, "text": "new"})
    assert stale.status_code == 409
    provider.client.create_event.assert_not_called()
    provider.client.get_memory_record.return_value["memoryRecord"]["namespaces"] = ["/adp/owners/foreign"]
    denied = await client.request("DELETE", base + "/record-a", json={"revision": "a" * 64})
    assert denied.status_code == 404
    provider.client.delete_memory_record.assert_not_called()
    listing = await client.get(base)
    assert listing.status_code == 200
    assert listing.json() == {"records": [], "status": "partial"}


async def test_export_pages_owner_records_and_reports_truncated_inspect(mounted):
    client, _, _, provider, namespace, _ = mounted
    first = provider.client.list_memory_records.return_value["memoryRecordSummaries"][0]
    second = {**first, "memoryRecordId": "record-b", "content": {"text": "Second preference"}}
    provider.client.list_memory_records.side_effect = [
        {"memoryRecordSummaries": [first], "nextToken": "opaque-next"},
        {"memoryRecordSummaries": [first], "nextToken": "opaque-next"},
        {"memoryRecordSummaries": [second]},
    ]
    base = "/chat/sessions/session-a/agentcore-memory"
    read = await client.get(base)
    assert read.json()["status"] == "partial"
    assert len(read.json()["records"]) == 1
    exported = await client.get(base + "/export")
    assert exported.status_code == 200, exported.text
    assert exported.json()["status"] == "ok"
    assert [record["record_id"] for record in exported.json()["records"]] == ["record-a", "record-b"]
    assert provider.client.list_memory_records.call_args.kwargs["nextToken"] == "opaque-next"
    assert provider.client.list_memory_records.call_args.kwargs["namespace"] == namespace


async def test_missing_agentcore_target_reports_unavailable_not_empty(mounted, monkeypatch):
    client, _, _, provider, _, _ = mounted
    monkeypatch.setattr(chat_model, "agentcore_recall", lambda: None)
    response = await client.get("/chat/sessions/session-a/agentcore-memory")
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "agentcore_memory_unavailable"
    provider.client.list_memory_records.assert_not_called()


@pytest.mark.parametrize("revocation", ["consent", "privacy", "membership", "owner"])
async def test_revoked_owner_cannot_query_or_mutate_agentcore_memory(mounted, monkeypatch, revocation):
    client, _, _, provider, _, context = mounted
    header = context.get_item.return_value["Item"]
    if revocation == "consent":
        header["memoryConsent"] = False
    elif revocation == "privacy":
        header["memoryPrivacy"] = "private"
    elif revocation == "membership":
        monkeypatch.setattr(chat_history, "current_chat_member", AsyncMock(return_value=False))
    else:
        header["ownerUserId"] = "owner-b"
    base = "/chat/sessions/session-a/agentcore-memory"
    responses = [
        await client.get(base),
        await client.get(base + "/export"),
        await client.post(base + "/record-a/correct", json={"revision": "a" * 64, "text": "Other preference"}),
        await client.request("DELETE", base + "/record-a", json={"revision": "a" * 64}),
    ]
    assert [response.status_code for response in responses] == [404] * len(responses)
    assert len({response.json()["detail"]["error"] for response in responses}) == 1
    provider.client.list_memory_records.assert_not_called()
    provider.client.get_memory_record.assert_not_called()
    provider.client.create_event.assert_not_called()
    provider.client.delete_memory_record.assert_not_called()


async def test_revocation_during_query_discards_results_without_exposing_content(mounted):
    client, _, _, provider, _, context = mounted

    def revoke(**kwargs):
        context.get_item.return_value["Item"]["memoryConsent"] = False
        return {
            "memoryRecordSummaries": [
                {
                    "memoryRecordId": "record-a",
                    "namespaces": [kwargs["namespace"]],
                    "memoryStrategyId": "semantic",
                    "content": {"text": "Hidden after revocation"},
                }
            ]
        }

    provider.client.list_memory_records.side_effect = revoke
    response = await client.get("/chat/sessions/session-a/agentcore-memory")
    assert response.status_code == 404
    assert "Hidden after revocation" not in response.text
    assert provider.client.list_memory_records.call_count == 1


@pytest.mark.parametrize("operation", ["correct", "forget"])
async def test_foreign_and_missing_memory_ids_have_the_same_denial_without_mutation(mounted, operation):
    from botocore.exceptions import ClientError

    client, _, _, provider, namespace, _ = mounted
    foreign = {**provider.client.get_memory_record.return_value["memoryRecord"], "namespaces": ["/adp/owners/foreign"]}
    provider.client.get_memory_record.side_effect = [
        {"memoryRecord": foreign},
        ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}}, "GetMemoryRecord"),
    ]
    base = "/chat/sessions/session-a/agentcore-memory"

    async def request(record_id):
        if operation == "correct":
            return await client.post(base + f"/{record_id}/correct", json={"revision": "a" * 64, "text": "Not authorized"})
        return await client.request("DELETE", base + f"/{record_id}", json={"revision": "a" * 64})

    foreign_response = await request("record-a")
    missing_response = await request("record-a")
    assert foreign_response.status_code == missing_response.status_code == 404
    assert foreign_response.json() == missing_response.json()
    assert foreign_response.json()["detail"]["error"] == "chat_scope_refused"
    assert all(call.kwargs["namespace"] == namespace for call in provider.client.get_memory_record.call_args_list)
    provider.client.create_event.assert_not_called()
    provider.client.delete_memory_record.assert_not_called()
