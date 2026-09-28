"""Tests for agent_run_id contextvar propagation to usage_logs.

Issue #1616: Verify that the X-Agent-RunId header reaches _log_usage
and that the contextvar mechanism correctly passes the value through.
"""

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
