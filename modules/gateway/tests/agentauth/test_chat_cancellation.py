import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import jwt
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import update

from src.agentauth import chat_cancellation
from src.agentauth.execution import ExecutionStatus
from src.agentauth.store import AuthorityStoreError
from src.auth import dependencies
from src.auth.cognito_jwt import CognitoTokenClaims
from src.shared.database import get_db
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from tests.agentauth import test_chat_delivery as delivery_fixtures
from tests.agentauth.test_chat_model_execution import REQUEST, reserved_amount

client = delivery_fixtures.client
model = delivery_fixtures.model
protected_root = delivery_fixtures.protected_root
retained_input_table = delivery_fixtures.retained_input_table
runtime = delivery_fixtures.runtime
store = delivery_fixtures.store
sts = delivery_fixtures.sts
transport = delivery_fixtures.transport
BODY = {"session_id": "session-a", "task_id": "task-a"}
PATH = "/v1/chat/turns/cancel"


@pytest.fixture
async def owner(model, transport, db_session_factory):
    app = FastAPI()
    app.include_router(chat_cancellation.router)
    user = model.context.model_copy(update={"account_type": "human", "auth_source": "jwt"})
    service = chat_cancellation.ChatCancellation(model.runtime[1].store, model.runtime[2], transport.sessions)
    app.dependency_overrides[dependencies.get_current_user] = lambda: user
    app.dependency_overrides[chat_cancellation.cancellation_service] = lambda: service

    async def database():
        async with db_session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.example.test") as http:
        yield SimpleNamespace(client=http, app=app, user=user, service=service)


async def test_owner_cancellation_is_durable_idempotent_intent_not_terminal(owner, model):
    first = await owner.client.post(PATH, json=BODY)
    assert first.status_code == 202 and first.json() == {"status": "cancellation_requested", **BODY}
    assert first.headers["cache-control"] == "no-store"
    authority = model.runtime[1].store.authority
    marker = authority.abort_intent(invocation_id="run-user", tenant_id="tenant")
    assert marker is not None
    assert (await owner.client.post(PATH, json=BODY)).json() == first.json()
    assert authority.abort_intent(invocation_id="run-user", tenant_id="tenant") == marker
    assert authority.load_execution(invocation_id="run-user", tenant_id="tenant").status == ExecutionStatus.ACTIVE
    assert (
        await model.client.post(
            "/v1/chat/model/invoke",
            json={
                "run_id": "run-user",
                "session_id": "session-a",
                "operation_id": "after-cancel",
                "request": REQUEST,
            },
        )
    ).status_code == 404
    model.provider.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": "other"},
        {"org_id": "other"},
        {"account_type": "service"},
        {"auth_source": "iam"},
        {"user_id": "other", "is_admin": True, "attributed_user_id": "human", "attributed_org_id": "tenant"},
    ],
)
async def test_foreign_service_and_attributed_identities_cannot_cancel(owner, model, changes):
    for key, value in changes.items():
        setattr(owner.user, key, value)
    assert (await owner.client.post(PATH, json=BODY)).status_code == 404
    assert model.runtime[1].store.authority.abort_intent(invocation_id="run-user", tenant_id="tenant") is None


async def test_cognito_subject_resolves_to_registered_canonical_owner(owner, db_session_factory):
    async with db_session_factory() as db:
        await db.execute(update(User).where(User.id == "human").values(cognito_sub="signed-subject"))
        await db.commit()
    owner.user.user_id = "signed-subject"
    assert (await owner.client.post(PATH, json=BODY)).status_code == 202


async def test_http_authentication_context_reaches_owner_authorization(owner, db_session_factory, monkeypatch):
    owner.app.dependency_overrides.pop(dependencies.get_current_user)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: db_session_factory)
    now = int(datetime.now(UTC).timestamp())
    validator = Mock()
    validator.validate_token.return_value = CognitoTokenClaims(
        sub="human",
        iss="https://issuer.example.test",
        client_id="browser",
        token_use="access",
        exp=now + 300,
        iat=now,
        org_id="tenant",
        team_id="team",
    )
    monkeypatch.setattr(dependencies, "_get_cognito_validator", lambda: validator)
    response = await owner.client.post(PATH, json=BODY, headers={"Authorization": "Bearer synthetic-browser-login"})
    assert response.status_code == 202
    validator.validate_token.assert_called_once_with("synthetic-browser-login")


async def test_revoked_member_cannot_cancel(owner, model, db_session_factory):
    async with db_session_factory() as db:
        await db.execute(update(TenantMembership).where(TenantMembership.user_id == "human").values(revoked_at=datetime.now(UTC)))
        await db.commit()
    assert (await owner.client.post(PATH, json=BODY)).status_code == 404
    assert model.runtime[1].store.authority.abort_intent(invocation_id="run-user", tenant_id="tenant") is None


@pytest.mark.parametrize("changes", [{"task_id": "old-task"}, {"session_id": "foreign-session"}])
async def test_stale_task_or_foreign_session_does_not_cancel_current_turn(owner, model, changes):
    assert (await owner.client.post(PATH, json={**BODY, **changes})).status_code == 404
    assert model.runtime[1].store.authority.abort_intent(invocation_id="run-user", tenant_id="tenant") is None


