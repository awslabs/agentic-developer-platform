"""Effective policy is established by exact bounded reads, not a ready connection."""

import base64
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from adp_tools.adapter_contracts import Admission
from fastapi.testclient import TestClient

from tool_adapters import policy_observation as policy
from tool_adapters.atlassian import READ_TOOLS
from tool_adapters.config import AdapterConfiguration
from tool_adapters.server import create_app


def digest(value):
    return hashlib.sha256(policy.canonical(value)).hexdigest()


@pytest.mark.parametrize("operation", sorted(READ_TOOLS))
def test_outcomes_require_the_concrete_permission_and_exact_fixture(operation):
    permission = "search" if operation.startswith("search_") else "read"
    result = {"id": "101"}
    if operation.startswith("search_"):
        result = {"issues" if operation == "search_jira" else "results": [result]}
    packet = {"result": {"structuredContent": result}}
    assert policy.policy_outcome(packet, operation, digest(result)) == "allowed"
    assert policy.policy_outcome(packet, operation, "a" * 64) == "unknown"
    assert policy.policy_outcome({"result": {"structuredContent": {}}}, operation, digest({})) == "unknown"
    for name in (permission, "write", "other"):
        denial = {
            "error": {"message": f"Access denied: Your organization admin has not authorized the {name} permission."}
        }
        assert policy.policy_outcome(denial, operation, digest(result)) == (
            "blocked" if name == permission else "unknown"
        )
    assert policy.policy_outcome({"error": {"message": "Forbidden"}}, operation, digest(result)) == "unknown"


def settings():
    schema = {"type": "object", "properties": {"cloudId": {"type": "string"}}, "required": ["cloudId"]}
    return {
        "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
        "core_role_arn": "arn:aws:iam::123456789012:role/core",
        "gateway_role_arn": "arn:aws:iam::123456789012:role/gateway",
        "assertion_key_arn": "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
        "edge_provenance_sha256": hashlib.sha256(b"edge").hexdigest(),
        "identity_workload": "adp-tools-dev-atlassian-mcp-oauth-user",
        "identity_provider": "adp-tools-dev-atlassian-mcp-user",
        "config_revision": "a" * 64,
        "subject_schema_sha256": digest({}),
        "sites_schema_sha256": digest({}),
        "read_schemas": {name: schema for name in READ_TOOLS},
        "read_schemas_sha256": {name: digest(schema) for name in READ_TOOLS},
    }


def test_provider_probe_executes_all_pinned_reads_and_checks_subject_site_and_results():
    config = SimpleNamespace(**settings())
    calls = []
    scopes = [scope for _, scope in READ_TOOLS.values()]

    class Transport:
        def __init__(self, **kwargs):
            pass

        def send(self, method, params, token):
            assert token == "transient-secret"
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
                            *[
                                {"name": READ_TOOLS[alias][0], "inputSchema": schema}
                                for alias, schema in config.read_schemas.items()
                            ],
                        ]
                    }
                }
            calls.append(params["name"])
            value = {"id": "101"}
            if params["name"] == "atlassianUserInfo":
                value = {"accountId": "alice"}
            elif params["name"] == "getAccessibleAtlassianResources":
                value = {"resources": [{"id": "site", "scopes": scopes}]}
            elif params["name"] == READ_TOOLS["search_jira"][0]:
                value = {"issues": [value]}
            elif params["name"] == READ_TOOLS["search_confluence"][0]:
                value = {"results": [value]}
            return {"result": {"structuredContent": value}}

    expected = {
        name: digest(
            {"issues" if name == "search_jira" else "results": [{"id": "101"}]}
            if name.startswith("search_")
            else {"id": "101"}
        )
        for name in READ_TOOLS
    }
    arguments = dict(
        probes={name: {"cloudId": "site"} for name in READ_TOOLS},
        expected_results=expected,
        subject="alice",
        site_id="site",
        deadline=time.monotonic() + 20,
        transport_factory=Transport,
    )
    result = policy.probe_permissions("transient-secret", config, **arguments)
    assert {row["status"] for row in result["probes"].values()} == {"allowed"}
    assert calls[-4:] == [READ_TOOLS[name][0] for name in READ_TOOLS]
    assert "transient-secret" not in json.dumps(result)
    with pytest.raises(ValueError):
        policy.probe_permissions("transient-secret", config, **{**arguments, "subject": "bob"})


