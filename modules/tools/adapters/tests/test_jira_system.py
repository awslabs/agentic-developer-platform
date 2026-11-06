"""System context uses MCP reads, current schemas and an explicit system identity."""

import asyncio
import base64
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from adp_tools.jira_context import JiraContextQuery, jira_jql
from adp_tools.jira_system_contract import JiraSystemRequest
from fastapi import HTTPException
from starlette.requests import Request

from tool_adapters import jira_system


def test_atlassian_ui_envelope_matches_the_tool_data_without_leaking_context():
    issue = {"id": "100", "key": "APP-1", "fields": {"summary": "Example"}}
    text = {"data": {"issues": [issue], "isLast": False, "nextPageToken": "page-2"}}
    result = {
        "content": [{"type": "text", "text": json.dumps(text)}],
        "structuredContent": {
            "issues": {"nodes": [issue], "pageInfo": {"hasNextPage": True, "endCursor": "page-2"}},
            "context": {"sessionId": "provider-internal"},
        },
    }
    assert jira_system.value_from_result(result) == text["data"]
    result["structuredContent"]["issues"]["pageInfo"]["endCursor"] = "wrong"
    with pytest.raises(ValueError, match="ambiguous"):
        jira_system.value_from_result(result)


@pytest.mark.parametrize(
    "operation,tool,arguments",
    [
        ("issues", "searchJiraIssuesUsingJql", {"jql": "project IN (100)"}),
        ("backlog", "getJiraBoardIssueData", {"boardId": 5, "dataset": "backlog"}),
        ("issues", "getJiraBoardIssueData", {"boardId": 5, "dataset": "all"}),
        ("sprints", "listJiraBoardSprints", {"boardId": 5, "startAt": 2}),
    ],
)
@pytest.mark.parametrize("description_changed", [False, True])
def test_only_read_mcp_tools_are_called_and_pagination_is_forwarded(operation, tool, arguments, description_changed):
    schema = {"type": "object", "properties": {key: {} for key in [*arguments, "cloudId"]}, "required": ["cloudId"]}
    reviewed = json.loads(json.dumps(schema))
    if description_changed:
        schema["properties"]["cloudId"]["description"] = "Updated provider help text"
    packets = []

    class Transport:
        def __init__(self, *, deadline):
            assert deadline > time.monotonic()

        def send(self, method, params, key):
            assert key == "test-key"
            packets.append((method, params))
            if method == "initialize":
                return {"result": {"protocolVersion": jira_system.PROTOCOL_VERSION}}
            if method == "notifications/initialized":
                return None
            if method == "tools/list":
                return {
                    "result": {
                        "tools": [{"name": tool, "inputSchema": schema}, {"name": "deleteJiraIssue", "inputSchema": {}}]
                    }
                }
            return {"result": {"content": [{"type": "text", "text": '{"data":{"values":[]}}'}]}}

    body = SimpleNamespace(
        operation=operation,
        arguments=arguments,
        site_id="site",
        schemas={tool: reviewed},
        deadline_at_ms=int(time.time() * 1000) + 10000,
    )
    result = jira_system.execute(body, key_loader=lambda _: "test-key", transport_factory=Transport)
    assert result["data"] == {"values": []}
    assert packets[-1] == ("tools/call", {"name": tool, "arguments": {**arguments, "cloudId": "site"}})
    body.schemas = {tool: {**schema, "required": ["newArgument"]}}
    packets.clear()
    with pytest.raises(ValueError, match="schema_changed"):
        jira_system.execute(body, key_loader=lambda _: "test-key", transport_factory=Transport)
    assert not any(method == "tools/call" for method, _ in packets)


