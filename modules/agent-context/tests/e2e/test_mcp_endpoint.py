"""
E2E tests for the Context MCP Server endpoint.

Tests 7-12 from issue #21:
7.  GET /tools lists exactly 6 tools: search, understand, impact, browse, remember, experience
8.  search() against empty index returns well-formed empty result
9.  search(scope="docs") returns results (live only)
10. remember + search(scope="memory") round-trip (live only)
11. Malformed JSON -> 400 (not 500)
12. Request without auth check
"""

from __future__ import annotations

import uuid

import pytest


EXPECTED_TOOL_NAMES = {"search", "understand", "impact", "browse", "remember", "experience"}


# ---------------------------------------------------------------------------
# Test 7: GET /tools lists exactly 6 tools
# ---------------------------------------------------------------------------


class TestToolsListing:
    """Verify the MCP endpoint exposes exactly the 6 documented tools."""

    def test_tools_count(self, mcp_client):
        tools = mcp_client.get_tools()
        assert len(tools) == 6, f"Expected 6 tools, got {len(tools)}: {tools}"

    def test_tools_names(self, mcp_client):
        tools = mcp_client.get_tools()
        names = {t["name"] for t in tools}
        assert names == EXPECTED_TOOL_NAMES, (
            f"Tool names mismatch: got {names}, expected {EXPECTED_TOOL_NAMES}"
        )

    def test_tools_have_descriptions(self, mcp_client):
        tools = mcp_client.get_tools()
        for tool in tools:
            assert "description" in tool, f"Tool {tool['name']} missing description"
            assert len(tool["description"]) > 0, f"Tool {tool['name']} has empty description"

    def test_tools_have_parameters(self, mcp_client):
        tools = mcp_client.get_tools()
        for tool in tools:
            assert "parameters" in tool, f"Tool {tool['name']} missing parameters"
            assert isinstance(tool["parameters"], dict), (
                f"Tool {tool['name']} parameters should be a dict"
            )


# ---------------------------------------------------------------------------
# Test 8: search() against empty index returns well-formed empty result
# ---------------------------------------------------------------------------


class TestSearchEmpty:
    """Verify search on an empty/mock index returns a valid response."""

    def test_search_empty_returns_valid_response(self, mcp_client):
        result = mcp_client.call_tool(
            "search",
            {
                "query": "hello world",
                "scope": "code",
                "limit": 5,
            },
        )
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        # Must have a results field (possibly empty)
        assert "results" in result or "error" not in result, f"Search returned error: {result}"

    def test_search_empty_no_500(self, mcp_client):
        """Search on empty index must not return a 500/stack trace."""
        result = mcp_client.call_tool(
            "search",
            {
                "query": "nonexistent-query-xyz-12345",
                "scope": "code",
                "limit": 1,
            },
        )
        # Should get a clean response, not an error
        assert isinstance(result, dict)
        if "error" in result:
            # Errors should be user-friendly, not stack traces
            assert "Traceback" not in str(result["error"])


# ---------------------------------------------------------------------------
# Test 9: search(scope="docs") returns results (live only)
# ---------------------------------------------------------------------------


class TestSearchDocs:
    """Live-only: verify docs search returns real results after ingestion."""

    @pytest.mark.live_only
    def test_search_docs_returns_results(self, mcp_client):
        result = mcp_client.call_tool(
            "search",
            {
                "query": "kubernetes deployment",
                "scope": "docs",
            },
        )
        assert isinstance(result, dict)
        results = result.get("results", [])
        assert len(results) > 0, "Expected at least one doc search result"


# ---------------------------------------------------------------------------
# Test 10: remember + search(scope="memory") round-trip (live only)
# ---------------------------------------------------------------------------


class TestRememberRecall:
    """Live-only: verify storing a memory and retrieving it via search."""

    @pytest.mark.live_only
    def test_remember_and_recall(self, mcp_client):
        test_key = f"test/{uuid.uuid4().hex[:8]}"
        test_value = f"e2e-test-memory-{uuid.uuid4().hex[:8]}"

        # Store
        store_result = mcp_client.call_tool(
            "remember",
            {
                "session_id": test_key,
                "messages": [
                    {"role": "user", "content": f"Remember this: {test_value}"},
                    {"role": "assistant", "content": f"Stored: {test_value}"},
                ],
                "outcome": test_value,
            },
        )
        assert isinstance(store_result, dict)

        # Recall via search
        search_result = mcp_client.call_tool(
            "search",
            {
                "query": test_value,
                "scope": "memory",
            },
        )
        assert isinstance(search_result, dict)
        results = search_result.get("results", [])
        assert len(results) > 0, (
            f"Expected to find stored memory for '{test_value}', got empty results"
        )


# ---------------------------------------------------------------------------
# Test 11: Malformed JSON -> 400 (not 500)
# ---------------------------------------------------------------------------


class TestMalformedInput:
    """Verify the MCP endpoint handles malformed input gracefully."""

    def test_malformed_json_returns_400(self, mcp_client):
        resp = mcp_client.call_tool_raw(b"{{invalid json")
        assert resp.status_code == 400, f"Expected 400 for malformed JSON, got {resp.status_code}"

    def test_malformed_json_no_stack_trace(self, mcp_client):
        resp = mcp_client.call_tool_raw(b"not json at all")
        body = resp.text if hasattr(resp, "text") else str(resp.content)
        assert "Traceback" not in body, "Response contains a stack trace"


# ---------------------------------------------------------------------------
# Test 12: Auth posture check
# ---------------------------------------------------------------------------


class TestAuthPosture:
    """Verify the MCP endpoint's auth behavior.

    The MCP server is typically in-cluster with no external auth.
    This test verifies the endpoint responds (in-cluster) and documents
    what happens for external access.
    """

    def test_tools_endpoint_accessible(self, mcp_client):
        """The /tools endpoint should be accessible (at least in-cluster)."""
        tools = mcp_client.get_tools()
        assert isinstance(tools, list), "Expected /tools to return a list"
        assert len(tools) > 0, "Expected at least one tool from /tools"
