"""Controlled adapter fault fixture; the facade journal has separate production tests."""

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


class UncertainWriteRoute:
    def __init__(self, release, monkeypatch):
        self.release = release
        self.reconciler_role = "arn:aws:iam::123456789012:role/fixture-reconciler"
        self.gateway_role = "arn:aws:iam::123456789012:role/fixture-gateway"
        schema = {
            "type": "object",
            "properties": {"cloudId": {"type": "string"}, "issueIdOrKey": {"type": "string"}},
            "required": ["cloudId", "issueIdOrKey"],
        }
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
                    "reconciler_role_arn": self.reconciler_role,
                    "read_schemas": {alias: schema for alias in READ_TOOLS},
                    "read_schemas_sha256": {alias: digest for alias in READ_TOOLS},
                    "write_enabled": True,
                    "write_schemas": {alias: schema for alias in WRITE_TOOLS},
                    "write_schemas_sha256": {alias: digest for alias in WRITE_TOOLS},
                },
            }
        )
        self.client = None
        self.active_generation = release["backend_generation"]
        self.effect_count = 0
        self.read_count = 0
        self.provider = {
            "FIX-42": {"summary": "before", "owner": "run-one"},
            "FOREIGN-1": {"summary": "unrelated", "owner": "other"},
        }
        self.consumed = False
        self.connection = {"id": "connection-one", "revision": "revision-one", "site_id": "site-one"}
        self.arguments = {"cloudId": "site-one", "issueIdOrKey": "FIX-42"}
        self.alias = "update_jira_issue"
        self.operation_id = "OP#tenant-one#owner-one#atlassian___update_jira_issue#request-one"
        self.binding = {
            "operation_id": self.operation_id,
            "receipt_id": "receipt-one",
            "payload_sha256": hashlib.sha256(canonical(self.arguments)).hexdigest(),
            "release": release,
        }
        self.write_authorization = self._admission("tool_call", self.alias)
        self.authorization = AsyncMock(side_effect=self._authorize)
        self.dispatch = AsyncMock(side_effect=self._dispatch)
        monkeypatch.setattr(atlassian_route, "publish_reads", AsyncMock(return_value=[]))
        monkeypatch.setattr(atlassian_route, "authorize_read", self.authorization)
        monkeypatch.setattr(atlassian_route, "consume_dispatch", self.dispatch)
        monkeypatch.setattr(atlassian_route, "retrieve_access_token", AsyncMock(return_value="fixture-token"))
        monkeypatch.setattr(atlassian_route, "write_tool", self._write_and_lose_reply)
        monkeypatch.setattr(atlassian_route, "read_tool", self._read)

    def _admission(self, purpose, operation):
        now = int(time.time() * 1000)
        return Admission.model_validate(
            {
                "schema_version": "1.0",
                "admission_id": "admission-one",
                "binding": self.binding,
                "purpose": purpose,
                "connector": "atlassian",
                "operation": operation,
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
                    "scopes": ["read:jira:agent-interface"]
                    if purpose == "reconcile"
                    else ["write:jira:agent-interface"],
                },
                "issued_at_ms": now,
                "expires_at_ms": now + 5000,
                "deadline_at_ms": now + 16000,
            }
        )

    async def _authorize(self, assertion, binding, operation, deadline, configuration, **kwargs):
        if (
            self.active_generation != self.release["backend_generation"]
            or configuration.release.backend_generation != self.release["backend_generation"]
        ):
            raise HTTPException(403, detail={"error": "adapter_release_fenced"})
        return self._admission("reconcile" if kwargs.get("purpose") == "reconcile" else "tool_call", operation)

    async def _dispatch(self, admission, configuration):
        if self.consumed:
            raise HTTPException(503, detail={"error": "authority_unavailable"})
        self.consumed = True

    def _write_and_lose_reply(self, token, alias, arguments, pins, *, deadline):
        assert alias == self.alias and arguments == self.arguments
        assert pins == self.configuration.observation.write_schemas_sha256 and token == "fixture-token"
        self.effect_count += 1
        self.provider["FIX-42"]["summary"] = "updated"
        raise TimeoutError("provider acknowledgement lost")

    def _read(self, token, alias, arguments, pins, *, deadline, reviewed_schemas):
        assert reviewed_schemas == self.configuration.observation.read_schemas
        assert alias == "get_jira_issue" and token == "fixture-token"
        assert arguments == self.arguments and pins == self.configuration.observation.read_schemas_sha256
        self.read_count += 1
        return {"structuredContent": {"id": "FIX-42", **self.provider["FIX-42"]}}

    def restart(self, generation=None):
        if self.client is not None:
            self.client.__exit__(None, None, None)
        config = self.configuration
        if generation is not None:
            config = config.model_copy(
                update={"release": config.release.model_copy(update={"backend_generation": generation})}
            )
        self.client = TestClient(create_app(config, source_revision=self.release["source_revision"]))
        self.client.__enter__()

    def close(self):
        if self.client is not None:
            self.client.__exit__(None, None, None)
            self.client = None

    def write(self):
        approval = {
            "tool": "atlassian___" + self.alias,
            "request_id": "request-one",
            "payload_sha256": self.binding["payload_sha256"],
            "policy_revision": 1,
            "consent_source": "explicit",
        }
        recorded = {
            "tool": approval["tool"],
            "operation_id": self.operation_id,
            "request_id": "request-one",
            "receipt_id": "receipt-one",
            "execution_id": "execution-one",
            "payload_sha256": self.binding["payload_sha256"],
            "release": self.release,
            "connection": self.connection,
            "dispatch_intent": "dispatched",
            "approval": approval,
        }
        claims = {
            "binding": self.binding,
            "rpc_id": self.binding["operation_id"],
            "tool": approval["tool"],
            "principal": {"tenant_id": "tenant-one", "user_id": "owner-one"},
            "connection": self.connection,
            "arguments_sha256": self.binding["payload_sha256"],
            "atlassian_approval": approval,
            "atlassian_operation": recorded,
            "expires_at": int(time.time()) + 18,
        }
        assertion = base64.urlsafe_b64encode(json.dumps({"claims": claims}).encode()).decode().rstrip("=")
        return self.client.post(
            "/internal/v1/tools/atlassian/mcp",
            headers={
                "x-adp-edge-provenance": "fixture-edge",
                "x-caller-identity": self.gateway_role,
            },
            json={
                "jsonrpc": "2.0",
                "id": self.operation_id,
                "method": "tools/call",
                "params": {
                    "name": self.alias,
                    "arguments": {**self.arguments, "adp_assertion": assertion},
                },
            },
        )

    def readback(self):
        deadline = int(time.time() * 1000) + 16000
        original = {
            **self.binding,
            "tool": "atlassian___" + self.alias,
            "request_id": "request-one",
            "connection": self.connection,
        }
        claims = {
            "principal": {"tenant_id": "tenant-one", "user_id": "owner-one"},
            "connection": self.connection,
            "atlassian_operation": original,
            "read_arguments_sha256": hashlib.sha256(canonical(self.arguments)).hexdigest(),
        }
        assertion = base64.urlsafe_b64encode(json.dumps({"claims": claims}).encode()).decode().rstrip("=")
        return self.client.post(
            "/internal/v1/tools/atlassian/reconcile",
            headers={
                "x-adp-edge-provenance": "fixture-edge",
                "x-caller-identity": self.reconciler_role,
            },
            json={
                "schema_version": "1.0",
                "purpose": "reconcile",
                "connector": "atlassian",
                "operation": "get_jira_issue",
                "binding": self.binding,
                "read_arguments": self.arguments,
                "signed_assertion": assertion,
                "deadline_at_ms": deadline,
            },
        )
