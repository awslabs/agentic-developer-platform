"""Tests for client_tool capture on the cost record.

Issue #4398 (EPIC #4324, FR-6.1–6.3). Capture WHICH tool made each proxied
request — Claude Code, Codex CLI, Cursor, the web chat — onto `usage_logs`.

Nothing in v1 reads this column (FR-6.3: no UI, no read endpoint), so these tests
are the ONLY thing standing between a silently-broken capture and a permanent hole
in the data. The column is **not back-fillable**: if capture writes NULL on every
row, no later fix can recover the history, and — because NULL legitimately means
"not captured" — nothing anywhere would look wrong. There is no dashboard to
notice, no alarm to fire, no user to complain. Hence the emphasis below on
asserting the value actually **reaches the writer**, not merely that the
normaliser returns the right string in isolation.

Test groups map to the issue's Validation list:

  1. `TestNormalizeClientTool`          — normalised value, distinct stable values (1, 6)
  2. `TestNormalizerNeverRaises`        — malformed/novel input → None, no raise (2, 3)
  3. `TestSetClientToolFromHeader`      — the route dependency's contract
  4. `TestContextvarSurvivesToWriteSite` — the #1755 trap (the real risk here)
  5. `TestCapturePersistsOnCostRecord`  — end-to-end: value lands on usage_logs (1)
  6. `TestNotCapturedIsNullNotASentinel` — NULL means not-captured, never a tool name (2, 4)
"""

import contextvars
from datetime import datetime, timedelta
from typing import Annotated
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from src.proxy.client_tool import (
    CLIENT_TOOL_CLAUDE_CODE,
    CLIENT_TOOL_CODEX_CLI,
    CLIENT_TOOL_CURSOR,
    CLIENT_TOOL_SDK,
    CLIENT_TOOL_WEB_CHAT,
    KNOWN_CLIENT_TOOLS,
    normalize_client_tool,
)
from src.proxy.routes import set_client_tool_from_header
from src.proxy.service import _current_client_tool
from src.shared.schemas.auth import TokenContext
from tests.proxy.pricing_fixtures import price_fixture_usage


@pytest.fixture(autouse=True)
def _reset_client_tool_contextvar():
    """Each test starts with the contextvar unset.

    Autouse because a leaked value from a previous test is exactly the
    cross-request contamination bug these tests exist to catch — a leaky fixture
    would mask it.
    """
    token = _current_client_tool.set(None)
    yield
    _current_client_tool.reset(token)


# ============================================================================
# 1. Normalisation — Validation items 1 and 6
# ============================================================================


