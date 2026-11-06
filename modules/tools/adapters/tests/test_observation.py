"""Exercise signed observation through the packaged adapter route and MCP method boundary."""

import asyncio
import hashlib
import hmac
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from tool_adapters import observation
from tool_adapters.atlassian_observe import collect_observation
from tool_adapters.config import AdapterConfiguration
from tool_adapters.server import create_app


def test_provider_observation_requires_pinned_schemas_and_two_real_method_replies(record_property):
    methods = []
    schema_hash = hashlib.sha256(b"{}").hexdigest()

    class Transport:
        def __init__(self, *, deadline):
            assert deadline > time.monotonic()

        def send(self, method, params, token):
            assert token == "transient-token-sentinel"
            methods.append((method, params))
            if method == "initialize":
                return {"result": {"protocolVersion": "2025-03-26"}}
            if method == "notifications/initialized":
                return None
            if method == "tools/list":
                return {
                    "result": {
                        "tools": [
                            {"name": "atlassianUserInfo", "inputSchema": {}},
                            {"name": "getAccessibleAtlassianResources", "inputSchema": {}},
                        ]
                    }
                }
            if params["name"] == "atlassianUserInfo":
                return {"result": {"structuredContent": {"accountId": "subject-1"}}}
            return {
                "result": {
                    "structuredContent": {
                        "resources": [
                            {"id": "site-1", "scopes": ["read:jira:agent-interface", "search:jira:agent-interface"]},
                            {
                                "id": "site-2",
                                "scopes": ["read:confluence:agent-interface", "search:confluence:agent-interface"],
                            },
                        ]
                    }
                }
            }

    settings = SimpleNamespace(subject_schema_sha256=schema_hash, sites_schema_sha256=schema_hash)
    subject, sites, scopes = collect_observation("transient-token-sentinel", settings, transport_factory=Transport)
    assert subject == "subject-1" and [site.site_id for site in sites] == ["site-1", "site-2"]
    assert "read:jira:agent-interface" in scopes and "read:confluence:agent-interface" in scopes
    assert [method for method, _ in methods] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
        "tools/call",
    ]
    first_methods = [method for method, _ in methods]
    settings.sites_schema_sha256 = "a" * 64

    with pytest.raises(ValueError, match="unverified provider schema"):
        collect_observation("transient-token-sentinel", settings, transport_factory=Transport)
    record_property(
        "identity_observation",
        json.dumps(
            {
                "methods": first_methods,
                "site_count": len(sites),
                "subject": subject,
                "schema_rejected": True,
            }
        ),
    )


def test_adapter_rejects_forged_role_and_purpose_before_identity(configuration, release, monkeypatch, record_property):
    import tool_adapters.observation as observation

    monitor = MagicMock()
    monkeypatch.setattr(observation.boto3, "client", lambda name: monitor)
    configured = AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "observation",
            "release": release,
            "observation": {
                "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
                "core_role_arn": "arn:aws:iam::123456789012:role/adp-core",
                "assertion_key_arn": "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
                "edge_provenance_sha256": hashlib.sha256(b"edge-fixture").hexdigest(),
                "identity_workload": "adp-tools-dev-atlassian-mcp-oauth-user",
                "identity_provider": "adp-tools-dev-atlassian-mcp-user",
                "config_revision": "a" * 64,
                "subject_schema_sha256": "b" * 64,
                "sites_schema_sha256": "c" * 64,
            },
        }
    )
    now = int(time.time() * 1000)
    body = {
        "schema_version": "1.0",
        "purpose": "connection_observe",
        "connector": "atlassian",
        "binding": {
            "operation_id": "attempt-1",
            "receipt_id": "attempt-1",
            "payload_sha256": "c" * 64,
            "release": release,
        },
        "attempt_id": "attempt-1",
        "config_revision": "a" * 64,
        "signed_assertion": "untrusted",
        "deadline_at_ms": now + 5000,
    }
    with TestClient(create_app(configured, source_revision="a" * 40)) as client:
        assert client.post("/internal/v1/tool-adapters/observe", json=body).status_code == 403
        wrong_role = {
            "x-adp-edge-provenance": "edge-fixture",
            "x-caller-identity": "arn:aws:iam::123456789012:role/other",
        }
        assert client.post("/internal/v1/tool-adapters/observe", json=body, headers=wrong_role).status_code == 403
        body["purpose"] = "tool_call"
        refused = client.post("/internal/v1/tool-adapters/observe", json=body, headers=wrong_role)
        assert refused.status_code in {403, 422}
    monitor.get_workload_access_token_for_user_id.assert_not_called()
    record_property(
        "identity_observation",
        json.dumps(
            {
                "forged_role_status": 403,
                "wrong_purpose_status": refused.status_code,
                "identity_calls": monitor.get_workload_access_token_for_user_id.call_count,
            }
        ),
    )