def test_jql_is_constructed_with_literal_filters_and_fixed_project_scope():
    query = JiraContextQuery(
        product_id="product", project_id="project", source_id="source", view="issues", text='x" OR project = SECRET'
    )
    jql = jira_jql(["100"], query)
    assert jql.startswith("project IN (100) AND text ~ ")
    # Decode the one JQL string literal: the attempted OR stays inside it.
    literal = jql.split("text ~ ", 1)[1].removesuffix(" ORDER BY Rank ASC")
    assert "OR project = SECRET" in json.loads(literal)

    with pytest.raises(ValueError):
        JiraContextQuery(product_id="p", project_id="j", view="issues", source_id="s", jql="project = SECRET")
    with pytest.raises(ValueError):
        JiraContextQuery(product_id="p", project_id="j", view="issues", source_id="s", limit=51)


def test_identity_retrieves_a_system_key_without_user_federation(monkeypatch):
    client = MagicMock()
    client.get_workload_access_token.return_value = {"workloadAccessToken": "workload-token"}
    client.get_resource_api_key.return_value = {"apiKey": "system-key"}
    monkeypatch.setattr(jira_system.boto3, "client", lambda *args, **kwargs: client)
    assert jira_system.resource_key(SimpleNamespace(workload="system", provider="provider")) == "system-key"
    client.get_workload_access_token.assert_called_once_with(workloadName="system")
    client.get_resource_api_key.assert_called_once_with(
        workloadIdentityToken="workload-token", resourceCredentialProviderName="provider"
    )
    client.get_workload_access_token_for_user_id.assert_not_called()


@pytest.mark.parametrize("tamper", [None, "site_id", "arguments", "workload", "deadline", "edge", "role"])
def test_private_transport_binds_the_entire_system_request(release, monkeypatch, tamper):
    role = "arn:aws:iam::123456789012:role/core"
    workload = "adp-tools-test-atlassian-mcp-system"
    body = JiraSystemRequest(
        connection_id="connection",
        connection_revision="revision",
        tenant_id="tenant",
        site_id="site",
        workload=workload,
        provider=workload + "-" + hashlib.sha256(b"tenant").hexdigest()[:12] + "-key",
        operation="issues",
        arguments={"jql": "project IN (100)"},
        release=release,
        deadline_at_ms=int(time.time() * 1000) + 10000,
        signed_assertion="placeholder",
    )
    claims = {
        "audience": "tool-adapters:jira-system",
        "caller_role_arn": role,
        "request_sha256": jira_system.request_digest(body),
    }
    body = body.model_copy(
        update={
            "signed_assertion": base64.urlsafe_b64encode(
                json.dumps({"claims": claims, "signature": "c2ln"}).encode()
            ).decode()
        }
    )
    config = SimpleNamespace(
        release=body.release,
        observation=SimpleNamespace(
            core_role_arn=role,
            edge_provenance_sha256=hashlib.sha256(b"proof").hexdigest(),
            identity_workload="adp-tools-test-atlassian-mcp-oauth-user",
            assertion_key_arn="key",
        ),
    )
    client = MagicMock()
    client.verify.return_value = {"SignatureValid": True}
    monkeypatch.setattr(jira_system.boto3, "client", lambda *args, **kwargs: client)
    headers = [(b"x-adp-edge-provenance", b"proof"), (b"x-caller-identity", role.encode())]
    if tamper == "site_id":
        body = body.model_copy(update={"site_id": "another-site"})
    if tamper == "arguments":
        body = body.model_copy(update={"arguments": {"jql": "project = SECRET"}})
    if tamper == "workload":
        body = body.model_copy(update={"workload": "another-workload"})
    if tamper == "deadline":
        body = body.model_copy(update={"deadline_at_ms": 1})
    if tamper == "edge":
        headers[0] = (headers[0][0], b"wrong")
    if tamper == "role":
        headers[1] = (headers[1][0], b"arn:aws:iam::123456789012:role/other")
    request = Request({"type": "http", "headers": headers})
    if tamper:
        with pytest.raises(HTTPException):
            asyncio.run(jira_system.verify_request(body, request, config))
        client.verify.assert_not_called()
    else:
        asyncio.run(jira_system.verify_request(body, request, config))
        client.verify.assert_called_once()
