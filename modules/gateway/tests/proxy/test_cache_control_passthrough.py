"""Prompt-cache breakpoints must survive the /v1/messages translation path.

Issue #4180: ``AnthropicTextContent`` & friends declared no ``cache_control``
field, and pydantic's default ``extra="ignore"`` dropped the key at validation
time — before the translator ever ran. Requests still succeeded, answers still
looked right, and the customer paid full price on every turn.

These tests pin all four assertions that matter:
1. a breakpoint on any of the four content-block types reaches the Bedrock body
2. the system and tools positions carry it too (the highest-value placements)
3. a request WITHOUT markers translates identically to pre-fix behaviour
4. unknown client keys still do not reach the Bedrock body (the fix is a typed
   field, not ``extra: "allow"``)
"""

from src.proxy.format_translator import FormatTranslator
from src.proxy.schemas import (
    AnthropicCacheControl,
    AnthropicMessage,
    AnthropicMessagesRequest,
    AnthropicTextContent,
    AnthropicTool,
    AnthropicToolInput,
    AnthropicToolResultContent,
    AnthropicToolUseContent,
)

EPHEMERAL = {"type": "ephemeral"}


def _request(content, **kwargs) -> AnthropicMessagesRequest:
    """Build a minimal Anthropic request carrying the given user content."""
    return AnthropicMessagesRequest(
        model="claude-opus-4.6",
        max_tokens=1024,
        messages=[AnthropicMessage(role="user", content=content)],
        **kwargs,
    )


