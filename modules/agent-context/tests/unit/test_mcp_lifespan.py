"""Integration tests for MCP endpoint with the real lifespan running.

Validates that the MCP session-manager lifespan is properly composed into
the parent FastAPI app's lifespan (Issue #1612). This is the test gap that
allowed the original bug to ship: the #1609 tests used ASGITransport(app=app)
WITHOUT running the real lifespan, so they passed while prod returned 500.

These tests exercise the app with its lifespan actually running via
asgi-lifespan's LifespanManager, proving that `POST /mcp/` works end-to-end
(no "Task group is not initialized" RuntimeError).
"""

from __future__ import annotations

import json

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from door.server import app

# Use module-scoped event loop so the lifespan fixture (which can only run
# once per mcp_server instance) spans all tests in this file.
pytestmark = pytest.mark.asyncio(loop_scope="module")


def _parse_sse_json(text: str) -> dict:
    """Parse a JSON-RPC result from an SSE-formatted response.

    MCP Streamable HTTP returns responses as SSE events even for single
    messages. Format: 'event: message\\ndata: {json}\\n\\n'
    """
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise ValueError(f"No 'data:' line found in SSE response: {text[:200]}")


@pytest.fixture(scope="module")
async def live_app():
    """Run the app's lifespan once for the entire test module.

    The MCP session manager's run() can only be called once per instance
    (by design in mcp 1.28.x). Using module scope ensures we start it once
    and reuse for all tests in this file.
    """
    async with LifespanManager(app) as manager:
        yield manager.app


@pytest.fixture
async def live_client(live_app):
    """Async HTTP client backed by the lifespan-running app.

    This is the critical difference from the #1609 test fixture which used
    bare ASGITransport(app=app) — that skips the lifespan and the session
    manager is never started.

    Uses localhost:5100 to satisfy MCP's DNS-rebinding protection which
    requires the Host header to include a port (allowed_hosts = ['localhost:*']).
    Sets Accept header to satisfy MCP Streamable HTTP transport requirements.
    """
    transport = ASGITransport(app=live_app)
    async with AsyncClient(
        transport=transport,
        base_url="http://localhost:5100",
        headers={"Accept": "application/json, text/event-stream"},
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# MCP session manager lifespan tests (the bug fix validation)
# ---------------------------------------------------------------------------


class TestMCPLifespanComposed:
    """Verify MCP endpoint works when the app lifespan is actually running.

    These tests would have caught the original #1612 bug: without
    session_manager.run() in the lifespan, POST /mcp/ returns 500 with
    "Task group is not initialized".
    """

    async def test_mcp_initialize_succeeds(self, live_client):
        """POST /mcp/ with JSON-RPC initialize returns 200 (not 500).

        This is the exact reproduction case from the bug report.
        """
        resp = await live_client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"},
                },
            },
        )
        assert resp.status_code == 200, (
            f"Expected 200 from /mcp/ initialize, got {resp.status_code}. Body: {resp.text[:500]}"
        )
        body = _parse_sse_json(resp.text)
        assert "result" in body, f"Expected JSON-RPC result, got: {body}"
        assert body["result"]["protocolVersion"] == "2025-03-26"

    async def test_mcp_tools_list_returns_all_tools(self, live_client):
        """tools/list via MCP returns all 7 verbs under live lifespan.

        With stateless_http=True, each request creates its own session —
        tools/list works directly without a prior initialize handshake.
        """
        resp = await live_client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
        )
        assert resp.status_code == 200, (
            f"Expected 200 from tools/list, got {resp.status_code}. Body: {resp.text[:500]}"
        )
        body = _parse_sse_json(resp.text)
        assert "result" in body, f"Expected JSON-RPC result, got: {body}"
        tools = body["result"]["tools"]
        assert len(tools) == 7, f"Expected 7 tools, got {len(tools)}: {[t['name'] for t in tools]}"
        tool_names = {t["name"] for t in tools}
        assert tool_names == {
            "search",
            "understand",
            "impact",
            "browse",
            "remember",
            "experience",
            "secure",
        }


# ---------------------------------------------------------------------------
# ACL fail-closed under live lifespan (re-validates #1609 ACL tests)
# ---------------------------------------------------------------------------


class TestACLUnderLiveLifespan:
    """Re-run ACL fail-closed assertions with the real lifespan running.

    The #1609 tests passed without the lifespan because they tested at the
    Python call level. These tests exercise the full HTTP path (lifespan +
    session manager + tool dispatch) to prove ACL still works correctly
    when the session manager is actually alive.
    """

    async def test_mcp_search_no_identity_returns_empty(self, live_client):
        """MCP tools/call search without identity headers -> empty results (fail-closed)."""
        resp = await live_client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "search",
                    "arguments": {"query": "test", "scope": "code"},
                },
            },
        )
        assert resp.status_code == 200
        body = _parse_sse_json(resp.text)
        assert "result" in body, f"Expected result, got: {body}"
        # Parse the text content from the MCP response
        content = body["result"]["content"]
        assert len(content) >= 1
        parsed = json.loads(content[0]["text"])
        # ACL fail-closed: no identity -> empty results
        assert parsed["results"] == []


# ---------------------------------------------------------------------------
# Legacy REST still works under live lifespan (regression guard)
# ---------------------------------------------------------------------------


class TestLegacyRESTUnderLiveLifespan:
    """Verify REST endpoints still work with the MCP lifespan composition."""

    async def test_health_endpoint(self, live_client):
        """GET /health returns 200 (unaffected by MCP lifespan fix)."""
        resp = await live_client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    async def test_get_tools_endpoint(self, live_client):
        """GET /tools returns 6 tools (REST path unaffected)."""
        resp = await live_client.get("/tools")
        assert resp.status_code == 200
        tools = resp.json()
        assert len(tools) == 7

    async def test_post_call_endpoint(self, live_client):
        """POST /call still routes correctly (REST path unaffected)."""
        resp = await live_client.post(
            "/call",
            content=json.dumps(
                {"name": "search", "arguments": {"query": "test", "scope": "code"}}
            ).encode(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "results" in body
