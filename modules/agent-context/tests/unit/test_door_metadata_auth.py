"""Metadata-only delegation uses the real signed native MCP transport."""

# ruff: noqa: F811

import base64
import hashlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from door import mcp_app, server
from tests.unit.test_door_auth import identity  # noqa: F401

pytestmark = pytest.mark.door_auth


@pytest.fixture
def isolated_door(monkeypatch):
    monkeypatch.setattr(mcp_app.mcp_server, "_session_manager", None)
    mount = next(route for route in server.app.routes if getattr(route, "path", None) == "/mcp")
    monkeypatch.setattr(mount, "app", mcp_app.mcp_server.streamable_http_app())
    acl = SimpleNamespace(get_allowed_repos=lambda caller: {"fixture/repo"})
    monkeypatch.setattr(server.state, "acl_store", acl)
    for field in (
        "zoekt",
        "s3_client",
        "db_pool",
        "semantic_code_store",
        "semantic_http_client",
        "experience_tool",
    ):
        monkeypatch.setattr(server.state, field, None)
    monkeypatch.setattr(server.config, "project_filter_enabled", True)
    monkeypatch.setattr(server.config, "semantic_enabled", True)
    monkeypatch.setattr(server.config, "s3_bucket", "fixture-bucket")
    return acl


def rpc_result(response):
    assert response.status_code == 200
    if response.headers["content-type"].startswith("text/event-stream"):
        packets = [
            json.loads(line[5:].strip())
            for line in response.text.splitlines()
            if line.startswith("data:")
        ]
        assert len(packets) == 1
        return packets[0]["result"]
    return response.json()["result"]


def metadata_token(
    key,
    body,
    *,
    audience="adp-knowledge-catalogue",
    path="/mcp/",
    method="POST",
    issued=None,
    expires=None,
):
    now = int(time.time())
    claims = {
        "iss": "adp-gateway",
        "aud": audience,
        "kid": "test",
        "method": method,
        "path": path,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "iat": now if issued is None else issued,
        "exp": now + 30 if expires is None else expires,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    signed = b"adpdm1." + encoded
    return (signed + b"." + base64.urlsafe_b64encode(key.sign(signed)).rstrip(b"=")).decode()


async def test_signed_metadata_list_returns_actual_schemas_without_data_identity(
    identity, isolated_door
):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
    async with (
        mcp_app.mcp_server.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://localhost"
        ) as client,
    ):
        response = await client.post(
            "/mcp/",
            content=body,
            headers={
                "x-adp-door-catalogue": metadata_token(identity, body),
                "x-owner-sub": "forged-user",
                "x-github-login": "forged-login",
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
        )
    assert {tool["name"] for tool in rpc_result(response)["tools"]} == {
        "search",
        "understand",
        "impact",
        "browse",
        "remember",
        "experience",
        "secure",
    }
    assert "forged" not in response.text


@pytest.mark.parametrize(
    "change",
    [
        "call",
        "rest",
        "audience",
        "expired",
        "body",
        "query",
        "identity_header",
        "duplicate",
        "cursor",
    ],
)
async def test_metadata_token_can_never_authorize_data_or_other_requests(
    identity, isolated_door, change
):
    packet = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    path = "/mcp/"
    if change == "call":
        packet.update(
            method="tools/call", params={"name": "search", "arguments": {"query": "private"}}
        )
    if change == "cursor":
        packet["params"] = {"cursor": "unreviewed"}
    if change == "rest":
        path = "/tools"
    body = json.dumps(packet).encode()
    token = metadata_token(
        identity,
        body,
        audience="adp-knowledge-door" if change == "audience" else "adp-knowledge-catalogue",
        path=path,
        method="GET" if change == "rest" else "POST",
        issued=1 if change == "expired" else None,
        expires=31 if change == "expired" else None,
    )
    headers = [
        ("x-adp-door-catalogue", token),
        ("accept", "application/json, text/event-stream"),
        ("content-type", "application/json"),
    ]
    if change == "identity_header":
        headers.append(("x-adp-door-identity", token))
    if change == "duplicate":
        headers.append(("x-adp-door-catalogue", token))
    async with (
        mcp_app.mcp_server.session_manager.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://localhost"
        ) as client,
    ):
        response = await client.request(
            "GET" if change == "rest" else "POST",
            path + ("?tenant=foreign" if change == "query" else ""),
            content=b"{}" if change == "body" else body,
            headers=headers,
        )
    assert response.status_code == 401


@pytest.mark.parametrize("authority", ("none", "forged_headers", "legacy_key", "catalogue"))
async def test_direct_knowledge_call_is_denied_before_mounted_backend(
    identity, isolated_door, monkeypatch, authority
):
    packet = {
        "jsonrpc": "2.0",
        "id": "fixture-request",
        "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": "private-sentinel"}},
    }
    body = json.dumps(packet).encode()
    calls = []
    mount = next(route for route in server.app.routes if getattr(route, "path", None) == "/mcp")

    async def backend(scope, receive, send):
        calls.append(scope)
        raise AssertionError("unauthenticated request reached mounted MCP backend")

    monkeypatch.setattr(mount, "app", backend)
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    if authority == "forged_headers":
        headers.update(
            {
                "x-owner-sub": "fixture-owner",
                "x-tenant-id": "fixture-tenant",
                "x-github-login": "fixture-user",
                "x-adp-run-service": "true",
            }
        )
    elif authority == "legacy_key":
        headers.update(
            {"x-internal-api-key": "fixture-legacy-key", "x-tenant-id": "fixture-tenant"}
        )
    elif authority == "catalogue":
        headers["x-adp-door-catalogue"] = metadata_token(identity, body)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://localhost"
    ) as client:
        response = await client.post("/mcp/", content=body, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert not calls