class TestCacheControlSurvivesValidation:
    """The models must accept and retain cache_control (they used to drop it)."""

    def test_text_block_retains_marker(self):
        block = AnthropicTextContent(type="text", text="stable prefix", cache_control=EPHEMERAL)
        assert block.cache_control == AnthropicCacheControl(type="ephemeral")

    def test_tool_result_block_retains_marker(self):
        block = AnthropicToolResultContent(
            type="tool_result",
            tool_use_id="toolu_1",
            content="42",
            cache_control=EPHEMERAL,
        )
        assert block.cache_control is not None

    def test_tool_use_block_retains_marker(self):
        """A breakpoint on the trailing tool_use block is an ordinary agent shape."""
        block = AnthropicToolUseContent(
            type="tool_use",
            id="toolu_1",
            name="get_weather",
            input={"city": "Seattle"},
            cache_control=EPHEMERAL,
        )
        assert block.cache_control is not None

    def test_image_block_retains_marker(self):
        request = _request(
            [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "abc"},
                    "cache_control": EPHEMERAL,
                }
            ]
        )
        assert request.messages[0].content[0].cache_control is not None

    def test_marker_absent_stays_none(self):
        assert AnthropicTextContent(type="text", text="hi").cache_control is None

    def test_unknown_cache_control_type_rejected(self):
        """The field is typed: only Anthropic's documented value validates."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AnthropicTextContent(type="text", text="hi", cache_control={"type": "permanent"})


class TestCacheControlReachesBedrockBody:
    """The end-to-end assertion: the marker is in the translated request."""

    def test_text_block_marker_in_bedrock_content(self):
        translator = FormatTranslator()
        result = translator.anthropic_to_bedrock(_request([AnthropicTextContent(type="text", text="stable prefix", cache_control=EPHEMERAL)]))
        assert result.messages[0].content[0]["cache_control"] == EPHEMERAL

    def test_tool_result_block_marker_in_bedrock_content(self):
        translator = FormatTranslator()
        result = translator.anthropic_to_bedrock(
            _request(
                [
                    AnthropicToolResultContent(
                        type="tool_result",
                        tool_use_id="toolu_1",
                        content="42",
                        cache_control=EPHEMERAL,
                    )
                ]
            )
        )
        assert result.messages[0].content[0]["cache_control"] == EPHEMERAL

    def test_tool_use_block_marker_in_bedrock_content(self):
        translator = FormatTranslator()
        result = translator.anthropic_to_bedrock(
            _request(
                [
                    AnthropicToolUseContent(
                        type="tool_use",
                        id="toolu_1",
                        name="get_weather",
                        input={"city": "Seattle"},
                        cache_control=EPHEMERAL,
                    )
                ]
            )
        )
        assert result.messages[0].content[0]["cache_control"] == EPHEMERAL

    def test_image_block_marker_in_bedrock_content(self):
        translator = FormatTranslator()
        result = translator.anthropic_to_bedrock(
            _request(
                [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "abc"},
                        "cache_control": EPHEMERAL,
                    }
                ]
            )
        )
        assert result.messages[0].content[0]["cache_control"] == EPHEMERAL

    def test_marker_position_is_preserved(self):
        """Position matters: the breakpoint marks a boundary, not the request."""
        translator = FormatTranslator()
        result = translator.anthropic_to_bedrock(
            _request(
                [
                    AnthropicTextContent(type="text", text="stable prefix", cache_control=EPHEMERAL),
                    AnthropicTextContent(type="text", text="volatile suffix"),
                ]
            )
        )
        blocks = result.messages[0].content
        assert blocks[0]["cache_control"] == EPHEMERAL
        assert "cache_control" not in blocks[1]


class TestSystemPositionBreakpoint:
    """System-prompt breakpoints already survived; pin it so they keep doing so."""

    def test_system_list_form_marker_preserved(self):
        translator = FormatTranslator()
        request = _request(
            "hello",
            system=[{"type": "text", "text": "long stable system prompt", "cache_control": EPHEMERAL}],
        )
        result = translator.anthropic_to_bedrock(request)
        assert result.system[0]["cache_control"] == EPHEMERAL

    def test_system_string_form_unaffected(self):
        translator = FormatTranslator()
        result = translator.anthropic_to_bedrock(_request("hello", system="plain system prompt"))
        assert result.system == "plain system prompt"


class TestToolsPositionBreakpoint:
    """Tool definitions are a high-value breakpoint position.

    ``tools`` is not forwarded to Bedrock at all yet — that is #790 / EPIC #745,
    deliberately kept OUT of this PR by operator decision. So the assertion this
    PR can honestly make is that the marker survives validation and serialization
    of the tool model; the boundary test below pins the #790 gap explicitly so it
    is impossible to mistake for working end to end.
    """

    def test_tool_definition_retains_marker(self):
        tool = AnthropicTool(
            name="get_weather",
            description="Look up weather",
            input_schema=AnthropicToolInput(type="object", properties={"city": {"type": "string"}}),
            cache_control=EPHEMERAL,
        )
        assert tool.cache_control is not None
        assert tool.model_dump(exclude_none=True)["cache_control"] == EPHEMERAL

    def test_tools_not_yet_forwarded_to_bedrock(self):
        """Boundary pin for #790: when tools ARE forwarded, update this test.

        Without this, a reader would assume the tools-position breakpoint works
        end to end. It does not — the whole `tools` field is dropped in
        translation, independently of cache_control.
        """
        translator = FormatTranslator()
        request = _request(
            "what's the weather?",
            tools=[
                AnthropicTool(
                    name="get_weather",
                    input_schema=AnthropicToolInput(type="object"),
                    cache_control=EPHEMERAL,
                )
            ],
        )
        result = translator.anthropic_to_bedrock(request)
        assert result.tools is None, "tools now forwarded — extend this test to assert the marker survives (#790)"


class TestNoRegressionForNonCachingClients:
    """The overwhelmingly common case must be byte-identical to pre-fix output."""

    def test_no_marker_means_no_cache_control_key(self):
        """exclude_none: an optional field must not leak `cache_control: null`."""
        translator = FormatTranslator()
        result = translator.anthropic_to_bedrock(_request([AnthropicTextContent(type="text", text="hello")]))
        block = result.messages[0].content[0]
        assert "cache_control" not in block
        assert block == {"type": "text", "text": "hello"}

    def test_string_content_unchanged(self):
        translator = FormatTranslator()
        result = translator.anthropic_to_bedrock(_request("hello"))
        assert result.messages[0].content == [{"type": "text", "text": "hello"}]

    def test_tool_result_without_marker_has_no_null_key(self):
        translator = FormatTranslator()
        result = translator.anthropic_to_bedrock(_request([AnthropicToolResultContent(type="tool_result", tool_use_id="toolu_1", content="42")]))
        assert result.messages[0].content[0] == {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": "42",
        }


class TestPassThroughSurfaceNotWidened:
    """The fix adds one typed field — it does NOT blanket-allow client extras.

    ``extra: "allow"`` would let arbitrary client keys reach the Bedrock request
    body, and Bedrock rejects unknown keys inside content blocks with a 400 —
    converting today's silent overcharge into an availability regression.
    """

    def test_unknown_key_does_not_reach_bedrock_body(self):
        translator = FormatTranslator()
        result = translator.anthropic_to_bedrock(_request([{"type": "text", "text": "hello", "some_vendor_extension": "boom"}]))
        assert "some_vendor_extension" not in result.messages[0].content[0]

    def test_ttl_is_not_forwarded(self):
        """Extended-TTL caching needs a beta header this gateway discards.

        Accepting `ttl` while dropping `anthropic-beta` would 400 requests that
        succeed today, so `ttl` is intentionally not modelled.
        """
        translator = FormatTranslator()
        result = translator.anthropic_to_bedrock(_request([{"type": "text", "text": "hello", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]))
        assert result.messages[0].content[0]["cache_control"] == EPHEMERAL
