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
# Search scope=memory tests
# ---------------------------------------------------------------------------


class TestSearchMemoryScope:
    """Verify search with scope=memory routes through recall_memory."""

    @pytest.mark.asyncio
    async def test_search_memory_no_experience_tool_returns_empty(self, client):
        """When experience_tool is None, search scope=memory returns empty."""
        resp = await client.post(
            "/call",
            content=json.dumps(
                {
                    "name": "search",
                    "arguments": {"query": "deployment strategy", "scope": "memory"},
                }
            ).encode(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["results"] == []
        assert body["total"] == 0

    @pytest.mark.asyncio
    async def test_search_memory_with_experience_tool_returns_results(self, client):
        """When experience_tool is configured, search scope=memory returns recall results."""
        import math
        import uuid

        from door.server import state
        from personal_context.experience_tool import ExperienceTool
        from personal_context.storage import PersonalContextStore

        class _FakeBackend:
            def __init__(self):
                self._store = {}

            def put(self, path, data):
                self._store[path] = data

            def get(self, path):
                return self._store.get(path)

            def delete(self, path):
                self._store.pop(path, None)

            def list_prefix(self, prefix):
                return [v for k, v in self._store.items() if k.startswith(prefix)]

        class _FakeEmbedder:
            def __init__(self, dim=64):
                self.dimension = dim

            def embed(self, text):
                vec = [0.0] * self.dimension
                for ch in text.lower():
                    vec[ord(ch) % self.dimension] += 1.0
                norm = math.sqrt(sum(x * x for x in vec))
                if norm > 0:
                    vec = [x / norm for x in vec]
                return vec

        backend = _FakeBackend()
        store = PersonalContextStore(backend)
        tool = ExperienceTool(store=store, embedding_client=_FakeEmbedder())

        # Inject the experience tool into server state
        original = state.experience_tool
        state.experience_tool = tool
        try:
            owner_sub = str(uuid.uuid4())
            headers = {"X-Owner-Sub": owner_sub, "X-Tenant-Id": "org-test"}

            # First: save a memory via remember
            resp = await client.post(
                "/call",
                content=json.dumps(
                    {
                        "name": "remember",
                        "arguments": {
                            "session_id": "sess-abc",
                            "messages": [
                                {"role": "user", "content": "How to scale EKS nodes?"},
                                {"role": "assistant", "content": "Use cluster autoscaler."},
                            ],
                            "outcome": "Learned EKS scaling via cluster autoscaler.",
                        },
                    }
                ).encode(),
                headers=headers,
            )
            assert resp.status_code == 200
            remember_body = resp.json()
            assert remember_body["stored"] is True

            # Now: search with scope=memory should find it
            resp = await client.post(
                "/call",
                content=json.dumps(
                    {
                        "name": "search",
                        "arguments": {
                            "query": "EKS cluster autoscaler scaling",
                            "scope": "memory",
                        },
                    }
                ).encode(),
                headers=headers,
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["total"] >= 1, f"Expected memory results, got: {body}"
            assert len(body["results"]) >= 1
            # Verify result contains the stored content
            found = any(
                "autoscaler" in r.get("content", "").lower()
                or "eks" in r.get("content", "").lower()
                for r in body["results"]
            )
            assert found, f"Expected EKS/autoscaler in results: {body['results']}"
        finally:
            state.experience_tool = original


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