class TestNormalizeClientTool:
    """A recognisable client normalises to a stable member of the closed set."""

    @pytest.mark.parametrize(
        ("user_agent", "expected"),
        [
            # Real-world shapes: version suffixes and platform triples must not
            # defeat the match, which is why matching is substring-based.
            ("claude-cli/2.1.3 (external, cli)", CLIENT_TOOL_CLAUDE_CODE),
            ("claude-cli/1.0.0", CLIENT_TOOL_CLAUDE_CODE),
            ("Claude-Code/0.9 (darwin arm64)", CLIENT_TOOL_CLAUDE_CODE),
            ("codex_cli_rs/0.47.0 (Mac OS 15.6.0)", CLIENT_TOOL_CODEX_CLI),
            ("codex-cli/1.2.3", CLIENT_TOOL_CODEX_CLI),
            ("Cursor/1.7.44 (darwin)", CLIENT_TOOL_CURSOR),
            ("adp-web/1.0", CLIENT_TOOL_WEB_CHAT),
            ("anthropic-sdk-python/0.39.0", CLIENT_TOOL_SDK),
            ("openai-python/1.55.0", CLIENT_TOOL_SDK),
            ("Boto3/1.35.0 Python/3.12", CLIENT_TOOL_SDK),
        ],
    )
    def test_recognised_clients_normalise(self, user_agent, expected):
        assert normalize_client_tool(user_agent) == expected

    def test_case_is_insensitive(self):
        """UA casing varies by client and version; it must not change the value.

        Otherwise the same tool fragments across spellings — the "unnormalised
        values persisted" failure in the issue's impact table.
        """
        assert normalize_client_tool("CLAUDE-CLI/2.0") == normalize_client_tool("claude-cli/2.0") == CLIENT_TOOL_CLAUDE_CODE

    def test_versions_collapse_to_one_value(self):
        """Validation 6 (no spelling drift): versions must NOT fragment.

        This is the point of normalising at all — persisting the raw UA would put
        every version in its own bucket and make a future breakdown useless.
        """
        values = {
            normalize_client_tool("claude-cli/1.0.0"),
            normalize_client_tool("claude-cli/2.1.3 (external, cli)"),
            normalize_client_tool("claude-cli/99.0.0-beta"),
        }
        assert values == {CLIENT_TOOL_CLAUDE_CODE}

    def test_two_different_clients_get_two_distinct_values(self):
        """Validation 6: recognised clients must not collapse into each other."""
        claude = normalize_client_tool("claude-cli/2.1.3")
        codex = normalize_client_tool("codex_cli_rs/0.47.0")
        assert claude != codex
        assert {claude, codex} <= KNOWN_CLIENT_TOOLS

    def test_all_distinct_clients_map_to_distinct_values(self):
        """Stronger form of Validation 6 across the whole vocabulary.

        Guards the ordered-substring matcher: adding a marker that shadows an
        existing one (e.g. a bare "claude" ahead of "claude-cli") would silently
        collapse two tools into one bucket, and this fails when it does.
        """
        observed = {
            normalize_client_tool("claude-cli/2.1.3"),
            normalize_client_tool("codex_cli_rs/0.47.0"),
            normalize_client_tool("Cursor/1.7.44"),
            normalize_client_tool("adp-web/1.0"),
            normalize_client_tool("openai-python/1.55.0"),
        }
        assert len(observed) == 5

    def test_returns_only_members_of_the_closed_set(self):
        """Never a raw UA, never an ad-hoc string — only the declared vocabulary."""
        for ua in ("claude-cli/2.1.3", "codex_cli_rs/0.4", "Cursor/1.0", "adp-web/1", "boto3/1.35"):
            assert normalize_client_tool(ua) in KNOWN_CLIENT_TOOLS

    def test_raw_user_agent_is_never_returned(self):
        """The persisted value must not be the raw header.

        A raw UA can carry a hostname or a path; it is also unbounded. Neither
        belongs in this column.
        """
        ua = "claude-cli/2.1.3 (external, cli) on jordan-mbp"
        assert normalize_client_tool(ua) != ua
        assert len(normalize_client_tool(ua)) < len(ua)

    def test_cursor_wins_over_embedded_sdk_marker(self):
        """Marker precedence is deliberate: attribute the editor, not its transport.

        Cursor can send both its own marker and a vendor SDK's. The editor is the
        answer to "which tool made this request".
        """
        assert normalize_client_tool("Cursor/1.7.44 openai-python/1.55.0") == CLIENT_TOOL_CURSOR


# ============================================================================
# 2. Failure posture — Validation items 2 and 3
# ============================================================================


class TestNormalizerNeverRaises:
    """Capture is best-effort: it must never fail the request that carried it.

    This runs on the hot path for EVERY proxied request, so an input that raises
    here is an availability incident caused by a reporting field. The issue calls
    this out explicitly as a blast-radius class.
    """

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "   ",
            "\x00\x01\x02",
            "unknown-client/1.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "🙈🙉🙊",
            "claude" * 5000,
            "%s %d {} \\x00 ../../etc/passwd",
            "'; DROP TABLE usage_logs; --",
            b"claude-cli/2.0",
            12345,
            object(),
            ["claude-cli/2.0"],
        ],
    )
    def test_never_raises_on_hostile_or_novel_input(self, value):
        """Validation 3: malformed input → no exception. Result is None or valid."""
        result = normalize_client_tool(value)
        assert result is None or result in KNOWN_CLIENT_TOOLS

    @pytest.mark.parametrize("value", [None, "", "   ", "unknown-client/1.0", "Mozilla/5.0", 12345, b"x"])
    def test_unrecognised_resolves_to_none(self, value):
        """Validation 2/3: unrecognised, absent or malformed → None."""
        assert normalize_client_tool(value) is None

    def test_no_sentinel_string_is_ever_returned(self):
        """FR-6.2: "not captured" is None, never a string like "unknown".

        A sentinel would be indistinguishable from a real tool to a future
        consumer and would corrupt the breakdown — the exact failure the issue
        names. NULL and "unknown" are not interchangeable.
        """
        for value in (None, "", "totally-novel-agent/9", "Mozilla/5.0"):
            result = normalize_client_tool(value)
            assert result is None
            assert result not in ("unknown", "other", "unrecognised", "none", "null", "")

    def test_pathological_length_is_bounded(self):
        """A 10 MB header must not turn into unbounded CPU on the hot path.

        Asserted behaviourally: a marker beyond the scan window is not matched,
        which is what proves the scan is bounded rather than merely fast today.
        """
        assert normalize_client_tool("x" * 100_000 + "claude-cli/2.0") is None
        assert normalize_client_tool("claude-cli/2.0" + "x" * 100_000) == CLIENT_TOOL_CLAUDE_CODE


