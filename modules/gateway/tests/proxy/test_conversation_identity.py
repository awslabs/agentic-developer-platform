"""Conversation identity on the cost record (#9024).

The ids that group requests into a conversation are on the wire but were never stored; nothing
is back-fillable, and NULL legitimately means "not captured", so — as for `client_tool` — the
tests assert the values reach the writer, not only that the parser is right.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.proxy.client_tool import CLIENT_TOOL_CLAUDE_CODE, CLIENT_TOOL_CODEX_CLI, CLIENT_TOOL_KIRO_CREW, normalize_client_tool
from src.proxy.conversation_identity import (
    DERIVED_PREFIX,
    ORIGIN_DEV_BOX,
    ORIGIN_DIRECT,
    ConversationIdentity,
    _current_conversation,
    claude_code_session_id,
    codex_thread_id,
    conversation_title,
    derived_conversation_id,
    forwarded_session_id,
    identify,
)
from src.proxy.routes import set_conversation_identity
from tests.proxy.pricing_fixtures import price_fixture_usage
from tests.proxy.test_client_tool_capture import _CapturingUsageService, _token_context

SESSION = "22222222-2222-4222-8222-222222222222"
CLAUDE_METADATA = {"user_id": f"user_7f3a_account_11111111-1111-1111-1111-111111111111_session_{SESSION}"}
THREAD = "01a176f5-6c8a-76c2-b24b-5c819646d8f9"


@pytest.fixture(autouse=True)
def _reset():
    _current_conversation.set(ConversationIdentity())
    yield
    _current_conversation.set(ConversationIdentity())


class TestParsers:
    def test_claude_code_session_id_from_metadata(self):
        assert claude_code_session_id(CLAUDE_METADATA) == SESSION
        assert claude_code_session_id({"user_id": "no-session-here"}) is None
        assert claude_code_session_id("not a dict") is None
        assert claude_code_session_id({"user_id": 42}) is None

    def test_codex_thread_from_header_then_prompt_cache_key(self):
        assert (
            codex_thread_id({"x-codex-turn-metadata": json.dumps({"thread_id": THREAD, "turn_id": "x"})}, {"prompt_cache_key": "other-key-1"})
            == THREAD
        )
        assert codex_thread_id({"x-codex-turn-metadata": "{not json"}, {"prompt_cache_key": "fallback-key-123"}) == "fallback-key-123"
        assert codex_thread_id({}, {"prompt_cache_key": "a b c"}) is None, "identifiers only; spaces are not an id"
        assert codex_thread_id(None, None) is None

    def test_title_is_the_first_user_turn_collapsed_redacted_and_bounded(self):
        anthropic = {
            "messages": [
                {"role": "assistant", "content": "hi"},
                {"role": "user", "content": [{"type": "text", "text": "  fix\n\nthe   bug with token ghp_abcdefghijklmnopqrstuvwxyz0123456 please"}]},
            ]
        }
        assert conversation_title(anthropic) == "fix the bug with token [redacted] please"
        openai = {
            "input": [
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "system"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x" * 500}]},
            ]
        }
        assert conversation_title(openai) == "x" * 120
        assert conversation_title({"input": "   "}) is None
        assert conversation_title({"messages": []}) is None
        assert conversation_title("nope") is None

    def test_title_skips_the_harness_injected_context_and_keeps_the_persons_words(self):
        reminder = "<system-reminder>\nCodebase and user instructions are shown below.\n</system-reminder>\n\nfix the login bug"
        assert conversation_title({"messages": [{"role": "user", "content": reminder}]}) == "fix the login bug"
        codex = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "<environment_context>\n<cwd>/home/ec2-user</cwd>\n</environment_context>"},
                        {"type": "input_text", "text": "rank repos by commits"},
                    ],
                }
            ]
        }
        assert conversation_title(codex) == "rank repos by commits"
        only_context = {"messages": [{"role": "user", "content": "<system-reminder>only this</system-reminder>"}]}
        assert conversation_title(only_context) == "<system-reminder>only this</system-reminder>", (
            "nothing else to show: keep the text rather than an empty title"
        )

    def test_new_user_agents_are_recognised(self):
        assert normalize_client_tool("codex-tui/0.164.0 (Ubuntu 24.4.0; x86_64) tmux-256color") == CLIENT_TOOL_CODEX_CLI
        assert (
            normalize_client_tool("codex_exec/0.164.0 (Amazon Linux 2023.9.20251014; x86_64) unknown (codex_exec; 0.164.0)") == CLIENT_TOOL_CODEX_CLI
        )
        assert normalize_client_tool("codex_sdk_ts/0.155.1 (Debian 13.0.0; x86_64) unknown (codex_exec; 0.155.1)") == CLIENT_TOOL_CODEX_CLI
        assert normalize_client_tool("kirocrew/0.8.0") == CLIENT_TOOL_KIRO_CREW


class TestFallbacks:
    def test_forwarded_session_header(self):
        assert forwarded_session_id({"x-adp-client-session": CLAUDE_METADATA["user_id"]}) == SESSION
        assert forwarded_session_id({"x-adp-client-session": SESSION}) == SESSION
        assert forwarded_session_id({"x-adp-client-session": "garbage"}) is None and forwarded_session_id(None) is None

    def test_derived_id_is_stable_for_one_conversation_and_distinct_across_people_prompts_and_days(self):
        body = {
            "system": "You are Claude Code",
            "messages": [{"role": "user", "content": "fix the bug"}, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "thanks"}],
        }
        first = derived_conversation_id("u1", CLIENT_TOOL_CLAUDE_CODE, body, "2026-10-27")
        assert first and first.startswith(DERIVED_PREFIX)
        longer = {**body, "messages": body["messages"] + [{"role": "assistant", "content": "done"}]}
        assert derived_conversation_id("u1", CLIENT_TOOL_CLAUDE_CODE, longer, "2026-10-27") == first, "later turns re-send the same first message"
        assert derived_conversation_id("u2", CLIENT_TOOL_CLAUDE_CODE, body, "2026-10-27") != first
        assert derived_conversation_id("u1", CLIENT_TOOL_CLAUDE_CODE, body, "2026-10-28") != first
        assert (
            derived_conversation_id("u1", CLIENT_TOOL_CLAUDE_CODE, {**body, "messages": [{"role": "user", "content": "other prompt"}]}, "2026-10-27")
            != first
        )
        assert derived_conversation_id("u1", CLIENT_TOOL_CLAUDE_CODE, {"messages": []}, "2026-10-27") is None

    def test_identify_prefers_metadata_then_forwarded_session_then_codex_then_derived(self):
        body = b'{"system":"s","messages":[{"role":"user","content":"hello"}]}'
        bedrock_mode = identify(
            user_agent="claude-cli/2.1.296 (external, cli)",
            forwarded_user_agent=None,
            dev_box=False,
            headers={},
            body_bytes=body,
            principal="u1",
            day="2026-10-27",
        )
        assert bedrock_mode.conversation_id.startswith(DERIVED_PREFIX) and bedrock_mode.harness == CLIENT_TOOL_CLAUDE_CODE
        on_box = identify(
            user_agent="node",
            forwarded_user_agent="claude-cli/2.1.296",
            dev_box=True,
            headers={"x-adp-client-session": CLAUDE_METADATA["user_id"]},
            body_bytes=body,
            principal="owner",
            day="2026-10-27",
        )
        assert on_box.conversation_id == SESSION
        spoofed = identify(
            user_agent="claude-cli/2.1.296",
            forwarded_user_agent=None,
            dev_box=False,
            headers={"x-adp-client-session": CLAUDE_METADATA["user_id"]},
            body_bytes=body,
            principal="u1",
            day="2026-10-27",
        )
        assert spoofed.conversation_id.startswith(DERIVED_PREFIX), "the forwarded session is honoured only on the host route"


class TestIdentify:
    def test_claude_code_direct(self):
        body = json.dumps({"metadata": CLAUDE_METADATA, "messages": [{"role": "user", "content": "say pong"}]}).encode()
        result = identify(user_agent="claude-cli/2.1.3 (external, cli)", forwarded_user_agent=None, dev_box=False, headers={}, body_bytes=body)
        assert result == ConversationIdentity(harness=CLIENT_TOOL_CLAUDE_CODE, conversation_id=SESSION, origin=ORIGIN_DIRECT, title="say pong")

    def test_dev_box_prefers_the_forwarded_client_agent_only_on_the_host_route(self):
        headers = {"x-codex-turn-metadata": json.dumps({"thread_id": THREAD})}
        body = b'{"input":"hello there"}'
        on_box = identify(user_agent="node", forwarded_user_agent="codex-tui/0.164.0", dev_box=True, headers=headers, body_bytes=body)
        assert on_box == ConversationIdentity(harness=CLIENT_TOOL_CODEX_CLI, conversation_id=THREAD, origin=ORIGIN_DEV_BOX, title="hello there")
        spoofed = identify(user_agent="claude-cli/2.1.3", forwarded_user_agent="codex-tui/0.164.0", dev_box=False, headers={}, body_bytes=body)
        assert spoofed.harness == CLIENT_TOOL_CLAUDE_CODE and spoofed.origin == ORIGIN_DIRECT, "off the host route the forwarded header is ignored"

    def test_dev_box_without_a_harness_agent_is_kiro_crew(self):
        result = identify(
            user_agent="node",
            forwarded_user_agent=None,
            dev_box=True,
            headers={},
            body_bytes=b'{"messages":[{"role":"user","content":"title please"}]}',
        )
        assert result.harness == CLIENT_TOOL_KIRO_CREW and result.origin == ORIGIN_DEV_BOX
        assert result.conversation_id is not None and result.conversation_id.startswith(DERIVED_PREFIX), (
            "Kiro's own calls carry no id; grouped by their first message"
        )

    def test_malformed_and_oversize_bodies_never_raise(self):
        bad = identify(
            user_agent="claude-cli/2",
            forwarded_user_agent=None,
            dev_box=False,
            headers={"x-codex-turn-metadata": "\x00\xff"},
            body_bytes=b"{not json",
        )
        assert bad == ConversationIdentity(harness=CLIENT_TOOL_CLAUDE_CODE, conversation_id=None, origin=ORIGIN_DIRECT, title=None)
        huge = identify(user_agent="claude-cli/2", forwarded_user_agent=None, dev_box=False, headers={}, body_bytes=b"x" * (4 * 1024 * 1024 + 1))
        assert huge.conversation_id is None and huge.title is None and huge.harness == CLIENT_TOOL_CLAUDE_CODE
        assert identify(user_agent=None, forwarded_user_agent=None, dev_box=False, headers=None, body_bytes=None) == ConversationIdentity(
            origin=ORIGIN_DIRECT
        )


class TestDependency:
    @staticmethod
    def _request(headers: dict[str, str], body: bytes):
        request = MagicMock()
        request.headers = headers
        request.body = AsyncMock(return_value=body)
        return request

    @pytest.mark.asyncio
    async def test_sets_the_contextvar_from_the_request(self):
        context = _token_context()
        body = json.dumps({"metadata": CLAUDE_METADATA, "messages": [{"role": "user", "content": "say pong"}]}).encode()
        result = await set_conversation_identity(self._request({"user-agent": "claude-cli/2.1.3"}, body), context, None)
        assert result.conversation_id == SESSION and _current_conversation.get() == result

    @pytest.mark.asyncio
    async def test_dev_box_context_marks_origin_and_uses_the_forwarded_agent(self):
        context = _token_context()
        context._dev_box_instance_id = "i-0123456789abcdef0"
        result = await set_conversation_identity(
            self._request({"user-agent": "node", "x-adp-client-user-agent": "claude-cli/2.1.3 (external, cli)"}, b'{"messages":[]}'), context, "run-1"
        )
        assert result.origin == ORIGIN_DEV_BOX and result.harness == CLIENT_TOOL_CLAUDE_CODE

    @pytest.mark.asyncio
    async def test_contextvar_is_set_unconditionally_and_body_errors_are_swallowed(self):
        _current_conversation.set(ConversationIdentity(harness="stale", conversation_id="stale", origin="stale", title="stale"))
        request = self._request({}, b"")
        request.body = AsyncMock(side_effect=RuntimeError("stream consumed"))
        result = await set_conversation_identity(request, _token_context(), None)
        assert result == ConversationIdentity(origin=ORIGIN_DIRECT) and _current_conversation.get() == result

    def test_dependency_is_async(self):
        import inspect

        assert inspect.iscoroutinefunction(set_conversation_identity)


class TestReachesTheWriter:
    @staticmethod
    def _session_factory():
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return MagicMock(return_value=session)

    @pytest.mark.asyncio
    async def test_bedrock_write_site(self):
        from src.proxy.service import ProxyService

        _CapturingUsageService.calls = []
        _current_conversation.set(
            ConversationIdentity(harness=CLIENT_TOOL_CLAUDE_CODE, conversation_id=SESSION, origin=ORIGIN_DEV_BOX, title="say pong")
        )
        context = _token_context()
        context._crew_owner_user_id = "owner-human-1"
        service = ProxyService.__new__(ProxyService)
        with (
            patch("src.proxy.service.get_session_factory", self._session_factory),
            patch("src.proxy.service.UsageService", _CapturingUsageService),
            patch("src.proxy.service.reconcile_budget_reservation", AsyncMock()),
        ):
            await service._log_usage(
                context=context,
                model="anthropic.claude-3-5-sonnet",
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.001,
                latency_ms=100,
                status_code=200,
            )
        kwargs = _CapturingUsageService.calls[0]
        assert (kwargs["harness"], kwargs["conversation_id"], kwargs["origin"], kwargs["conversation_title"], kwargs["owner_user_id"]) == (
            CLIENT_TOOL_CLAUDE_CODE,
            SESSION,
            ORIGIN_DEV_BOX,
            "say pong",
            "owner-human-1",
        )

    @pytest.mark.asyncio
    async def test_mantle_write_site_and_not_captured_is_none(self):
        from src.proxy.mantle_service import MantlePassthroughService

        _CapturingUsageService.calls = []
        _current_conversation.set(ConversationIdentity(harness=CLIENT_TOOL_CODEX_CLI, conversation_id=THREAD, origin=ORIGIN_DIRECT, title=None))
        service = MantlePassthroughService(MagicMock(), "https://bedrock-mantle.us-east-1.api.aws")
        with (
            patch("src.proxy.mantle_service.get_session_factory", self._session_factory),
            patch("src.proxy.mantle_service.UsageService", _CapturingUsageService),
            patch("src.proxy.mantle_service.reconcile_budget_reservation", AsyncMock()),
            patch("src.proxy.mantle_service.price_completed_usage", AsyncMock(side_effect=price_fixture_usage)),
        ):
            await service._log_usage(_token_context(), "openai.gpt-6-astra", {"input_tokens": 3, "output_tokens": 2}, 50, 200, "req-1", None)
        kwargs = _CapturingUsageService.calls[0]
        assert (kwargs["harness"], kwargs["conversation_id"], kwargs["origin"], kwargs["conversation_title"], kwargs["owner_user_id"]) == (
            CLIENT_TOOL_CODEX_CLI,
            THREAD,
            ORIGIN_DIRECT,
            None,
            None,
        )
