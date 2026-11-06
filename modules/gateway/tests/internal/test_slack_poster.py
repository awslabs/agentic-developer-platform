"""Synthetic approved Slack channel and one-call post transport; no real Slack traffic."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from src.internal import credential_routes, tools_slack
from src.internal import slack_poster as poster
from src.internal.slack_reader import SlackIdentityObservation

TOKEN = "xoxp-private-test-token"
OWNER = "owner-fixture"
TEAM = "T00000001"
USER = "U00000001"
CHANNEL = "C00000001"
MARKER = "7d787d28-5c0e-4c86-b56f-fc13bb332b00"
MESSAGE = "ADP isolated fixture message."
TEXT = f"{MESSAGE} [run:{MARKER}]"
IDENTITY = SlackIdentityObservation(USER, TEAM, ("chat:write", "users:read"))
FIXTURE = {
    "owner_id": OWNER,
    "team_id": TEAM,
    "channel_id": CHANNEL,
    "message": MESSAGE,
    "run_marker": MARKER,
    "approved": True,
    "isolated": True,
    "cleanup_method": "chat.delete",
}


def acknowledgement(**changes):
    return {
        "ok": True,
        "channel": CHANNEL,
        "ts": "1710000000.000001",
        "message": {"user": USER, "text": TEXT, "ts": "1710000000.000001"},
        **changes,
    }


@pytest.fixture
def wire(monkeypatch):
    state = SimpleNamespace(calls=[], responses=[], observed=AsyncMock(return_value=IDENTITY), dns=[])
    monkeypatch.setenv("ADP_TOOLS_SLACK_POST_ENABLED", "true")
    monkeypatch.setenv("ADP_TOOLS_SLACK_POST_FIXTURE", json.dumps(FIXTURE))
    monkeypatch.setattr(poster, "observe_slack_identity", state.observed)
    monkeypatch.setattr(poster, "get_settings", lambda: SimpleNamespace(vault_proxy_require_https=True, vault_proxy_host_allowlist="slack.com"))

    def resolve(host, port):
        state.dns.append((host, port))
        return [(None, None, None, None, ("8.8.8.8", 443))]

    monkeypatch.setattr(credential_routes.socket, "getaddrinfo", resolve)

    async def dispatch(request):
        state.calls.append(request)
        assert request.url.scheme == "https" and request.url.host == "8.8.8.8"
        assert request.url.path == "/api/chat.postMessage" and request.method == "POST"
        assert request.headers["host"] == "slack.com" and request.extensions["sni_hostname"] == "slack.com"
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "channel": CHANNEL,
            "text": TEXT,
            "parse": "none",
            "link_names": False,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        reply = state.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    client_class = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        assert kwargs["timeout"].connect == 3 and kwargs["timeout"].read == 10
        return client_class(transport=httpx.MockTransport(dispatch), **kwargs)

    monkeypatch.setattr(poster.httpx, "AsyncClient", client)
    return state


async def send(wire, *, identity=IDENTITY, owner_id=OWNER, channel_id=CHANNEL, marker=MARKER, text=TEXT):
    fixture = poster.approved_post_fixture(owner_id=OWNER, team_id=TEAM)
    return await poster.post_slack_fixture(
        TOKEN,
        protected_identity=identity,
        fixture=fixture,
        owner_id=owner_id,
        channel_id=channel_id,
        run_marker=marker,
        text=text,
    )


async def test_one_exact_post_has_user_receipt_and_no_multipost_or_unfurl(wire):
    wire.responses.append(httpx.Response(200, json=acknowledgement()))
    result = await send(wire)
    assert result == {"channel_id": CHANNEL, "ts": "1710000000.000001", "user_id": USER, "text": TEXT, "run_marker": MARKER}
    wire.observed.assert_awaited_once_with(TOKEN)
    assert len(wire.calls) == 1 and wire.dns == [("slack.com", None)]
    assert TOKEN not in str(result)


@pytest.mark.parametrize("raw", (None, "{invalid-json", '{"owner_id":"a","owner_id":"b"}'))
async def test_missing_or_ambiguous_private_approval_never_dispatches(wire, monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("ADP_TOOLS_SLACK_POST_FIXTURE")
    else:
        monkeypatch.setenv("ADP_TOOLS_SLACK_POST_FIXTURE", raw)
    with pytest.raises(HTTPException) as refused:
        await send(wire)
    assert refused.value.status_code == 403
    assert wire.calls == [] and wire.observed.await_count == 0


async def test_disallowed_egress_never_sends_token_or_message(wire, monkeypatch):
    monkeypatch.setattr(poster, "get_settings", lambda: SimpleNamespace(vault_proxy_require_https=True, vault_proxy_host_allowlist=""))
    with pytest.raises(HTTPException) as refused:
        await send(wire)
    assert refused.value.status_code == 503
    assert refused.value.detail == {"error": "slack_egress_unavailable"}
    assert wire.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"approved": False},
        {"isolated": False},
        {"cleanup_method": "unapproved"},
        {"channel_id": "D00000001"},
        {"owner_id": "someone-else"},
        {"team_id": "T00000002"},
        {"message": "@here"},
        {"message": ""},
        {"message": MESSAGE, "unexpected": True},
        {"run_marker": str(uuid4())},
    ],
)
async def test_fixture_refuses_unapproved_or_malformed_destinations_before_provider(wire, monkeypatch, change):
    monkeypatch.setenv("ADP_TOOLS_SLACK_POST_FIXTURE", json.dumps({**FIXTURE, **change}))
    with pytest.raises(HTTPException) as refused:
        await send(wire)
    assert refused.value.status_code == 403
    assert wire.calls == [] and wire.observed.await_count == 0


@pytest.mark.parametrize(
    "owner_id,channel_id,marker,text",
    [
        ("someone-else", CHANNEL, MARKER, TEXT),
        (OWNER, "D00000001", MARKER, TEXT),
        (OWNER, "C00000002", MARKER, TEXT),
        (OWNER, CHANNEL, MARKER.upper(), TEXT),
        (OWNER, CHANNEL, MARKER, "ordinary message"),
    ],
)
async def test_call_arguments_cannot_expand_owner_channel_or_message(wire, owner_id, channel_id, marker, text):
    with pytest.raises(HTTPException):
        await send(wire, owner_id=owner_id, channel_id=channel_id, marker=marker, text=text)
    assert wire.calls == [] and wire.observed.await_count == 0


async def test_new_valid_marker_cannot_create_a_second_message_for_same_approved_run(wire):
    marker = str(uuid4())
    with pytest.raises(HTTPException) as refused:
        await send(wire, marker=marker, text=f"{MESSAGE} [run:{marker}]")
    assert refused.value.status_code == 403
    assert wire.calls == [] and wire.observed.await_count == 0


@pytest.mark.parametrize(
    "identity,observed",
    [
        (SlackIdentityObservation(USER, TEAM, ("users:read",)), IDENTITY),
        (IDENTITY, SlackIdentityObservation(USER, TEAM, ("users:read",))),
        (IDENTITY, SlackIdentityObservation("U00000002", TEAM, ("chat:write", "users:read"))),
    ],
)
async def test_revoked_scope_or_changed_user_never_posts(wire, identity, observed):
    wire.observed.return_value = observed
    with pytest.raises(HTTPException) as refused:
        await send(wire, identity=identity)
    assert refused.value.status_code == 403 and wire.calls == []


@pytest.mark.parametrize(
    "reply,code,status",
    [
        (httpx.Response(200, json={"ok": False, "error": "missing_scope"}), "slack_api_error", 403),
        (httpx.Response(200, json={"ok": False, "error": "token_revoked"}), "slack_api_error", 401),
        (httpx.Response(502), "slack_post_uncertain", 503),
        (httpx.ReadError("reply lost"), "slack_post_uncertain", 503),
        (httpx.Response(200, json=acknowledgement(channel="C00000002")), "slack_post_uncertain", 503),
        (httpx.Response(200, json=acknowledgement(message={"user": USER, "text": "wrong"})), "slack_post_uncertain", 503),
        (httpx.ConnectError("private transport failure"), "slack_post_uncertain", 503),
    ],
)
async def test_failed_or_uncertain_reply_is_truthful_and_never_retried(wire, reply, code, status):
    wire.responses.append(reply)
    with pytest.raises(HTTPException) as refused:
        await send(wire)
    assert refused.value.status_code == status and refused.value.detail["error"] == code
    if code == "slack_api_error":
        assert refused.value.detail == {"error": code, "provider_status": 200, "provider_error": reply.json()["error"]}
    if isinstance(reply, httpx.Response) and reply.status_code == 502:
        assert refused.value.detail == {"error": "slack_post_uncertain", "provider_status": 502}
    assert TOKEN not in str(refused.value) and "private transport failure" not in str(refused.value)
    assert len(wire.calls) == 1


@pytest.fixture
def protected_post(wire, monkeypatch):
    claims = {
        "principal": {"user_id": OWNER, "tenant_id": "org-fixture", "team_id": "team-fixture"},
        "slack_connection": {"connection_id": "owned-fixture"},
        "connection_id": "owned-fixture",
        "receipt_id": "one-attempt",
        "expires_at": 3000000000,
    }
    packet = {
        "method": "tools/call",
        "id": 29,
        "params": {
            "name": "post_message",
            "arguments": {
                "channel": CHANNEL,
                "text": TEXT,
                "run_marker": MARKER,
                "adp_assertion": "signed",
            },
        },
    }
    credential = SimpleNamespace(scopes={"oauth_scopes": ["users:read", "chat:write"]})
    binding = SimpleNamespace(provider_account_id=f"{TEAM}:{USER}", observed_scopes=["chat:write", "users:read"])
    receipt = AsyncMock()
    broker = AsyncMock(return_value=TOKEN)
    monkeypatch.setattr(tools_slack, "verify_facade", AsyncMock(return_value=claims))
    monkeypatch.setattr(tools_slack, "current_binding", lambda **kwargs: {"target_status": "READY", "target_id": "fixture"})
    monkeypatch.setattr(tools_slack, "check_fresh", lambda value: None)
    monkeypatch.setattr(tools_slack, "claim_receipt", receipt)
    monkeypatch.setattr(tools_slack, "_identity_access_token", broker)
    monkeypatch.setattr(tools_slack, "get_identity_client", lambda: object())

    @asynccontextmanager
    async def authority(value, runtime, operation="get_user"):
        assert operation == "post_message" and value == claims
        yield credential, binding, SimpleNamespace(id=OWNER)

    monkeypatch.setattr(tools_slack, "current_authority", authority)
    return SimpleNamespace(packet=packet, credential=credential, binding=binding, receipt=receipt, broker=broker)


@pytest.mark.parametrize("provider_error,status", [("missing_scope", 403), ("token_revoked", 401)])
async def test_protected_post_reports_http_200_slack_failure_without_false_success(protected_post, wire, provider_error, status):
    wire.responses.append(httpx.Response(200, json={"ok": False, "error": provider_error}))
    with pytest.raises(HTTPException) as refused:
        await tools_slack.serve(protected_post.packet, SimpleNamespace())
    assert refused.value.status_code == status
    assert refused.value.detail == {"error": "slack_api_error", "provider_status": 200, "provider_error": provider_error}
    assert TOKEN not in str(refused.value)
    assert len(wire.calls) == protected_post.receipt.await_count == protected_post.broker.await_count == 1


async def test_protected_post_refuses_revoked_saved_write_before_broker(protected_post, wire):
    protected_post.credential.scopes = {"oauth_scopes": ["users:read"]}
    with pytest.raises(HTTPException) as refused:
        await tools_slack.serve(protected_post.packet, SimpleNamespace())
    assert refused.value.status_code == 403
    assert wire.calls == []
    protected_post.receipt.assert_not_awaited()
    protected_post.broker.assert_not_awaited()


async def test_protected_post_refuses_vendor_write_revocation_before_post(protected_post, wire):
    wire.observed.return_value = SlackIdentityObservation(USER, TEAM, ("users:read",))
    with pytest.raises(HTTPException) as refused:
        await tools_slack.serve(protected_post.packet, SimpleNamespace())
    assert refused.value.status_code == 403
    assert refused.value.detail == {"error": "slack_write_scope_missing"}
    assert wire.calls == []
    wire.observed.assert_awaited_once_with(TOKEN)
    protected_post.receipt.assert_awaited_once()
    protected_post.broker.assert_awaited_once()


async def test_protected_post_after_prior_message_rejects_every_revoked_write_grant(protected_post, wire, monkeypatch):
    wire.responses.append(httpx.Response(200, json=acknowledgement()))
    response = await tools_slack.serve(protected_post.packet, SimpleNamespace())
    assert json.loads(response.body)["result"]["content"][0]["type"] == "text"
    assert len(wire.calls) == protected_post.receipt.await_count == protected_post.broker.await_count == 1

    marker = str(uuid4())
    text = f"{MESSAGE} [run:{marker}]"
    monkeypatch.setenv("ADP_TOOLS_SLACK_POST_FIXTURE", json.dumps({**FIXTURE, "run_marker": marker}))
    packet = json.loads(json.dumps(protected_post.packet))
    packet["params"]["arguments"]["run_marker"] = marker
    packet["params"]["arguments"]["text"] = text

    protected_post.credential.scopes = {"oauth_scopes": ["users:read"]}
    with pytest.raises(HTTPException) as saved_revoked:
        await tools_slack.serve(packet, SimpleNamespace())
    assert saved_revoked.value.status_code == 403
    protected_post.credential.scopes = {"oauth_scopes": ["users:read", "chat:write"]}

    protected_post.binding.observed_scopes = ["users:read"]
    with pytest.raises(HTTPException) as binding_revoked:
        await tools_slack.serve(packet, SimpleNamespace())
    assert binding_revoked.value.status_code == 403
    protected_post.binding.observed_scopes = ["chat:write", "users:read"]
    assert len(wire.calls) == protected_post.receipt.await_count == protected_post.broker.await_count == 1

    wire.observed.return_value = SlackIdentityObservation(USER, TEAM, ("users:read",))
    with pytest.raises(HTTPException) as vendor_revoked:
        await tools_slack.serve(packet, SimpleNamespace())
    assert vendor_revoked.value.status_code == 403
    assert vendor_revoked.value.detail == {"error": "slack_write_scope_missing"}
    assert len(wire.calls) == 1
    assert protected_post.receipt.await_count == protected_post.broker.await_count == 2
    assert wire.observed.await_count == 2
    assert TOKEN not in str(vendor_revoked.value)