# ============================================================================
# 3. The route dependency
# ============================================================================


class TestSetClientToolFromHeader:
    """The dependency derives, normalises, and publishes the value."""

    @staticmethod
    def _request(headers: dict[str, str]):
        request = MagicMock()
        request.headers = headers
        return request

    @pytest.mark.asyncio
    async def test_sets_contextvar_from_user_agent(self):
        result = await set_client_tool_from_header(self._request({"user-agent": "claude-cli/2.1.3"}))
        assert result == CLIENT_TOOL_CLAUDE_CODE
        assert _current_client_tool.get() == CLIENT_TOOL_CLAUDE_CODE

    @pytest.mark.asyncio
    async def test_missing_header_sets_none(self):
        """Validation 2: no recognisable client → None, no error."""
        result = await set_client_tool_from_header(self._request({}))
        assert result is None
        assert _current_client_tool.get() is None

    @pytest.mark.asyncio
    async def test_unrecognised_header_sets_none_without_raising(self):
        """Validation 3: a novel client must not raise inside the dependency."""
        result = await set_client_tool_from_header(self._request({"user-agent": "brand-new-tool/0.1"}))
        assert result is None

    @pytest.mark.asyncio
    async def test_contextvar_is_set_unconditionally(self):
        """A UA-less request must NOT inherit a previous request's tool.

        ContextVars can carry a value into a task that did not set one, so a
        conditional `if tool:` would mis-attribute this request to whatever ran
        before it — silently, and wrongly, as real captured data.
        """
        _current_client_tool.set(CLIENT_TOOL_CURSOR)
        await set_client_tool_from_header(self._request({}))
        assert _current_client_tool.get() is None

    def test_dependency_is_async(self):
        """Guards the #1755 trap at the signature level.

        A contextvar set in a SYNC dependency is lost before the endpoint runs
        (Starlette runs sync deps in a threadpool, which copies the context), so
        `agent_run_id` read NULL on 100% of rows until it was patched by threading
        the value through eight call sites. If someone "simplifies" this to `def`,
        capture silently returns to writing NULL forever with no error — so the
        async-ness is asserted, not just commented.
        """
        import inspect

        assert inspect.iscoroutinefunction(set_client_tool_from_header)


# ============================================================================
# 4. The #1755 trap — the actual risk in this change
# ============================================================================