@pytest.mark.parametrize("change", ["owner", "context_owner", "foreign_unadmitted", "incarnation", "lease", "ended", "expired"])
async def test_replaced_or_ended_session_refuses_cancellation(owner, model, transport, change):
    header = model.runtime[2].get_item(Key={"PK": "session#session-a", "SK": "header"})["Item"]
    if change == "owner":
        transport.row["owner_principal"] = "foreign"
    elif change in {"context_owner", "foreign_unadmitted"}:
        header["ownerUserId"] = "foreign"
        if change == "foreign_unadmitted":
            header.pop("chatLease")
    elif change == "incarnation":
        transport.row["created_at"] += 1
    elif change == "lease":
        header["chatLease"]["generation"] += 1
    elif change == "ended":
        header["status"] = "ended"
    else:
        header["ttl"] = 1
    transport.sessions.put_item(Item=transport.row)
    model.runtime[2].put_item(Item=header)
    assert (await owner.client.post(PATH, json=BODY)).status_code == 404
    assert model.runtime[1].store.authority.abort_intent(invocation_id="run-user", tenant_id="tenant") is None


async def test_attempt_change_at_write_is_rejected(owner, model, monkeypatch):
    authority = model.runtime[1].store.authority
    record = authority.record_abort_intent

    def replace_attempt(**kwargs):
        model.runtime[1].store.client.update_item(
            TableName=model.runtime[1].store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-user"}},
            UpdateExpression="SET current_attempt = :attempt",
            ExpressionAttributeValues={":attempt": {"N": "2"}},
        )
        return record(**kwargs)

    monkeypatch.setattr(authority, "record_abort_intent", replace_attempt)
    assert (await owner.client.post(PATH, json=BODY)).status_code == 409
    assert authority.abort_intent(invocation_id="run-user", tenant_id="tenant") is None


async def test_lost_cancellation_write_response_is_sanitized_and_retry_is_idempotent(owner, model, monkeypatch):
    authority = model.runtime[1].store.authority
    record = authority.record_abort_intent

    def lost_response(**kwargs):
        record(**kwargs)
        raise AuthorityStoreError("private store detail")

    monkeypatch.setattr(authority, "record_abort_intent", lost_response)
    first = await owner.client.post(PATH, json=BODY)
    assert first.status_code == 503 and "private" not in first.text
    marker = authority.abort_intent(invocation_id="run-user", tenant_id="tenant")
    assert marker is not None
    monkeypatch.setattr(authority, "record_abort_intent", record)
    assert (await owner.client.post(PATH, json=BODY)).status_code == 202
    assert authority.abort_intent(invocation_id="run-user", tenant_id="tenant") == marker


@pytest.mark.parametrize("key", ["run_id", "user_id", "tenant_id", "attempt", "lease_generation", "principal"])
async def test_browser_cannot_choose_execution_or_principal_fields(owner, key):
    assert (await owner.client.post(PATH, json={**BODY, key: "forged"})).status_code == 422


async def test_missing_auth_and_sandbox_capability_are_not_browser_identity(owner, model, monkeypatch):
    owner.app.dependency_overrides.pop(dependencies.get_current_user)
    assert (await owner.client.post(PATH, json=BODY)).status_code == 401
    validator = Mock()
    validator.validate_token.side_effect = jwt.InvalidTokenError("not a Cognito token")
    monkeypatch.setattr(dependencies, "_get_cognito_validator", lambda: validator)
    assert (await owner.client.post(PATH, json=BODY, headers={"Authorization": model.client.headers["Authorization"]})).status_code == 401
    validator.validate_token.assert_called_once()
    assert model.runtime[1].store.authority.abort_intent(invocation_id="run-user", tenant_id="tenant") is None


async def test_disabled_cancellation_does_not_create_authority_clients(owner, monkeypatch):
    owner.app.dependency_overrides.pop(chat_cancellation.cancellation_service)
    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "false")
    factory = Mock()
    monkeypatch.setattr(chat_cancellation, "root_store", factory)
    assert (await owner.client.post(PATH, json=BODY)).status_code == 503
    factory.assert_not_called()


@pytest.mark.parametrize("streamed", [False, True])
async def test_browser_cancel_stops_provider_and_preserves_uncertain_spending(owner, model, transport, streamed):
    started, stopped = asyncio.Event(), asyncio.Event()

    async def provider(**kwargs):
        if streamed:
            await kwargs["on_event"](delivery_fixtures.TEXT)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    model.provider.side_effect = provider
    invocation = asyncio.create_task(
        model.client.post(
            "/v1/chat/model/invoke",
            headers={"Accept": "application/x-ndjson"},
            json={
                "run_id": "run-user",
                "session_id": "session-a",
                "operation_id": "call-1",
                "request": REQUEST,
                "deliver_response": True,
            },
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert (await owner.client.post(PATH, json=BODY)).status_code == 202
        response = await asyncio.wait_for(invocation, 5)
        assert json.loads(response.text.splitlines()[-1])["code"] == "denied"
        assert stopped.is_set()
        operation = model.service.journal._read("run-user", "call-1")
        assert operation["status"] == "unknown" and operation["reservation_status"] == "unknown"
        assert await reserved_amount(model) == Decimal("0.1")
        model.service.usage_writer.assert_not_awaited()
        assert transport.client.send_message.call_count == (3 if streamed else 0)
    finally:
        if not invocation.done():
            invocation.cancel()
        await asyncio.gather(invocation, return_exceptions=True)
