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

from door import server as door_server
from door.acl import SearchHit
from door.search_backend import ZoektUnavailable
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


@pytest.fixture
def healthy_code_backend(monkeypatch):
    class HealthyIndex:
        def __init__(self):
            self.calls = 0

        async def search(self, query, *, limit):
            self.calls += 1
            return [SearchHit(repo_name="fixture/private", data={"file": "private.py", "fact": "private-sentinel"})]

    index = HealthyIndex()
    monkeypatch.setattr(door_server.state, "zoekt", index)
    return index


@pytest.fixture
def unavailable_code_backend(monkeypatch):
    class UnavailableIndex:
        async def search(self, query, *, limit):
            raise ZoektUnavailable("isolated backend unavailable")

    monkeypatch.setattr(door_server.state, "zoekt", UnavailableIndex())


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


@pytest.mark.door_auth
async def test_signed_secure_identify_and_plan_use_server_identity_on_both_transports(
    live_client, monkeypatch, caplog
):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from door import auth, server
    from door.acl import CallerPrincipal
    from tests.unit.test_door_auth import token
    from tests.unit.test_secure_identify_ownership import CataloguePool

    key = Ed25519PrivateKey.generate()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    monkeypatch.setattr(auth.config, "door_verification_keys", json.dumps({"test": public}))
    catalogue = CataloguePool()
    callers = []

    class Store:
        allowed = {"OrgA/Service", "OrgA/Empty"}
        unavailable = False

        def get_allowed_repos(self, caller):
            callers.append(caller)
            if self.unavailable:
                raise RuntimeError("synthetic ACL outage")
            return self.allowed

    store = Store()
    monkeypatch.setattr(server.state, "db_pool", catalogue)
    monkeypatch.setattr(server.state, "acl_store", store)
    monkeypatch.setattr(server.config, "project_filter_enabled", True)

    def forbid_project_lookup(*args, **kwargs):
        raise AssertionError("secure dispatch must not read project metadata before ACL admission")

    monkeypatch.setattr(server, "_resolve_project_scope", forbid_project_lookup)

    async def invoke(path, action, repo):
        arguments = {"action": action, "cve": "CVE-2026-1234", "project": "untrusted"}
        if repo is not None:
            arguments["repo"] = repo
        if path == "/call":
            payload = {"name": "secure", "arguments": arguments}
        else:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "secure", "arguments": arguments},
            }
        body = json.dumps(payload).encode()
        response = await live_client.post(
            path,
            content=body,
            headers={
                "Content-Type": "application/json",
                "x-adp-door-identity": token(key, path=path, body=body),
                "x-github-login": "mallory",
                "x-tenant-id": "tenant-b",
                "x-owner-sub": "mallory",
            },
        )
        assert response.status_code == 200
        return (
            response.json()
            if path == "/call"
            else json.loads(_parse_sse_json(response.text)["result"]["content"][0]["text"])
        )

    try:
        for path in ("/call", "/mcp/"):
            for action in ("identify", "plan"):
                catalogue.queries.clear()
                foreign = await invoke(path, action, "OrgB/Service")
                unknown = await invoke(path, action, "Unknown/Service")
                assert json.dumps(foreign, sort_keys=True).replace(
                    "OrgB/Service", "<repo>"
                ) == json.dumps(unknown, sort_keys=True).replace("Unknown/Service", "<repo>")
                basename = await invoke(path, action, "Service")
                domain_alias = await invoke(path, action, "github.com/OrgB/Service")
                assert not catalogue.queries
                assert all(
                    result["metadata"]["repo_indexed"] is False
                    for result in (foreign, unknown, basename, domain_alias)
                )
                owned = await invoke(path, action, "github.com/orga/service")
                assert owned["findings_count"] == 1
                assert owned["findings"][0]["repos_affected"] == ["OrgA/Service"]
                catalogue.queries.clear()
                empty = await invoke(path, action, "OrgA/Empty")
                assert empty["findings_count"] == 0
                assert empty["metadata"] == {"cve_known": True, "repo_indexed": True}
                assert any(
                    "FROM repositories WHERE repo_name = %s" in sql and params == ("OrgA/Empty",)
                    for sql, params in catalogue.queries
                )
                assert all(
                    caller.tenant_id == "tenant-a" and caller.github_login == "alice"
                    for caller in callers
                )
                catalogue.queries.clear()
                store.allowed = set()
                denied = await invoke(path, action, "OrgA/Service")
                denied_unknown = await invoke(path, action, "Unknown/Service")
                assert denied["findings_count"] == 0
                assert denied["metadata"]["repo_indexed"] is False
                assert json.dumps(denied, sort_keys=True).replace(
                    "OrgA/Service", "<repo>"
                ) == json.dumps(denied_unknown, sort_keys=True).replace("Unknown/Service", "<repo>")
                assert not catalogue.queries

                store.allowed = {"OrgA/Service", "OrgA/Empty"}
                store.unavailable = True
                unavailable = await invoke(path, action, "OrgA/Service")
                unavailable_foreign = await invoke(path, action, "OrgB/Service")
                assert unavailable["findings_count"] == 0
                assert unavailable["metadata"]["repo_indexed"] is False
                assert json.dumps(unavailable, sort_keys=True).replace(
                    "OrgA/Service", "<repo>"
                ) == json.dumps(unavailable_foreign, sort_keys=True).replace(
                    "OrgB/Service", "<repo>"
                )
                assert not catalogue.queries
                store.unavailable = False

                monkeypatch.setattr(server.state, "acl_store", None)
                missing = await invoke(path, action, "OrgA/Service")
                missing_foreign = await invoke(path, action, "OrgB/Service")
                assert missing["code"] == "acl_store_unavailable"
                assert missing == missing_foreign
                assert not catalogue.queries
                monkeypatch.setattr(server.state, "acl_store", store)
            catalogue.queries.clear()
            unfiltered = await invoke(path, "identify", None)
            assert unfiltered["findings_count"] == 1
            dependency_queries = [sql for sql, _ in catalogue.queries if "JOIN repositories" in sql]
            assert dependency_queries and all(
                "r.repo_name = ANY(%s)" in sql for sql in dependency_queries
            )

        assert "reason=secure_repo_not_permitted" in caplog.text
        assert "reason=secure_repo_acl_unavailable" in caplog.text
        assert "reason=acl_store_unavailable" in caplog.text

        catalogue.queries.clear()
        mismatched_caller = CallerPrincipal(github_login="mallory", tenant_id="tenant-b")
        rederived = await server._dispatch_tool(
            "secure",
            {"action": "identify", "repo": "OrgA/Service"},
            {"x-github-login": "alice", "x-tenant-id": "tenant-a"},
            mismatched_caller,
        )
        assert rederived["findings_count"] == 1
        assert callers[-1].github_login == "alice" and callers[-1].tenant_id == "tenant-a"
        catalogue.queries.clear()
        refused = await server._dispatch_tool(
            "secure",
            {"action": "identify", "repo": "OrgA/Service"},
            {},
            mismatched_caller,
        )
        assert refused["error"] == "unauthorized"
        assert not catalogue.queries
    finally:
        catalogue.db.close()


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

    async def test_mcp_search_no_identity_returns_empty(self, live_client, acl_store_live, healthy_code_backend):
        """MCP tools/call without identity filters private indexed results.

        ``acl_store_live`` supplies a reachable but empty ACL store. The real
        lifespan cannot build one here (no database in a unit test), and without
        the fixture the Door would answer with its store-unavailable refusal
        (#5658) — so this test would stop measuring the identity gate it names.
        """
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
        assert parsed["results"] == []
        assert "private-sentinel" not in str(parsed)
        assert healthy_code_backend.calls == 1


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

    async def test_post_call_endpoint(self, live_client, acl_store_live, unavailable_code_backend):
        """POST /call still routes correctly (REST path unaffected).

        ``acl_store_live`` is required for the same reason as above: this test's
        subject is REST routing surviving the MCP lifespan composition, so the
        request has to reach the verb rather than stop at the #5658 ACL-store
        gate.
        """
        resp = await live_client.post(
            "/call",
            content=json.dumps(
                {"name": "search", "arguments": {"query": "test", "scope": "code"}}
            ).encode(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "error": "Knowledge search backend is unavailable.",
            "code": "knowledge_backend_unavailable",
        }
