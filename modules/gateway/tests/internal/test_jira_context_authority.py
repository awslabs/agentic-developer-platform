"""The Product context alias must retain the original signed MCP call."""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.internal import tools_knowledge
from src.internal.tool_adapter_observation import canonical
from src.products import jira_context
from tests.internal.test_tool_adapter_authority import authority as adapter_authority
from tests.internal.test_tool_adapter_authority import request_body

authority = adapter_authority


@pytest.mark.parametrize("change", [None, "arguments", "connection", "query", "signature"])
async def test_context_read_is_bound_to_original_arguments_connection_and_deadline(authority, monkeypatch, change):
    client, _, _, _, _ = authority
    arguments = {"product_id": "product", "project_id": "project", "source_id": "source", "view": "issues"}
    if change == "query":
        arguments["jql"] = "project = unrelated"
    connection = {"id": "shared-credential", "revision": "current", "site_id": "site"}
    body = request_body(
        operation="get_project_context", connection=connection, claims_update={"arguments_sha256": hashlib.sha256(canonical(arguments)).hexdigest()}
    )
    body["context_arguments"] = dict(arguments)
    if change == "arguments":
        body["context_arguments"]["project_id"] = "forged-project"
    if change == "signature":
        body["signed_assertion"] = "untrusted"
    policy = AsyncMock(return_value=1)
    monkeypatch.setattr(tools_knowledge, "check_policy", policy)
    selected = SimpleNamespace(credential_id=connection["id"], revision="newer" if change == "connection" else "current", site_id="site")
    actor = SimpleNamespace(user_id="alice")
    monkeypatch.setattr(jira_context, "tool_context_connection", AsyncMock(return_value=(actor, selected)))
    read = AsyncMock(return_value={"data": {"issues": []}, "source": {"id": "source"}})
    monkeypatch.setattr(jira_context, "read_context", read)
    response = client.post("/internal/v1/tool-adapters/authorize", json=body)
    if change:
        assert response.status_code in {403, 422}, response.text
        read.assert_not_awaited()
    else:
        assert response.status_code == 200, response.text
        assert response.json()["jira_context_result"]["source"]["id"] == "source"
        policy.assert_awaited_once()
        assert read.call_args.kwargs["deadline_at_ms"] == body["deadline_at_ms"]
        assert read.call_args.args[1] is actor
        assert read.call_args.args[2].project_id == "project"
