"""Controlled private-route Jira write scenarios; no live-provider claims."""

import base64
import hashlib
import json
import time
from unittest.mock import AsyncMock

from adp_tools.adapter_contracts import Admission
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tool_adapters import atlassian_route
from tool_adapters.atlassian import READ_TOOLS, WRITE_TOOLS
from tool_adapters.atlassian_observe import canonical
from tool_adapters.config import AdapterConfiguration
from tool_adapters.server import create_app

DENIALS = (
    "read_only",
    "revoked",
    "wrong_site",
    "foreign_user",
    "denied_policy",
    "changed_approval",
    "changed_payload",
    "direct_request",
)


class WriteDenialRoute:
    def __init__(self, release, monkeypatch):
        self.gateway_role = "arn:aws:iam::123456789012:role/fixture-gateway"
        schema = {"type": "object", "properties": {"cloudId": {"type": "string"}}, "required": ["cloudId"]}
        digest = hashlib.sha256(canonical(schema)).hexdigest()
        self.configuration = AdapterConfiguration.model_validate(
            {
                "schema_version": "1.0",
                "mode": "observation",
                "release": release,
                "observation": {
                    "core_authorize_url": "https://core.example.test/internal/v1/tool-adapters/authorize",
                    "core_role_arn": "arn:aws:iam::123456789012:role/fixture-core",
                    "assertion_key_arn": "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
                    "edge_provenance_sha256": hashlib.sha256(b"fixture-edge").hexdigest(),
                    "identity_workload": "adp-tools-fixture-atlassian-mcp-oauth-user",
                    "identity_provider": "adp-tools-fixture-atlassian-mcp-user",
                    "config_revision": "a" * 64,
                    "subject_schema_sha256": "b" * 64,
                    "sites_schema_sha256": "c" * 64,
                    "gateway_role_arn": self.gateway_role,
                    "read_schemas": {alias: schema for alias in READ_TOOLS},
                    "read_schemas_sha256": {alias: digest for alias in READ_TOOLS},
                    "write_enabled": True,
                    "write_schemas": {alias: schema for alias in WRITE_TOOLS},
                    "write_schemas_sha256": {alias: digest for alias in WRITE_TOOLS},
                },
            }
        )
        self.client = TestClient(create_app(self.configuration, source_revision=release["source_revision"]))
        self.connection = {"id": "connection-one", "revision": "revision-one", "site_id": "site-one"}
        self.provider_issues = {}
        self.provider_effects = 0
        self.authorization = AsyncMock()
        self.dispatch = AsyncMock()
        self.identity = AsyncMock(return_value="fixture-transient-token")
        monkeypatch.setattr(atlassian_route, "publish_reads", AsyncMock(return_value=[]))
        monkeypatch.setattr(atlassian_route, "authorize_read", self.authorization)
        monkeypatch.setattr(atlassian_route, "consume_dispatch", self.dispatch)
        monkeypatch.setattr(atlassian_route, "retrieve_access_token", self.identity)
        monkeypatch.setattr(atlassian_route, "write_tool", self._mutate)

    def __enter__(self):
        self.client.__enter__()
        return self

    def __exit__(self, *args):
        return self.client.__exit__(*args)

    def _mutate(self, token, alias, arguments, pins, *, deadline):
        assert token == "fixture-transient-token"
        assert pins == self.configuration.observation.write_schemas_sha256
        assert deadline > time.monotonic()
        self.provider_effects += 1
        issue_id = f"FIX-{self.provider_effects}"
        self.provider_issues[issue_id] = {"alias": alias, "cloudId": arguments["cloudId"]}
        return {"structuredContent": {"id": issue_id}}

    def provider_readback(self):
        return {
            "effects": self.provider_effects,
            "issues": {issue_id: dict(issue) for issue_id, issue in self.provider_issues.items()},
        }

    def call(self, alias, change="approved", *, transport_id=None):
        if alias not in WRITE_TOOLS or change not in (*DENIALS, "approved"):
            raise ValueError("Unknown Jira write denial scenario")
        original = {"cloudId": "site-one"}
        arguments = {**original}
        if change in {"wrong_site", "changed_payload"}:
            arguments["cloudId"] = "site-two"
        bound_arguments = arguments if change == "wrong_site" else original
        digest = hashlib.sha256(canonical(bound_arguments)).hexdigest()
        operation = f"atlassian___{alias}"
        principal = {"tenant_id": "tenant-one", "user_id": "owner-one"}
        if change == "foreign_user":
            principal["user_id"] = "owner-other"
        binding = {
            "operation_id": f"relay-{alias}",
            "receipt_id": "receipt-one",
            "payload_sha256": digest,
            "release": self.configuration.release.model_dump(),
        }
        approval = {
            "tool": operation,
            "request_id": "request-one",
            "payload_sha256": digest,
            "policy_revision": 1,
            "consent_source": "explicit",
        }
        recorded = {
            "tool": operation,
            "operation_id": f"OP#tenant-one#{principal['user_id']}#{operation}#request-one",
            "request_id": "request-one",
            "receipt_id": "receipt-one",
            "execution_id": "execution-one",
            "payload_sha256": digest,
            "release": binding["release"],
            "connection": self.connection,
            "dispatch_intent": "dispatched",
            "approval": approval,
        }
        claims = {
            "binding": binding,
            "rpc_id": binding["operation_id"],
            "tool": operation,
            "principal": principal,
            "connection": self.connection,
            "arguments_sha256": digest,
            "atlassian_approval": dict(approval),
            "atlassian_operation": recorded,
            "expires_at": int(time.time()) + 18,
        }
        if change == "changed_approval":
            claims["atlassian_approval"]["policy_revision"] = 2
        now = int(time.time() * 1000)
        admission = Admission.model_validate(
            {
                "schema_version": "1.0",
                "admission_id": "admission-one",
                "binding": binding,
                "purpose": "tool_call",
                "connector": "atlassian",
                "operation": alias,
                "identity": {
                    "workload": self.configuration.observation.identity_workload,
                    "provider": self.configuration.observation.identity_provider,
                    "canonical_user_id": "owner-one",
                },
                "context": {
                    "tenant_id": "tenant-one",
                    "canonical_user_id": "owner-one",
                    "connection_id": self.connection["id"],
                    "binding_revision": self.connection["revision"],
                    "site_id": "site-one",
                    "scopes": (
                        ["read:jira:agent-interface"] if change == "read_only" else ["write:jira:agent-interface"]
                    ),
                },
                "issued_at_ms": now,
                "expires_at_ms": now + 5000,
                "deadline_at_ms": now + 16000,
            }
        )
        self.authorization.side_effect = (
            HTTPException(403, detail={"error": "adapter_authority_required"})
            if change in {"revoked", "denied_policy"}
            else None
        )
        self.authorization.return_value = admission
        assertion = base64.urlsafe_b64encode(json.dumps({"claims": claims}).encode()).decode().rstrip("=")
        headers = {"x-adp-edge-provenance": "fixture-edge", "x-caller-identity": self.gateway_role}
        if change == "direct_request":
            headers["x-caller-identity"] = "arn:aws:iam::123456789012:role/foreign"
        return self.client.post(
            "/internal/v1/tools/atlassian/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": binding["operation_id"] if transport_id is None else transport_id,
                "method": "tools/call",
                "params": {"name": alias, "arguments": {**arguments, "adp_assertion": assertion}},
            },
        )