@pytest.mark.parametrize("change", ["unchanged", "reconnect", "disconnect", "substituted"])
def test_observation_rechecks_owner_after_identity_refresh_before_provider(
    monkeypatch, release, change, record_property
):
    core_url = "https://core.example.test/internal/v1/tool-adapters/authorize"
    core_role = "arn:aws:iam::123456789012:role/adp-core"
    key = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"
    configured = AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "observation",
            "release": release,
            "observation": {
                "core_authorize_url": core_url,
                "core_role_arn": core_role,
                "assertion_key_arn": key,
                "edge_provenance_sha256": hashlib.sha256(b"edge-fixture").hexdigest(),
                "identity_workload": "adp-tools-dev-atlassian-mcp-oauth-user",
                "identity_provider": "adp-tools-dev-atlassian-mcp-user",
                "config_revision": "a" * 64,
                "subject_schema_sha256": "b" * 64,
                "sites_schema_sha256": "c" * 64,
            },
        }
    )
    deadline = int(time.time() * 1000) + 15000
    binding = {
        "operation_id": "attempt-1",
        "receipt_id": "attempt-1",
        "payload_sha256": "c" * 64,
        "release": release,
    }
    claims = {
        "audience": "tool-adapters:observe",
        "caller_role_arn": core_role,
        "purpose": "connection_observe",
        "user_id": "alice",
        "tenant_id": "org-acme",
        "attempt_id": "attempt-1",
        "config_revision": "a" * 64,
        "binding": binding,
        "deadline_at_ms": deadline,
    }
    signed = observation.canonical(claims)
    signature = observation.base64.urlsafe_b64encode(hmac.digest(b"fixture", signed, "sha256")).decode().rstrip("=")
    body = {
        "schema_version": "1.0",
        "purpose": "connection_observe",
        "connector": "atlassian",
        "binding": binding,
        "attempt_id": "attempt-1",
        "config_revision": "a" * 64,
        "signed_assertion": observation.base64.urlsafe_b64encode(
            observation.canonical(
                {
                    "claims": claims,
                    "signature": signature,
                }
            )
        )
        .decode()
        .rstrip("="),
        "deadline_at_ms": deadline,
    }
    entered = threading.Event()
    resume = threading.Event()
    identity = MagicMock()
    identity.get_workload_access_token_for_user_id.return_value = {"workloadAccessToken": "private-alice"}

    def refreshed(**kwargs):
        assert kwargs["workloadIdentityToken"] == "private-alice"
        assert kwargs["resourceCredentialProviderName"] == "adp-tools-dev-atlassian-mcp-user"
        assert kwargs["oauth2Flow"] == "USER_FEDERATION"
        entered.set()
        if not resume.wait(5):
            raise TimeoutError("Identity fixture timed out")
        return {"sessionStatus": "COMPLETED", "accessToken": "transient-token-sentinel"}

    identity.get_resource_oauth2_token.side_effect = refreshed
    kms = MagicMock()
    kms.verify.side_effect = lambda **kwargs: {
        "SignatureValid": (
            kwargs["KeyId"] == key
            and hmac.compare_digest(kwargs["Signature"], hmac.digest(b"fixture", kwargs["Message"], "sha256"))
        )
    }
    monkeypatch.setattr(
        observation.boto3,
        "client",
        lambda service, **kwargs: {
            "kms": kms,
            "bedrock-agentcore": identity,
        }[service],
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    provider = AsyncMock(return_value=("subject-alice", ({"site_id": "site-1", "scopes": ("read:me",)},), ("read:me",)))
    monkeypatch.setattr(observation, "observe_provider", provider)
    core_calls = []
    state = {"current": True}
    native_post = httpx.AsyncClient.post

    async def core_post(self, url, **kwargs):
        if url != core_url:
            assert url == "/internal/v1/tool-adapters/observe"
            return await native_post(self, url, **kwargs)
        core_calls.append(json.loads(kwargs["content"]))
        if state["current"] in {"reconnect", "disconnect"}:
            return httpx.Response(403, json={"detail": "attempt superseded"})
        now = int(time.time() * 1000)
        owner = "bob" if state["current"] == "substituted" else "alice"
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "admission_id": f"admission-{len(core_calls)}",
                "binding": binding,
                "purpose": "connection_observe",
                "connector": "atlassian",
                "operation": "observe",
                "identity": {
                    "workload": configured.observation.identity_workload,
                    "provider": configured.observation.identity_provider,
                    "canonical_user_id": owner,
                },
                "context": {
                    "tenant_id": "org-acme",
                    "canonical_user_id": owner,
                    "connection_id": None,
                    "binding_revision": None,
                    "site_id": None,
                    "scopes": ["read:me"],
                },
                "issued_at_ms": now,
                "expires_at_ms": min(now + 5000, deadline),
                "deadline_at_ms": deadline,
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", core_post)

    async def run():
        app = create_app(configured, source_revision="a" * 40)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://adapter.example.test") as client:
            pending = asyncio.create_task(
                client.post(
                    "/internal/v1/tool-adapters/observe",
                    json=body,
                    headers={
                        "x-caller-identity": core_role.replace(":iam:", ":sts:").replace(":role/", ":assumed-role/")
                        + "/core-session",
                        "x-adp-edge-provenance": "edge-fixture",
                    },
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                if change != "unchanged":
                    state["current"] = change
            finally:
                resume.set()
            return await asyncio.wait_for(pending, 5)

    result = asyncio.run(run())
    assert len(core_calls) == 2
    assert all(call["signed_assertion"] == body["signed_assertion"] for call in core_calls)
    assert identity.get_workload_access_token_for_user_id.call_args.kwargs["userId"] == "alice"
    assert identity.get_resource_oauth2_token.call_count == 1
    assert "sentinel" not in result.text
    if change == "unchanged":
        assert result.status_code == 200, result.text
        provider.assert_awaited_once_with("transient-token-sentinel", configured.observation)
    else:
        assert result.status_code == (403 if change == "substituted" else 503), result.text
        provider.assert_not_awaited()
    record_property(
        "identity_observation",
        json.dumps(
            {
                "change": change,
                "status": result.status_code,
                "owner": claims["user_id"],
                "attempt": body["attempt_id"],
                "generation": release["group_generation"],
                "core_checks": len(core_calls),
                "identity_refreshes": identity.get_resource_oauth2_token.call_count,
                "provider_calls": provider.await_count,
                "token_reflected": "sentinel" in result.text,
            }
        ),
    )
