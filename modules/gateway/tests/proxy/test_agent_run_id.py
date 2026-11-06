"""Tests for agent_run_id contextvar propagation to usage_logs.

Issue #1616: Verify that the X-Agent-RunId header reaches _log_usage
and that the contextvar mechanism correctly passes the value through.
"""

import pytest
from fastapi import HTTPException

from src.proxy.service import _current_agent_run_id


async def test_asgi_requests_keep_run_identity_in_the_request_task():
    import asyncio

    import httpx
    from fastapi import Depends, FastAPI

    from src.proxy.routes import set_agent_run_id_from_header

    app = FastAPI()
    ready = asyncio.Event()
    entered = 0

    @app.get("/run")
    async def read_run(_run=Depends(set_agent_run_id_from_header)):
        nonlocal entered
        entered += 1
        if entered == 3:
            ready.set()
        await asyncio.wait_for(ready.wait(), 2)
        return {"run_id": _current_agent_run_id.get()}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(
            *(client.get("/run", headers={"x-agent-runid": run} if run else {}) for run in ["alice-run", "bob-run", None])
        )
    assert [response.json()["run_id"] for response in responses] == ["alice-run", "bob-run", None]


class TestAgentRunIdContextvar:
    """Test the _current_agent_run_id contextvar mechanism."""

    def test_default_is_none(self):
        """Contextvar defaults to None when not set."""
        # Reset to default
        _current_agent_run_id.set(None)
        assert _current_agent_run_id.get() is None

    def test_set_and_get(self):
        """Setting the contextvar makes it readable."""
        _current_agent_run_id.set("inv-test-123")
        assert _current_agent_run_id.get() == "inv-test-123"
        # Clean up
        _current_agent_run_id.set(None)

    def test_empty_string_treated_as_falsy(self):
        """Empty string is set but evaluable as falsy for the header check."""
        _current_agent_run_id.set("")
        # The value is stored
        assert _current_agent_run_id.get() == ""
        # But it's falsy
        assert not _current_agent_run_id.get()
        _current_agent_run_id.set(None)


class TestSetAgentRunIdFromHeader:
    """Test the route-level dependency that extracts the header."""

    async def test_extracts_header_value(self):
        """The dependency reads x-agent-runid header and sets contextvar."""
        from unittest.mock import MagicMock

        from src.proxy.routes import set_agent_run_id_from_header

        request = MagicMock()
        request.headers = {"x-agent-runid": "inv-from-header-456"}

        result = await set_agent_run_id_from_header(request)

        assert result == "inv-from-header-456"
        assert _current_agent_run_id.get() == "inv-from-header-456"
        # Clean up
        _current_agent_run_id.set(None)

    async def test_missing_header_sets_none(self):
        """No header → contextvar set to None, returns None."""
        from unittest.mock import MagicMock

        from src.proxy.routes import set_agent_run_id_from_header

        request = MagicMock()
        request.headers = {}

        result = await set_agent_run_id_from_header(request)

        assert result is None
        assert _current_agent_run_id.get() is None

    async def test_absent_header_no_error(self):
        """Missing header does not raise an error (graceful degrade)."""
        from unittest.mock import MagicMock

        from src.proxy.routes import set_agent_run_id_from_header

        request = MagicMock()
        request.headers = {"content-type": "application/json"}

        # Should not raise
        result = await set_agent_run_id_from_header(request)
        assert result is None
        _current_agent_run_id.set(None)


class TestInvokeRoutesHaveAgentRunIdDependency:
    """Issue #1753 regression: EVERY Bedrock-invoke route must wire
    set_agent_run_id_from_header, or usage_logs.agent_run_id is NULL and per-run
    cost never links. The #1616 work missed /model/{id}/invoke (the native URL
    the Claude SDK actually uses), so 100% of rows had agent_run_id=NULL.
    """

    def _dep_callables(self, route):
        return {d.call for d in route.dependant.dependencies}

    def test_all_invoke_routes_wire_agent_run_id(self):
        from src.proxy.routes import router, set_agent_run_id_from_header

        # Every POST route whose path drives a model invocation must carry the dep.
        invoke_paths = {
            "/v1/chat/completions",
            "/v1/messages",
            "/bedrock/invoke",
            "/bedrock/invoke-with-response-stream",
            "/model/{model_id}/invoke",
            "/model/{model_id}/invoke-with-response-stream",
        }
        seen = {}
        for route in router.routes:
            path = getattr(route, "path", None)
            if path in invoke_paths and "POST" in getattr(route, "methods", set()):
                seen[path] = set_agent_run_id_from_header in self._dep_callables(route)

        # All six must be present AND wired.
        missing = invoke_paths - set(seen)
        assert not missing, f"invoke routes not found: {missing}"
        unwired = [p for p, ok in seen.items() if not ok]
        assert not unwired, f"invoke routes missing agent_run_id dependency: {unwired}"