class TestContextvarSurvivesToWriteSite:
    """The value must survive the route → `_log_usage` boundary.

    This is where the `agent_run_id` precedent FAILED (#1755). These tests run the
    dependency through a real ASGI app and read the contextvar from a callee, the
    same way `_log_usage` does. They are the reason this change does not need to
    thread a new parameter through every proxy signature — and the guard that
    keeps that true.
    """

    @staticmethod
    def _app():
        app = FastAPI()

        async def read_from_a_callee() -> str | None:
            """Stands in for `_log_usage`, which is several frames deep."""
            return _current_client_tool.get()

        @app.get("/probe")
        async def probe(_ct: Annotated[str | None, Depends(set_client_tool_from_header)]):
            return {"at_write_site": await read_from_a_callee()}

        return app

    def test_value_reaches_a_callee_of_the_endpoint(self):
        """The non-streaming path: dependency → endpoint → nested callee."""
        client = TestClient(self._app())
        body = client.get("/probe", headers={"user-agent": "claude-cli/2.1.3"}).json()
        assert body["at_write_site"] == CLIENT_TOOL_CLAUDE_CODE

    def test_value_reaches_a_streaming_generators_finally(self):
        """The streaming paths log usage in a generator's `finally`.

        Half the proxy routes are streaming, so if the contextvar did not survive
        into the generator, streaming traffic would be uncaptured — and streaming
        is the dominant shape for the agent clients this column exists to identify.
        """
        seen: dict[str, str | None] = {}
        app = FastAPI()

        async def chunks():
            try:
                yield b"data: hi\n\n"
            finally:
                seen["value"] = _current_client_tool.get()

        @app.get("/stream")
        async def stream(_ct: Annotated[str | None, Depends(set_client_tool_from_header)]):
            return StreamingResponse(chunks(), media_type="text/event-stream")

        client = TestClient(app)
        client.get("/stream", headers={"user-agent": "codex_cli_rs/0.47.0"})
        assert seen["value"] == CLIENT_TOOL_CODEX_CLI

    def test_a_sync_dependency_would_lose_the_value(self):
        """Documents WHY the dependency is async, by demonstrating the failure.

        If this ever stops holding, the async requirement is obsolete and the
        comments in `routes.py` should be revisited. Until then it is the
        executable evidence for a non-obvious design choice.
        """
        probe: contextvars.ContextVar[str | None] = contextvars.ContextVar("probe", default=None)
        app = FastAPI()

        def sync_dep(request: Request) -> str | None:
            probe.set(normalize_client_tool(request.headers.get("user-agent")))
            return probe.get()

        @app.get("/sync")
        async def route(_ct: Annotated[str | None, Depends(sync_dep)]):
            return {"at_write_site": probe.get()}

        client = TestClient(app)
        body = client.get("/sync", headers={"user-agent": "claude-cli/2.1.3"}).json()
        assert body["at_write_site"] is None

    def test_requests_do_not_leak_into_each_other(self):
        """Two sequential requests get their own values, not the previous one's."""
        client = TestClient(self._app())
        first = client.get("/probe", headers={"user-agent": "Cursor/1.7.44"}).json()
        second = client.get("/probe", headers={"user-agent": "claude-cli/2.1.3"}).json()
        third = client.get("/probe", headers={"user-agent": "unknown-thing/1"}).json()
        assert first["at_write_site"] == CLIENT_TOOL_CURSOR
        assert second["at_write_site"] == CLIENT_TOOL_CLAUDE_CODE
        assert third["at_write_site"] is None


# ============================================================================
# 5. End-to-end: the value lands on the cost record — Validation item 1
# ============================================================================


def _token_context() -> TokenContext:
    return TokenContext(
        user_id="test-user-123",
        org_id="test-org-456",
        team_id="test-team-789",
        department_id="test-dept-012",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now() + timedelta(hours=12),
    )


class _CapturingUsageService:
    """Captures the kwargs `log_request` was called with.

    Substituted for the real `UsageService` so the assertion is on what the writer
    receives — the boundary that actually decides what lands in the column.
    """

    calls: list[dict] = []

    def __init__(self, session):  # noqa: D107 - mirrors UsageService(session)
        pass

    async def log_request(self, **kwargs):
        type(self).calls.append(kwargs)


