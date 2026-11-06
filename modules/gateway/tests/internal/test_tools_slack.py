"""Protected Slack read can never become an unbound or write-capable target."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal import tools_slack
from src.internal.slack_poster import ApprovedPostFixture
from src.shared.models.base import Base
from src.shared.models.vault import OAuthToolReceipt

CLAIMS = {
    "principal": {"user_id": "user-alice", "tenant_id": "org-acme", "team_id": "team-eng"},
    "slack_connection": {"connection_id": "owned-connection"},
    "connection_id": "owned-connection",
    "receipt_id": "receipt-one",
    "expires_at": 3000000000,
}
PACKET = {
    "method": "tools/call",
    "id": 17,
    "params": {
        "name": "get_user",
        "arguments": {"user": "U00000002", "adp_assertion": "signed"},
    },
}


MARKER = "7d787d28-5c0e-4c86-b56f-fc13bb332b00"
TEXT = f"ADP isolated fixture message. [run:{MARKER}]"
POST_PACKET = {
    "method": "tools/call",
    "id": 29,
    "params": {
        "name": "post_message",
        "arguments": {
            "channel": "C00000001",
            "text": TEXT,
            "run_marker": MARKER,
            "adp_assertion": "signed",
        },
    },
}


@pytest.fixture
async def receipts(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tools_slack, "get_session_factory", lambda: factory)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_signed_write_uses_current_binding_and_durable_marker_before_one_post(receipts, monkeypatch):
    claims = {**CLAIMS, "receipt_id": "receipt-post-1"}
    verify = AsyncMock(return_value=claims)
    monkeypatch.setattr(tools_slack, "verify_facade", verify)
    monkeypatch.setattr(tools_slack, "check_fresh", lambda value: None)
    monkeypatch.setattr(tools_slack, "current_binding", lambda **kwargs: {"target_status": "READY", "target_id": "target-one"})
    monkeypatch.setattr(tools_slack, "get_identity_client", Mock(return_value=object()))
    fixture = ApprovedPostFixture("user-alice", "T00000001", "C00000001", "ADP isolated fixture message.", MARKER)
    monkeypatch.setattr(tools_slack, "approved_post_fixture", lambda **kwargs: fixture)
    broker = AsyncMock(return_value="synthetic-private-token")
    provider = AsyncMock(
        return_value={
            "channel_id": "C00000001",
            "user_id": "U00000001",
            "text": TEXT,
            "run_marker": MARKER,
            "ts": "1710000000.000001",
        }
    )
    monkeypatch.setattr(tools_slack, "_identity_access_token", broker)
    monkeypatch.setattr(tools_slack, "post_slack_fixture", provider)
    checks = []

    @asynccontextmanager
    async def authority(value, runtime, operation="get_user"):
        checks.append(operation)
        assert value["slack_connection"]["connection_id"] == value["connection_id"]
        yield (
            SimpleNamespace(id="owned-connection", scopes={"oauth_scopes": ["users:read", "chat:write"]}),
            SimpleNamespace(provider_account_id="T00000001:U00000001", observed_scopes=["chat:write", "users:read"]),
            SimpleNamespace(id="user-alice"),
        )

    monkeypatch.setattr(tools_slack, "current_authority", authority)
    result = await tools_slack.serve(POST_PACKET, SimpleNamespace())
    assert json.loads(result.body)["result"]["content"] == [{"type": "text", "text": json.dumps(provider.return_value)}]
    verify.assert_awaited_once_with(
        "signed",
        "post_message",
        {
            "channel": "C00000001",
            "text": TEXT,
            "run_marker": MARKER,
        },
        29,
        namespace="slack",
    )
    assert checks == ["post_message"] * 3 and broker.await_count == provider.await_count == 1
    async with receipts() as db:
        assert len((await db.scalars(select(OAuthToolReceipt))).all()) == 2
    claims["receipt_id"] = "receipt-post-2"
    with pytest.raises(HTTPException) as duplicate:
        await tools_slack.serve(POST_PACKET, SimpleNamespace())
    assert duplicate.value.status_code == 409
    claims["receipt_id"] = "receipt-post-3"
    claims["connection_id"] = "reconnected-owner"
    claims["slack_connection"] = {"connection_id": "reconnected-owner"}
    with pytest.raises(HTTPException) as reconnected:
        await tools_slack.serve(POST_PACKET, SimpleNamespace())
    assert reconnected.value.status_code == 409
    assert broker.await_count == provider.await_count == 1


@pytest.mark.asyncio
async def test_wrong_post_destination_never_claims_receipt_or_exchanges_token(receipts, monkeypatch):
    monkeypatch.setattr(tools_slack, "verify_facade", AsyncMock(return_value=CLAIMS))
    monkeypatch.setattr(tools_slack, "check_fresh", lambda value: None)
    monkeypatch.setattr(tools_slack, "current_binding", lambda **kwargs: {"target_status": "READY"})
    monkeypatch.setattr(
        tools_slack,
        "approved_post_fixture",
        lambda **kwargs: ApprovedPostFixture(
            "user-alice",
            "T00000001",
            "C00000001",
            "ADP isolated fixture message.",
            MARKER,
        ),
    )
    broker, provider = AsyncMock(), AsyncMock()
    monkeypatch.setattr(tools_slack, "_identity_access_token", broker)
    monkeypatch.setattr(tools_slack, "post_slack_fixture", provider)

    @asynccontextmanager
    async def authority(value, runtime, operation="get_user"):
        yield (
            SimpleNamespace(id="owned-connection", scopes={"oauth_scopes": ["users:read", "chat:write"]}),
            SimpleNamespace(provider_account_id="T00000001:U00000001", observed_scopes=["chat:write", "users:read"]),
            SimpleNamespace(id="user-alice"),
        )

    monkeypatch.setattr(tools_slack, "current_authority", authority)
    invalid = {
        **POST_PACKET,
        "params": {
            "name": "post_message",
            "arguments": {
                **POST_PACKET["params"]["arguments"],
                "channel": "D00000001",
            },
        },
    }
    with pytest.raises(HTTPException) as refused:
        await tools_slack.serve(invalid, SimpleNamespace())
    assert refused.value.status_code == 403
    broker.assert_not_awaited()
    provider.assert_not_awaited()
    async with receipts() as db:
        assert (await db.scalars(select(OAuthToolReceipt))).all() == []


@pytest.mark.asyncio
async def test_unbound_call_or_post_cannot_claim_receipt_or_reach_provider(monkeypatch):
    monkeypatch.setattr(tools_slack, "current_binding", lambda **kwargs: {"target_status": "READY"})
    monkeypatch.setattr(tools_slack, "verify_facade", AsyncMock(return_value={**CLAIMS, "slack_connection": {}}))
    receipt = AsyncMock()
    provider = AsyncMock()
    monkeypatch.setattr(tools_slack, "claim_receipt", receipt)
    monkeypatch.setattr(tools_slack, "read_slack_user", provider)
    post_provider = AsyncMock()
    monkeypatch.setattr(tools_slack, "post_slack_fixture", post_provider)
    with pytest.raises(HTTPException) as refused:
        await tools_slack.serve(PACKET, SimpleNamespace())
    assert refused.value.status_code == 403
    with pytest.raises(HTTPException) as refused:
        await tools_slack.serve(POST_PACKET, SimpleNamespace())
    assert refused.value.status_code == 403
    receipt.assert_not_awaited()
    provider.assert_not_awaited()
    post_provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_bound_read_claims_receipt_before_broker_and_returns_only_requested_profile(monkeypatch):
    calls = []
    verify = AsyncMock(return_value=CLAIMS)
    monkeypatch.setattr(tools_slack, "verify_facade", verify)
    monkeypatch.setattr(tools_slack, "check_fresh", lambda claims: None)
    monkeypatch.setattr(tools_slack, "current_binding", lambda **kwargs: {"target_status": "READY", "target_id": "approved-target"})
    monkeypatch.setattr(tools_slack, "get_identity_client", Mock(return_value=object()))

    @asynccontextmanager
    async def authority(claims, runtime):
        calls.append("current-authority")
        assert claims["slack_connection"]["connection_id"] == claims["connection_id"]
        yield (
            SimpleNamespace(id="owned-connection"),
            SimpleNamespace(provider_account_id="T00000001:U00000001", observed_scopes=["users:read"]),
            SimpleNamespace(id="user-alice"),
        )

    async def receipt(claims):
        calls.append("receipt")

    async def broker(credential, owner, client):
        assert credential.id == "owned-connection" and owner.id == "user-alice"
        calls.append("broker")
        return "synthetic-private-token"

    async def read(token, *, protected_identity, user_id):
        assert token == "synthetic-private-token" and user_id == "U00000002"
        assert (protected_identity.team_id, protected_identity.user_id, protected_identity.scopes) == ("T00000001", "U00000001", ("users:read",))
        calls.append("provider")
        return {"id": user_id, "name": "fixture-reader"}

    monkeypatch.setattr(tools_slack, "current_authority", authority)
    monkeypatch.setattr(tools_slack, "claim_receipt", receipt)
    monkeypatch.setattr(tools_slack, "_identity_access_token", broker)
    monkeypatch.setattr(tools_slack, "read_slack_user", read)
    response = await tools_slack.serve(PACKET, SimpleNamespace())
    assert json.loads(response.body)["result"]["content"] == [{"type": "text", "text": '{"id": "U00000002", "name": "fixture-reader"}'}]
    verify.assert_awaited_once_with("signed", "get_user", {"user": "U00000002"}, 17, namespace="slack")
    assert calls == ["current-authority", "receipt", "broker", "current-authority", "provider", "current-authority"]


@pytest.mark.asyncio
async def test_disconnect_after_broker_prevents_provider_read(monkeypatch):
    monkeypatch.setattr(tools_slack, "verify_facade", AsyncMock(return_value=CLAIMS))
    monkeypatch.setattr(tools_slack, "check_fresh", lambda claims: None)
    monkeypatch.setattr(tools_slack, "current_binding", lambda **kwargs: {"target_status": "READY"})
    monkeypatch.setattr(tools_slack, "claim_receipt", AsyncMock())
    monkeypatch.setattr(tools_slack, "_identity_access_token", AsyncMock(return_value="synthetic-private-token"))
    monkeypatch.setattr(tools_slack, "get_identity_client", Mock(return_value=object()))
    provider = AsyncMock()
    monkeypatch.setattr(tools_slack, "read_slack_user", provider)
    checks = []

    @asynccontextmanager
    async def authority(claims, runtime):
        checks.append(claims["connection_id"])
        if len(checks) != 1:
            raise HTTPException(403, "Slack connection changed")
        yield (
            SimpleNamespace(id="owned-connection"),
            SimpleNamespace(provider_account_id="T00000001:U00000001", observed_scopes=["users:read"]),
            SimpleNamespace(id="user-alice"),
        )

    monkeypatch.setattr(tools_slack, "current_authority", authority)
    with pytest.raises(HTTPException) as refused:
        await tools_slack.serve(PACKET, SimpleNamespace())
    assert refused.value.status_code == 403 and checks == ["owned-connection", "owned-connection"]
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_discovery_only_advertises_read(monkeypatch):
    monkeypatch.setattr(tools_slack, "current_binding", lambda **kwargs: {"target_status": "READY"})
    monkeypatch.setenv("ADP_TOOLS_SLACK_TARGET_ID", "approved-target")
    published = Mock()
    monkeypatch.setattr(tools_slack, "publish_catalogue", published)
    response = await tools_slack.serve(
        {"method": "tools/list", "id": 19, "params": {}}, SimpleNamespace(store=SimpleNamespace(client=object(), table="table"))
    )
    tools = json.loads(response.body)["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["get_user"]
    assert set(tools[0]["inputSchema"]["properties"]) == {"user", "adp_assertion"}
    published.assert_called_once()


@pytest.mark.asyncio
async def test_private_discovery_publishes_post_only_for_approved_pinned_fixture(monkeypatch):
    monkeypatch.setattr(tools_slack, "current_binding", lambda **kwargs: {"target_status": "READY", "target_id": "approved-target"})
    monkeypatch.setenv("ADP_TOOLS_SLACK_TARGET_ID", "approved-target")
    monkeypatch.setenv("ADP_TOOLS_SLACK_POST_ENABLED", "true")
    monkeypatch.setenv(
        "ADP_TOOLS_SLACK_POST_FIXTURE",
        json.dumps(
            {
                "owner_id": "user-alice",
                "team_id": "T00000001",
                "channel_id": "C00000001",
                "message": "ADP isolated fixture message.",
                "run_marker": MARKER,
                "approved": True,
                "isolated": True,
                "cleanup_method": "chat.delete",
            }
        ),
    )
    publish = Mock()
    monkeypatch.setattr(tools_slack, "publish_catalogue", publish)
    runtime = SimpleNamespace(store=SimpleNamespace(client=object(), table="table"))
    result = await tools_slack.serve({"method": "tools/list", "id": 3, "params": {}}, runtime)
    names = json.loads(result.body)["result"]["tools"]
    assert [tool["name"] for tool in names] == ["get_user", "post_message"]
    assert set(names[1]["inputSchema"]["properties"]) == {"channel", "text", "run_marker", "adp_assertion"}
    assert [tool["name"] for tool in publish.call_args.kwargs["tools"]] == ["get_user", "post_message"]
    monkeypatch.setenv("ADP_TOOLS_SLACK_POST_ENABLED", "false")
    denied = await tools_slack.serve({"method": "tools/list", "id": 4, "params": {}}, runtime)
    assert [tool["name"] for tool in json.loads(denied.body)["result"]["tools"]] == ["get_user"]


@pytest.mark.asyncio
async def test_lost_post_reply_cannot_replay_after_receipt_expiry_cleanup_or_reconnect(receipts, monkeypatch):
    claims = {**CLAIMS, "receipt_id": "uncertain-attempt"}
    monkeypatch.setattr(tools_slack, "verify_facade", AsyncMock(return_value=claims))
    monkeypatch.setattr(tools_slack, "check_fresh", lambda value: None)
    monkeypatch.setattr(tools_slack, "current_binding", lambda **kwargs: {"target_status": "READY", "target_id": "fixture"})
    monkeypatch.setattr(tools_slack, "get_identity_client", Mock(return_value=object()))
    fixture = ApprovedPostFixture("user-alice", "T00000001", "C00000001", "ADP isolated fixture message.", MARKER)
    monkeypatch.setattr(tools_slack, "approved_post_fixture", lambda **kwargs: fixture)
    broker = AsyncMock(return_value="synthetic-private-token")
    provider = AsyncMock(side_effect=HTTPException(503, {"error": "slack_post_uncertain"}))
    monkeypatch.setattr(tools_slack, "_identity_access_token", broker)
    monkeypatch.setattr(tools_slack, "post_slack_fixture", provider)

    @asynccontextmanager
    async def authority(value, runtime, operation="get_user"):
        assert operation == "post_message"
        yield (
            SimpleNamespace(scopes={"oauth_scopes": ["users:read", "chat:write"]}),
            SimpleNamespace(provider_account_id="T00000001:U00000001", observed_scopes=["chat:write", "users:read"]),
            SimpleNamespace(id="user-alice"),
        )

    monkeypatch.setattr(tools_slack, "current_authority", authority)
    with pytest.raises(HTTPException) as uncertain:
        await tools_slack.serve(POST_PACKET, SimpleNamespace())
    assert uncertain.value.status_code == 503 and uncertain.value.detail == {"error": "slack_post_uncertain"}
    assert provider.await_count == broker.await_count == 1

    async with receipts() as db, db.begin():
        stored = (await db.scalars(select(OAuthToolReceipt))).all()
        assert len(stored) == 2 and {row.expires_at.year for row in stored} == {2065, 9999}
        await db.execute(
            delete(OAuthToolReceipt)
            .where(OAuthToolReceipt.expires_at < datetime(2090, 1, 1, tzinfo=UTC))
            .execution_options(synchronize_session=False)
        )
    async with receipts() as db:
        survivors = (await db.scalars(select(OAuthToolReceipt))).all()
        assert len(survivors) == 1 and survivors[0].expires_at.year == 9999

    claims["receipt_id"] = "new-signed-attempt"
    claims["expires_at"] = 4102444800
    claims["connection_id"] = "reconnected-owner"
    claims["slack_connection"] = {"connection_id": "reconnected-owner"}
    claims["principal"] = {**claims["principal"], "tenant_id": "new-tenant", "team_id": "new-team"}
    with pytest.raises(HTTPException) as replay:
        await tools_slack.serve(POST_PACKET, SimpleNamespace())
    assert replay.value.status_code == 409
    assert provider.await_count == broker.await_count == 1
    async with receipts() as db:
        assert len((await db.scalars(select(OAuthToolReceipt))).all()) == 1