class TestHostRunRouteAllowlist:
    """#8223 cause 33: the host allowlist is the set the crew routers register.

    The literal inside the gate admitted the model invoke and the token refresh
    only, so the #8380 tools relay was refused on every call from a Dev Box.
    """

    HOST_ROUTES = {
        "/model/{model_id}/invoke",
        "/model/{model_id}/invoke-with-response-stream",
        "/crew/model-token/refresh",
        "/crew/tools",
        "/crew/host-restore/context",
        "/crew/host-restore/grant",
        "/crew/host-restore/release",
        "/crew/host-checkpoint/begin",
        "/crew/host-checkpoint/complete",
        "/crew/github-token",
        "/crew/aws-credentials",
        "/crew/orchestration/products",
        "/crew/orchestration/products/{product_id}/projects/{project_id}/sources",
        # #9522: adp-flow preflight reads on a Dev Box (owner-scoped, GET only, no writes).
        "/crew/orchestration/products/{product_id}/projects/{project_id}",
        "/crew/orchestration/projects/{project_id}/budget",
        "/crew/orchestration/github/status",
        "/crew/orchestration/flows",
        "/crew/orchestration/flows/{flow_id}",
        "/crew/orchestration/flows/{flow_id}/plans",
        "/crew/orchestration/flows/{flow_id}/decisions",
        "/crew/orchestration/flows/drafts/preview",
        "/crew/orchestration/flows/drafts",
        # #9640: a verified host run exchanges its proof for the owner's session (POST, mint only).
        "/crew/orchestration/owner-session",
        "/v1/projects/{project_id}/budget",
        "/openai/v1/responses",
    }

    def test_every_host_route_is_registered(self):
        import src.crew.checkpoint_routes  # noqa: F401  (registers at import)
        import src.crew.host_aws  # noqa: F401
        import src.crew.host_github  # noqa: F401
        import src.crew.host_tools  # noqa: F401
        import src.crew.orchestration_read_routes  # noqa: F401
        import src.crew.proxy  # noqa: F401
        import src.products.budget_routes  # noqa: F401
        import src.proxy.routes  # noqa: F401
        from src.crew.host_routes import HOST_RUN_ROUTE_PATHS

        missing = self.HOST_ROUTES - HOST_RUN_ROUTE_PATHS
        assert not missing, f"host routes not admitted by the gate: {sorted(missing)}"
        # Nothing outside the known host surface is admitted.
        unexpected = HOST_RUN_ROUTE_PATHS - self.HOST_ROUTES
        assert not unexpected, f"routes admitted for host runs without review: {sorted(unexpected)}"

    def test_every_crew_route_that_wires_the_gate_is_registered(self):
        from src.crew import checkpoint_routes, host_aws, host_github, host_tools, orchestration_read_routes, proxy
        from src.crew.host_routes import HOST_RUN_ROUTE_PATHS
        from src.proxy.routes import set_agent_run_id_from_header

        unregistered = []
        for module in (proxy, host_tools, host_github, host_aws, checkpoint_routes, orchestration_read_routes):
            for route in module.router.routes:
                deps = {d.call for d in getattr(route, "dependant").dependencies}
                if set_agent_run_id_from_header in deps and route.path not in HOST_RUN_ROUTE_PATHS:
                    unregistered.append(route.path)
        assert not unregistered, f"gated crew routes missing host_run_route(): {unregistered}"

    @pytest.mark.parametrize("path,admitted", [("/crew/tools", True), ("/crew/model-token/refresh", True), ("/v1/messages", False)])
    async def test_gate_admits_registered_routes_for_a_verified_host(self, path, admitted, monkeypatch):
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import src.crew.host_tools  # noqa: F401  (registers /crew/tools)
        import src.proxy.routes as routes_module
        from src.proxy.routes import set_agent_run_id_from_header
        from src.shared.schemas.auth import TokenContext

        instance = "i-0b88826afeaa19a6e"
        role = "arn:aws:iam::000000000000:role/adp-dev-box-guest"
        row = {
            "kind": "crew",
            "target_kind": "ec2-instance",
            "status": "active",
            "executor_scope": "session",
            "is_human_rooted": True,
            "root_human_id": "owner",
            "user_id": "owner",
            "target_ref": instance,
            "host_role_arn": role,
        }
        monkeypatch.setattr("src.proxy.bedrock_principal.read_routing_run", AsyncMock(return_value=row))
        monkeypatch.setattr(
            "src.auth.caller_provenance.verified_caller_identity",
            lambda request: f"arn:aws:sts::000000000000:assumed-role/adp-dev-box-guest/{instance}",
        )
        monkeypatch.setattr("src.auth.agent_registry.parse_assumed_role_arn", lambda caller: role)
        context = TokenContext(
            user_id="dev-box-guest",
            org_id="__platform__",
            team_id="__agents__",
            department_id="",
            account_type="service",
            auth_source="iam",
            scope="internal",
            requires_host_identity=True,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        request = SimpleNamespace(
            headers={"x-agent-runid": "run-1"}, state=SimpleNamespace(token_context=context), scope={"route": SimpleNamespace(path=path)}
        )
        if admitted:
            assert await set_agent_run_id_from_header(request) == "run-1"
            assert context._dev_box_instance_id == instance and context._dev_box_role_arn == role
        else:
            with pytest.raises(HTTPException) as exc:
                await set_agent_run_id_from_header(request)
            assert exc.value.status_code == 403 and "model route" in exc.value.detail
        routes_module._current_agent_run_id.set(None)
