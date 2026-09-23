"""Semantic results require the same repository ACL as exact code search."""

from unittest.mock import AsyncMock, Mock

import pytest

from door import server
from door.acl import CallerPrincipal


@pytest.mark.asyncio
async def test_semantic_search_filters_private_and_unattributed_results(monkeypatch):
    caller = CallerPrincipal(github_login="alice", tenant_id="team-a", owner_sub="alice-id")
    response = Mock()
    response.json.return_value = {"data": [{"embedding": [0.1]}]}
    http = Mock(post=AsyncMock(return_value=response))
    vectors = Mock()
    vectors.query_scoped.return_value = [
        {"key": str(i), "metadata": {"repo": repo, "chunk_text": repo or "unattributed"}}
        for i, repo in enumerate(["acme/allowed", "acme/private", "other/private", "", "allowed"])
    ]
    acl = Mock()
    acl.get_allowed_repos.return_value = {"acme/allowed"}
    monkeypatch.setattr(server.state, "acl_store", acl)
    monkeypatch.setattr(server.state, "semantic_http_client", http)
    monkeypatch.setattr(server.state, "semantic_code_store", vectors)
    monkeypatch.setattr(server.config, "semantic_enabled", True)

    result = await server._handle_search({"scope": "docs", "query": "source"}, caller)
    assert [r["repo"] for r in result["results"]] == ["acme/allowed"]
    assert result["total"] == 1
    acl.get_allowed_repos.assert_called_with(caller)


@pytest.mark.asyncio
async def test_semantic_search_denies_when_acl_query_fails(monkeypatch):
    caller = CallerPrincipal(github_login="alice", tenant_id="team-a")
    response = Mock()
    response.json.return_value = {"data": [{"embedding": [0.1]}]}
    monkeypatch.setattr(server.state, "semantic_http_client", Mock(post=AsyncMock(return_value=response)))
    monkeypatch.setattr(server.state, "semantic_code_store", Mock(query_scoped=Mock(return_value=[
        {"key": "private", "metadata": {"repo": "acme/private", "chunk_text": "private"}},
    ])))
    acl = Mock()
    acl.get_allowed_repos.side_effect = ConnectionError("ACL database unavailable")
    monkeypatch.setattr(server.state, "acl_store", acl)
    result = await server._handle_semantic_search("source", 10, caller)
    assert result["results"] == []
    assert result["total"] == 0