@pytest.mark.parametrize("change", [None, "role", "signature", "revision", "owner", "site", "reconnect", "probe"])
def test_packaged_policy_route_fences_claims_and_rechecks_after_identity(release, monkeypatch, change):
    config = AdapterConfiguration.model_validate(
        {"schema_version": "1.0", "mode": "observation", "release": release, "observation": settings()}
    )
    now = int(time.time() * 1000)
    binding = {"operation_id": "probe", "receipt_id": "probe", "payload_sha256": "f" * 64, "release": release}
    probes = {name: {"cloudId": "site"} for name in READ_TOOLS}
    expected = {name: "d" * 64 for name in READ_TOOLS}
    claims = {
        "audience": "tool-adapters:observe",
        "caller_role_arn": config.observation.core_role_arn,
        "purpose": "policy_observe",
        "binding": binding,
        "deadline_at_ms": now + 15000,
        "user_id": "alice",
        "tenant_id": "tenant",
        "connection_id": "connection",
        "binding_revision": "revision",
        "site_id": "site",
        "config_revision": "a" * 64,
        "provider_user_id": "alice",
        "probes_sha256": digest({"probes": probes, "expected_results": expected}),
    }
    body = {
        "schema_version": "1.0",
        "purpose": "policy_observe",
        "connector": "atlassian",
        "binding": binding,
        **{
            field: claims[field]
            for field in ("deadline_at_ms", "connection_id", "binding_revision", "site_id", "config_revision")
        },
        "probes": probes,
        "expected_results": expected,
    }
    body["signed_assertion"] = (
        base64.urlsafe_b64encode(policy.canonical({"claims": claims, "signature": "c2ln"})).decode().rstrip("=")
    )
    if change == "revision":
        body["binding_revision"] = "foreign"
    if change == "probe":
        body["expected_results"] = {name: "e" * 64 for name in READ_TOOLS}
    monkeypatch.setattr(
        policy.boto3,
        "client",
        lambda name: SimpleNamespace(verify=lambda **kwargs: {"SignatureValid": change != "signature"}),
    )
    count = 0

    async def authorize(*args):
        nonlocal count
        count += 1
        owner = "bob" if change == "owner" else "alice"
        return Admission.model_validate(
            {
                "schema_version": "1.0",
                "admission_id": "probe",
                "binding": binding,
                "purpose": "policy_observe",
                "connector": "atlassian",
                "operation": "policy_observe",
                "identity": {
                    "workload": config.observation.identity_workload,
                    "provider": config.observation.identity_provider,
                    "canonical_user_id": owner,
                },
                "context": {
                    "tenant_id": "tenant",
                    "canonical_user_id": owner,
                    "connection_id": "connection",
                    "binding_revision": "other" if change == "reconnect" and count == 2 else "revision",
                    "site_id": "foreign" if change == "site" else "site",
                    "scopes": [],
                },
                "issued_at_ms": now,
                "expires_at_ms": now + 15000,
                "deadline_at_ms": now + 15000,
            }
        )

    monkeypatch.setattr(policy, "authorize_observation", authorize)
    token = AsyncMock(return_value="transient-secret")
    monkeypatch.setattr(policy, "retrieve_access_token", token)
    called = []

    def probe(*args, **kwargs):
        called.append(kwargs)
        return {"provider_user_id": "alice", "scopes": [], "probes": {}}

    monkeypatch.setattr(policy, "probe_permissions", probe)
    with TestClient(create_app(config, source_revision="a" * 40)) as client:
        response = client.post(
            "/internal/v1/tool-adapters/observe",
            json=body,
            headers={
                "x-adp-edge-provenance": "edge",
                "x-caller-identity": "wrong"
                if change == "role"
                else config.observation.core_role_arn.replace(":iam:", ":sts:").replace(":role/", ":assumed-role/")
                + "/core-session",
            },
        )
    assert response.status_code == (200 if change is None else 403)
    assert bool(called) == (change is None)
    assert "transient-secret" not in response.text
