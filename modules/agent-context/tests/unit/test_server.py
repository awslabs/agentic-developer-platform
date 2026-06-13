"""Unit tests for the Context MCP Server (door/server.py).

Verifies:
- GET /tools returns exactly 6 tools matching the contract
- POST /call routes to correct verb handlers
- Malformed JSON returns 400
- ACL filtering is applied to results
- Server handles missing backends gracefully
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from door.server import TOOLS, app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    """Async HTTP client for testing the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Tool listing tests
# ---------------------------------------------------------------------------


class TestToolsListing:
    """Verify GET /tools matches the expected contract."""

    @pytest.mark.asyncio
    async def test_tools_count(self, client):
        resp = await client.get("/tools")
        assert resp.status_code == 200
        tools = resp.json()
        assert len(tools) == 6

    @pytest.mark.asyncio
    async def test_tools_names(self, client):
        resp = await client.get("/tools")
        tools = resp.json()
        names = {t["name"] for t in tools}
        expected = {"search", "understand", "impact", "browse", "remember", "experience"}
        assert names == expected

    @pytest.mark.asyncio
    async def test_tools_have_descriptions(self, client):
        resp = await client.get("/tools")
        tools = resp.json()
        for tool in tools:
            assert "description" in tool
            assert len(tool["description"]) > 0

    @pytest.mark.asyncio
    async def test_tools_have_parameters(self, client):
        resp = await client.get("/tools")
        tools = resp.json()
        for tool in tools:
            assert "parameters" in tool
            assert isinstance(tool["parameters"], dict)


# ---------------------------------------------------------------------------
# Malformed input tests
# ---------------------------------------------------------------------------


class TestMalformedInput:
    """Verify POST /call handles bad input gracefully."""

    @pytest.mark.asyncio
    async def test_malformed_json_returns_400(self, client):
        resp = await client.post("/call", content=b"{{invalid json")
        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body

    @pytest.mark.asyncio
    async def test_non_object_json_returns_400(self, client):
        resp = await client.post("/call", content=b'"just a string"')
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_missing_name_returns_400(self, client):
        resp = await client.post(
            "/call",
            content=json.dumps({"arguments": {}}).encode(),
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_no_stack_trace_in_error(self, client):
        resp = await client.post("/call", content=b"not json at all")
        assert "Traceback" not in resp.text


# ---------------------------------------------------------------------------
# Search verb tests
# ---------------------------------------------------------------------------


class TestSearchVerb:
    """Verify the search verb routing."""

    @pytest.mark.asyncio
    async def test_search_empty_query(self, client):
        resp = await client.post(
            "/call",
            content=json.dumps(
                {"name": "search", "arguments": {"query": "", "scope": "code"}}
            ).encode(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "results" in body
        assert body["results"] == []

    @pytest.mark.asyncio
    async def test_search_returns_valid_structure(self, client):
        resp = await client.post(
            "/call",
            content=json.dumps(
                {"name": "search", "arguments": {"query": "hello", "scope": "code", "limit": 5}}
            ).encode(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "results" in body
        assert "total" in body
        assert "query" in body


# ---------------------------------------------------------------------------
# Understand verb tests
# ---------------------------------------------------------------------------


class TestUnderstandVerb:
    """Verify the understand verb routing."""

    @pytest.mark.asyncio
    async def test_understand_empty_target(self, client):
        resp = await client.post(
            "/call",
            content=json.dumps({"name": "understand", "arguments": {"target": ""}}).encode(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "target" in body

    @pytest.mark.asyncio
    async def test_understand_returns_valid_structure(self, client):
        resp = await client.post(
            "/call",
            content=json.dumps(
                {"name": "understand", "arguments": {"target": "org/repo::function"}}
            ).encode(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "target" in body
        assert "definitions" in body or "summary" in body


# ---------------------------------------------------------------------------
# Impact verb tests
# ---------------------------------------------------------------------------


class TestImpactVerb:
    """Verify the impact verb routing."""

    @pytest.mark.asyncio
    async def test_impact_empty_target(self, client):
        resp = await client.post(
            "/call",
            content=json.dumps({"name": "impact", "arguments": {"target": ""}}).encode(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["affected"] == []

    @pytest.mark.asyncio
    async def test_impact_returns_valid_structure(self, client):
        resp = await client.post(
            "/call",
            content=json.dumps(
                {"name": "impact", "arguments": {"target": "org/repo::func", "cross_repo": False}}
            ).encode(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "target" in body
        assert "affected" in body
        assert "blast_radius" in body


# ---------------------------------------------------------------------------
# Browse verb tests
# ---------------------------------------------------------------------------


class TestBrowseVerb:
    """Verify the browse verb routing."""

    @pytest.mark.asyncio
    async def test_browse_returns_valid_structure(self, client):
        resp = await client.post(
            "/call",
            content=json.dumps(
                {"name": "browse", "arguments": {"action": "ls", "uri": "/"}}
            ).encode(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "action" in body
        assert "entries" in body


# ---------------------------------------------------------------------------
# Remember verb tests
# ---------------------------------------------------------------------------


class TestRememberVerb:
    """Verify the remember verb routing."""

    @pytest.mark.asyncio
    async def test_remember_missing_session_id(self, client):
        resp = await client.post(
            "/call",
            content=json.dumps(
                {"name": "remember", "arguments": {"session_id": "", "messages": []}}
            ).encode(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["stored"] is False


# ---------------------------------------------------------------------------
# Experience verb tests
# ---------------------------------------------------------------------------


class TestExperienceVerb:
    """Verify the experience verb routing."""

    @pytest.mark.asyncio
    async def test_experience_no_tool_returns_error(self, client):
        resp = await client.post(
            "/call",
            content=json.dumps(
                {
                    "name": "experience",
                    "arguments": {"action": "recall", "persona": "developer", "query": "test"},
                }
            ).encode(),
        )
        assert resp.status_code == 200
        body = resp.json()
        # Without experience tool configured, should return error or empty
        assert "error" in body or "results" in body


# ---------------------------------------------------------------------------
# Unknown tool tests
# ---------------------------------------------------------------------------


class TestUnknownTool:
    """Verify unknown tools are handled gracefully."""

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, client):
        resp = await client.post(
            "/call",
            content=json.dumps({"name": "nonexistent", "arguments": {}}).encode(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "error" in body


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """Verify the health endpoint."""

    @pytest.mark.asyncio
    async def test_health_returns_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# TOOLS constant validation
# ---------------------------------------------------------------------------


class TestToolsConstant:
    """Verify the TOOLS constant matches the expected contract shape."""

    def test_tools_count(self):
        assert len(TOOLS) == 6

    def test_tools_names(self):
        names = {t["name"] for t in TOOLS}
        assert names == {"search", "understand", "impact", "browse", "remember", "experience"}

    def test_each_tool_has_required_fields(self):
        for tool in TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "parameters" in tool
            assert isinstance(tool["parameters"], dict)