class TestCapturePersistsOnCostRecord:
    """`_log_usage` reads the contextvar and passes it to the writer."""

    @staticmethod
    def _session_factory():
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return MagicMock(return_value=session)

    async def _invoke_log_usage(self, client_tool_value: str | None) -> dict:
        """Run the real `ProxyService._log_usage` with the contextvar set."""
        from src.proxy.service import ProxyService

        _CapturingUsageService.calls = []
        _current_client_tool.set(client_tool_value)

        service = ProxyService.__new__(ProxyService)  # bypass __init__'s deps

        with (
            patch("src.proxy.service.get_session_factory", self._session_factory),
            patch("src.proxy.service.UsageService", _CapturingUsageService),
            patch("src.proxy.service.reconcile_budget_reservation", AsyncMock()),
        ):
            await service._log_usage(
                context=_token_context(),
                model="anthropic.claude-3-5-sonnet",
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.0012,
                latency_ms=900,
                status_code=200,
            )

        assert _CapturingUsageService.calls, "log_request was never called"
        return _CapturingUsageService.calls[0]

    @pytest.mark.asyncio
    async def test_captured_value_reaches_the_writer(self):
        """Validation 1: a recognised client persists a normalised value."""
        kwargs = await self._invoke_log_usage(CLIENT_TOOL_CLAUDE_CODE)
        assert kwargs["client_tool"] == CLIENT_TOOL_CLAUDE_CODE

    @pytest.mark.asyncio
    async def test_uncaptured_reaches_the_writer_as_none(self):
        """Validation 2: not-captured is passed as None, so the column is NULL."""
        kwargs = await self._invoke_log_usage(None)
        assert kwargs["client_tool"] is None

    @pytest.mark.asyncio
    async def test_mantle_path_also_captures(self):
        """The OpenAI passthrough is the SECOND write site and must not be NULL.

        Wiring only the Bedrock path would leave every mantle row uncaptured —
        indistinguishable from "not captured", so a future breakdown would
        under-report this route with nothing indicating why.
        """
        from src.proxy.mantle_service import MantlePassthroughService

        _CapturingUsageService.calls = []
        _current_client_tool.set(CLIENT_TOOL_CODEX_CLI)

        service = MantlePassthroughService(MagicMock(), "https://bedrock-mantle.us-east-1.api.aws")

        with (
            patch("src.proxy.mantle_service.get_session_factory", self._session_factory),
            patch("src.proxy.mantle_service.UsageService", _CapturingUsageService),
            patch("src.proxy.mantle_service.reconcile_budget_reservation", AsyncMock()),
            patch("src.proxy.mantle_service.price_completed_usage", AsyncMock(side_effect=price_fixture_usage)),
        ):
            await service._log_usage(
                context=_token_context(),
                model="openai.gpt-5",
                usage={"input_tokens": 10, "output_tokens": 20},
                latency_ms=100,
                status_code=200,
                request_id="req-1",
                agent_run_id=None,
            )

        assert _CapturingUsageService.calls, "mantle log_request was never called"
        assert _CapturingUsageService.calls[0]["client_tool"] == CLIENT_TOOL_CODEX_CLI

    @pytest.mark.asyncio
    async def test_capture_does_not_change_other_recorded_fields(self):
        """Adding capture must not disturb the cost data already being recorded."""
        kwargs = await self._invoke_log_usage(CLIENT_TOOL_CURSOR)
        assert kwargs["input_tokens"] == 100
        assert kwargs["output_tokens"] == 50
        assert kwargs["cost_usd"] == 0.0012
        assert kwargs["status_code"] == 200


# ============================================================================
# 6. NULL semantics on the model — Validation items 2 and 4
# ============================================================================


class TestNotCapturedIsNullNotASentinel:
    """FR-6.2 at the storage layer: NULL is "not captured", never a tool name."""

    def test_model_default_is_null_not_a_placeholder(self):
        """Validation 4: an unpopulated row reads back as None, not a tool name.

        This is the pre-migration / non-gateway-path row. If the model carried a
        default, those rows would read as a real tool.
        """
        from src.shared.models.usage import UsageLog

        row = UsageLog(
            org_id="org-a",
            department_id="d",
            team_id="t",
            user_id="u",
            account_type="human",
            model="m",
            input_tokens=1,
            output_tokens=2,
            cost_usd=0,
            latency_ms=10,
            status_code=200,
        )
        assert row.client_tool is None

    def test_none_is_not_a_member_of_the_known_set(self):
        """ "Not captured" must never be confusable with a captured tool.

        Any future consumer separating the two relies on this: None is outside the
        vocabulary, so a breakdown that groups by the set cannot accidentally
        render uncaptured rows as a tool.
        """
        assert None not in KNOWN_CLIENT_TOOLS

    def test_known_set_contains_no_unknown_style_sentinel(self):
        """Guards against a future edit "helpfully" adding an unknown bucket.

        Such a bucket would collapse "we did not capture this" into "the user ran
        a tool called unknown" — losing the distinction permanently, since the
        data cannot be re-derived.
        """
        assert not ({"unknown", "other", "none", "null", "unrecognised", ""} & KNOWN_CLIENT_TOOLS)

    def test_writer_accepts_none_for_client_tool(self):
        """`log_request`'s signature must default to None, not a placeholder.

        Callers that predate this change (and any future one that forgets the
        kwarg) must produce NULL rather than a fabricated tool.
        """
        import inspect

        from src.usage.service import UsageService

        assert inspect.signature(UsageService.log_request).parameters["client_tool"].default is None
