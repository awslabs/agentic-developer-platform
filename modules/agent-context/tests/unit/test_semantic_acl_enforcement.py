"""Semantic results require the same repository ACL as exact code search."""

from unittest.mock import AsyncMock, Mock

import pytest

from door import server
from door.acl import CallerPrincipal


@pytest.mark.asyncio
async def test_semantic_search_filters_private_and_unattributed_results(monkeypatch):
    caller = CallerPrincipal(github_login="alice", tenant_id="team-a", owner_sub="alice-id")
    response = Mock()
    response.json.return_value = {"model": "amazon.titan-embed-text-v2:0", "usage": {"prompt_tokens": 8, "total_tokens": 8, "completion_tokens": 0}, "data": [{"embedding": [0.1]}]}
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
    monkeypatch.setattr(server.config, "semantic_enabled", True)
    caller = CallerPrincipal(github_login="alice", tenant_id="team-a")
    response = Mock()
    response.json.return_value = {"model": "amazon.titan-embed-text-v2:0", "usage": {"prompt_tokens": 8, "total_tokens": 8, "completion_tokens": 0}, "data": [{"embedding": [0.1]}]}
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
    assert result["code"] == "semantic_search_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["disabled", "embedding", "vectors", "product_acl", "personal_acl"])
async def test_semantic_failure_is_explicit_and_never_a_successful_empty_result(monkeypatch, failure):
    product = failure == "product_acl"
    caller = CallerPrincipal(tenant_id="tenant-a", owner_sub="owner-a", run_bound=True, product_id="product-a" if product else "")
    response = Mock()
    response.json.return_value = {"model": "amazon.titan-embed-text-v2:0", "usage": {"prompt_tokens": 8, "total_tokens": 8, "completion_tokens": 0}, "data": [{"embedding": [0.1]}]}
    http = Mock(post=AsyncMock(return_value=response))
    vectors = Mock(query_scoped=Mock(return_value=[{"key": "result", "metadata": {
        "tenant_id": "tenant-a", "owner_sub": "" if product else "owner-a",
        "product_id": "product-a" if product else "", "visibility": "product" if product else "personal",
        "asset_id": "asset-a", "ingestion_attempt_id": "attempt-a", "chunk_text": "Never leak this excerpt on failure",
    }}]))
    acl = Mock()
    acl.get_active_ingestion_attempts.return_value = {"attempt-a"}
    acl.get_allowed_product_assets.return_value = {"asset-a": "attempt-a"}
    acl.get_allowed_personal_assets.return_value = {"asset-a": ("tenant-a", "attempt-a")}
    if failure == "embedding":
        http.post.side_effect = ConnectionError("embedding provider unavailable")
    elif failure == "vectors":
        vectors.query_scoped.side_effect = ConnectionError("vector provider unavailable")
    elif failure == "product_acl":
        acl.get_allowed_product_assets.side_effect = ConnectionError("ACL unavailable")
    elif failure == "personal_acl":
        acl.get_allowed_personal_assets.side_effect = ConnectionError("ACL unavailable")
    monkeypatch.setattr(server.state, "acl_store", acl)
    monkeypatch.setattr(server.state, "semantic_http_client", http)
    monkeypatch.setattr(server.state, "semantic_code_store", vectors)
    monkeypatch.setattr(server.config, "semantic_enabled", failure != "disabled")
    result = await server._handle_search({"scope": "personal", "query": "how is deployment done?"}, caller)
    assert result["code"] == "semantic_search_unavailable"
    assert result["results"] == []
    assert "Never leak" not in str(result)
    if failure == "disabled":
        http.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_docs_search_does_not_fall_back_to_exact_code(monkeypatch):
    monkeypatch.setattr(server.config, "semantic_enabled", False)
    zoekt = Mock(search=AsyncMock())
    monkeypatch.setattr(server.state, "zoekt", zoekt)
    result = await server._handle_search({"scope": "docs", "query": "installation instructions"}, CallerPrincipal(github_login="alice"))
    assert result["code"] == "semantic_search_unavailable"
    zoekt.search.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"query": "q", "limit": 101}, {"query": "q", "limit": -1}, {"query": "q", "limit": True}, {"query": "q" * 4097}])
async def test_search_is_bounded_before_dispatch(arguments, monkeypatch):
    zoekt = Mock(search=AsyncMock())
    monkeypatch.setattr(server.state, "zoekt", zoekt)
    result = await server._handle_search(arguments, CallerPrincipal(github_login="alice"))
    assert result["code"] == "invalid_search"
    zoekt.search.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["disabled", "network", "usage", "vectors", None])
async def test_semantic_usage_receipt_and_single_provider_call(monkeypatch, failure):
    caller = CallerPrincipal(tenant_id="tenant-a", owner_sub="owner-a", run_bound=True)
    response = Mock()
    response.json.return_value = {
        "model": "amazon.titan-embed-text-v2:0",
        "usage": {"prompt_tokens": 12, "total_tokens": 12, "completion_tokens": 0},
        "data": [{"embedding": [0.1]}],
    }
    http = Mock(post=AsyncMock(return_value=response))
    vectors = Mock(query_scoped=Mock(return_value=[]))
    if failure == "network":
        http.post.side_effect = TimeoutError("provider outcome unknown")
    if failure == "usage":
        response.json.return_value.pop("usage")
    if failure == "vectors":
        vectors.query_scoped.side_effect = RuntimeError("vector query failed")
    monkeypatch.setattr(server.config, "semantic_enabled", failure != "disabled")
    monkeypatch.setattr(server.state, "semantic_http_client", http)
    monkeypatch.setattr(server.state, "semantic_code_store", vectors)
    monkeypatch.setattr(server.state, "acl_store", Mock(get_active_ingestion_attempts=Mock(return_value=set())))
    result = await server._handle_semantic_search("deployment guide", 10, caller, personal_only=True)
    usage = result["embedding_usage"]
    assert usage["status"] == ("not_started" if failure == "disabled" else "unknown" if failure in {"network", "usage"} else "measured")
    if usage["status"] == "measured":
        assert usage["input_tokens"] == 12
    else:
        assert "input_tokens" not in usage
    if failure == "disabled":
        http.post.assert_not_awaited()
    else:
        http.post.assert_awaited_once()
        arguments = http.post.await_args.kwargs["json"]
        assert arguments["num_retries"] == arguments["max_retries"] == 0
        assert arguments["fallbacks"] == arguments["context_window_fallbacks"] == arguments["content_policy_fallbacks"] == []
    if failure:
        assert result["code"] == "semantic_search_unavailable"
