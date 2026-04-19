"""Tests for FormatTranslator component."""

from src.proxy.format_translator import FormatTranslator
from src.proxy.schemas import (
    AnthropicMessage,
    AnthropicMessagesRequest,
    AnthropicRole,
    BedrockInvokeRequest,
    BedrockInvokeResponse,
    OpenAIChatCompletionRequest,
    OpenAIMessage,
    OpenAIRole,
)


class TestOpenAIToBedrock:
    """Test OpenAI to Bedrock format conversion."""

    def test_basic_conversion(
        self,
        format_translator: FormatTranslator,
        sample_openai_request: OpenAIChatCompletionRequest,
    ) -> None:
        """Test basic OpenAI to Bedrock conversion."""
        result = format_translator.openai_to_bedrock(sample_openai_request, "anthropic.claude-3-5-sonnet-20241022-v2:0")

        assert isinstance(result, BedrockInvokeRequest)
        assert result.max_tokens == sample_openai_request.max_tokens
        assert result.anthropic_version == "bedrock-2023-05-31"

    def test_system_message_extraction(self, format_translator: FormatTranslator) -> None:
        """Test that system messages are extracted properly."""
        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",
            messages=[
                OpenAIMessage(role=OpenAIRole.SYSTEM, content="You are a helpful assistant."),
                OpenAIMessage(role=OpenAIRole.USER, content="Hello!"),
            ],
            max_tokens=1024,
        )

        result = format_translator.openai_to_bedrock(request, "anthropic.claude-3-5-sonnet-20241022-v2:0")

        assert result.system == "You are a helpful assistant."
        # Messages should not include system message
        assert len(result.messages) == 1
        assert result.messages[0].role == "user"

    def test_multiple_system_messages(self, format_translator: FormatTranslator) -> None:
        """Test handling multiple system messages."""
        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",
            messages=[
                OpenAIMessage(role=OpenAIRole.SYSTEM, content="First instruction."),
                OpenAIMessage(role=OpenAIRole.SYSTEM, content="Second instruction."),
                OpenAIMessage(role=OpenAIRole.USER, content="Hello!"),
            ],
            max_tokens=1024,
        )

        result = format_translator.openai_to_bedrock(request, "anthropic.claude-3-5-sonnet-20241022-v2:0")

        # System messages should be concatenated
        assert "First instruction." in result.system
        assert "Second instruction." in result.system

    def test_conversation_flow(self, format_translator: FormatTranslator) -> None:
        """Test multi-turn conversation conversion."""
        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",
            messages=[
                OpenAIMessage(role=OpenAIRole.USER, content="What is 2+2?"),
                OpenAIMessage(role=OpenAIRole.ASSISTANT, content="2+2 equals 4."),
                OpenAIMessage(role=OpenAIRole.USER, content="And 3+3?"),
            ],
            max_tokens=1024,
        )

        result = format_translator.openai_to_bedrock(request, "anthropic.claude-3-5-sonnet-20241022-v2:0")

        assert len(result.messages) == 3
        assert result.messages[0].role == "user"
        assert result.messages[1].role == "assistant"
        assert result.messages[2].role == "user"

    def test_temperature_mapping(self, format_translator: FormatTranslator) -> None:
        """Test temperature parameter mapping."""
        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",
            messages=[OpenAIMessage(role=OpenAIRole.USER, content="Hello")],
            max_tokens=1024,
            temperature=0.5,
        )

        result = format_translator.openai_to_bedrock(request, "anthropic.claude-3-5-sonnet-20241022-v2:0")
        assert result.temperature == 0.5

    def test_top_p_mapping(self, format_translator: FormatTranslator) -> None:
        """Test top_p parameter mapping."""
        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",
            messages=[OpenAIMessage(role=OpenAIRole.USER, content="Hello")],
            max_tokens=1024,
            top_p=0.9,
        )

        result = format_translator.openai_to_bedrock(request, "anthropic.claude-3-5-sonnet-20241022-v2:0")
        assert result.top_p == 0.9

    def test_stop_sequences_string(self, format_translator: FormatTranslator) -> None:
        """Test stop sequence as string."""
        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",
            messages=[OpenAIMessage(role=OpenAIRole.USER, content="Hello")],
            max_tokens=1024,
            stop="###",
        )

        result = format_translator.openai_to_bedrock(request, "anthropic.claude-3-5-sonnet-20241022-v2:0")
        assert result.stop_sequences == ["###"]

    def test_stop_sequences_list(self, format_translator: FormatTranslator) -> None:
        """Test stop sequences as list."""
        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",
            messages=[OpenAIMessage(role=OpenAIRole.USER, content="Hello")],
            max_tokens=1024,
            stop=["###", "END"],
        )

        result = format_translator.openai_to_bedrock(request, "anthropic.claude-3-5-sonnet-20241022-v2:0")
        assert result.stop_sequences == ["###", "END"]

    def test_default_max_tokens(self, format_translator: FormatTranslator) -> None:
        """Test default max_tokens when not specified."""
        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",
            messages=[OpenAIMessage(role=OpenAIRole.USER, content="Hello")],
        )

        result = format_translator.openai_to_bedrock(request, "anthropic.claude-3-5-sonnet-20241022-v2:0")
        assert result.max_tokens == 4096  # Default value


class TestBedrockToOpenAI:
    """Test Bedrock to OpenAI format conversion."""

    def test_basic_conversion(
        self,
        format_translator: FormatTranslator,
        sample_bedrock_response: BedrockInvokeResponse,
    ) -> None:
        """Test basic Bedrock to OpenAI conversion."""
        result = format_translator.bedrock_to_openai(sample_bedrock_response, "claude-3.5-sonnet")

        assert result.id.startswith("chatcmpl-")
        assert result.object == "chat.completion"
        assert result.model == "claude-3.5-sonnet"
        assert len(result.choices) == 1
        assert result.choices[0].message.role == "assistant"

    def test_content_extraction(
        self,
        format_translator: FormatTranslator,
        sample_bedrock_response: BedrockInvokeResponse,
    ) -> None:
        """Test text content extraction from Bedrock response."""
        result = format_translator.bedrock_to_openai(sample_bedrock_response, "claude-3.5-sonnet")

        assert result.choices[0].message.content is not None
        assert "Hello" in result.choices[0].message.content

    def test_usage_mapping(
        self,
        format_translator: FormatTranslator,
        sample_bedrock_response: BedrockInvokeResponse,
    ) -> None:
        """Test usage statistics mapping."""
        result = format_translator.bedrock_to_openai(sample_bedrock_response, "claude-3.5-sonnet")

        assert result.usage is not None
        assert result.usage.prompt_tokens == sample_bedrock_response.usage["input_tokens"]
        assert result.usage.completion_tokens == sample_bedrock_response.usage["output_tokens"]
        assert result.usage.total_tokens == result.usage.prompt_tokens + result.usage.completion_tokens

    def test_stop_reason_mapping(self, format_translator: FormatTranslator) -> None:
        """Test stop reason to finish_reason mapping."""
        response = BedrockInvokeResponse(
            id="msg_test",
            content=[{"type": "text", "text": "Done"}],
            model="anthropic.claude-3-5-sonnet-20241022-v2:0",
            stop_reason="end_turn",
            usage={"input_tokens": 10, "output_tokens": 5},
        )

        result = format_translator.bedrock_to_openai(response, "claude-3.5-sonnet")
        assert result.choices[0].finish_reason.value == "stop"

    def test_max_tokens_stop_reason(self, format_translator: FormatTranslator) -> None:
        """Test max_tokens stop reason mapping to length."""
        response = BedrockInvokeResponse(
            id="msg_test",
            content=[{"type": "text", "text": "Truncated..."}],
            model="anthropic.claude-3-5-sonnet-20241022-v2:0",
            stop_reason="max_tokens",
            usage={"input_tokens": 10, "output_tokens": 100},
        )

        result = format_translator.bedrock_to_openai(response, "claude-3.5-sonnet")
        assert result.choices[0].finish_reason.value == "length"


class TestAnthropicToBedrock:
    """Test Anthropic to Bedrock format conversion."""

    def test_basic_conversion(
        self,
        format_translator: FormatTranslator,
        sample_anthropic_request: AnthropicMessagesRequest,
    ) -> None:
        """Test basic Anthropic to Bedrock conversion."""
        result = format_translator.anthropic_to_bedrock(sample_anthropic_request)

        assert isinstance(result, BedrockInvokeRequest)
        assert result.max_tokens == sample_anthropic_request.max_tokens

    def test_system_message_preservation(
        self,
        format_translator: FormatTranslator,
        sample_anthropic_request: AnthropicMessagesRequest,
    ) -> None:
        """Test that system message is preserved."""
        result = format_translator.anthropic_to_bedrock(sample_anthropic_request)

        assert result.system == sample_anthropic_request.system

    def test_anthropic_version_override(self, format_translator: FormatTranslator) -> None:
        """Test anthropic_version always uses bedrock version regardless of input.

        The translator always sets anthropic_version to 'bedrock-2023-05-31'
        for compatibility with the Bedrock API, ignoring client-supplied values.
        """
        request = AnthropicMessagesRequest(
            model="claude-3-5-sonnet-20241022",
            messages=[AnthropicMessage(role=AnthropicRole.USER, content="Hello")],
            max_tokens=1024,
        )

        result = format_translator.anthropic_to_bedrock(request, anthropic_version="custom-version-2024")

        assert result.anthropic_version == "bedrock-2023-05-31"

    def test_content_blocks_conversion(
        self,
        format_translator: FormatTranslator,
        sample_anthropic_request_with_content_blocks: AnthropicMessagesRequest,
    ) -> None:
        """Test conversion of content blocks."""
        result = format_translator.anthropic_to_bedrock(sample_anthropic_request_with_content_blocks)

        assert len(result.messages) == 1
        # Content should be list of blocks
        content = result.messages[0].content
        assert isinstance(content, list)

    def test_parameters_mapping(self, format_translator: FormatTranslator) -> None:
        """Test parameter mapping from Anthropic to Bedrock."""
        request = AnthropicMessagesRequest(
            model="claude-3-5-sonnet-20241022",
            messages=[AnthropicMessage(role=AnthropicRole.USER, content="Hello")],
            max_tokens=1024,
            temperature=0.7,
            top_p=0.9,
            top_k=40,
            stop_sequences=["END"],
        )

        result = format_translator.anthropic_to_bedrock(request)

        assert result.temperature == 0.7
        assert result.top_p == 0.9
        assert result.top_k == 40
        assert result.stop_sequences == ["END"]


class TestBedrockToAnthropic:
    """Test Bedrock to Anthropic format conversion."""

    def test_basic_conversion(
        self,
        format_translator: FormatTranslator,
        sample_bedrock_response: BedrockInvokeResponse,
    ) -> None:
        """Test basic Bedrock to Anthropic conversion."""
        result = format_translator.bedrock_to_anthropic(sample_bedrock_response, "claude-3-5-sonnet-20241022")

        assert result.id == sample_bedrock_response.id
        assert result.type == "message"
        assert result.role == "assistant"
        assert result.model == "claude-3-5-sonnet-20241022"

    def test_content_conversion(
        self,
        format_translator: FormatTranslator,
        sample_bedrock_response: BedrockInvokeResponse,
    ) -> None:
        """Test content block conversion."""
        result = format_translator.bedrock_to_anthropic(sample_bedrock_response, "claude-3-5-sonnet-20241022")

        assert len(result.content) > 0
        assert result.content[0].type == "text"
        assert "Hello" in result.content[0].text

    def test_usage_mapping(
        self,
        format_translator: FormatTranslator,
        sample_bedrock_response: BedrockInvokeResponse,
    ) -> None:
        """Test usage statistics mapping."""
        result = format_translator.bedrock_to_anthropic(sample_bedrock_response, "claude-3-5-sonnet-20241022")

        assert result.usage.input_tokens == sample_bedrock_response.usage["input_tokens"]
        assert result.usage.output_tokens == sample_bedrock_response.usage["output_tokens"]

    def test_stop_reason_preservation(
        self,
        format_translator: FormatTranslator,
        sample_bedrock_response: BedrockInvokeResponse,
    ) -> None:
        """Test stop_reason is preserved."""
        result = format_translator.bedrock_to_anthropic(sample_bedrock_response, "claude-3-5-sonnet-20241022")

        assert result.stop_reason == sample_bedrock_response.stop_reason


class TestStreamingChunkConversion:
    """Test streaming chunk conversions."""

    def test_openai_message_start_chunk(self, format_translator: FormatTranslator) -> None:
        """Test conversion of message_start to OpenAI format."""
        chunk = {
            "type": "message_start",
            "message": {"id": "msg_123", "role": "assistant"},
        }

        result = format_translator.convert_bedrock_stream_chunk_to_openai(chunk, "claude-3.5-sonnet", "resp_123")

        assert result is not None
        assert result["id"] == "chatcmpl-resp_123"
        assert result["object"] == "chat.completion.chunk"
        assert result["choices"][0]["delta"]["role"] == "assistant"

    def test_openai_content_delta_chunk(self, format_translator: FormatTranslator) -> None:
        """Test conversion of content_block_delta to OpenAI format."""
        chunk = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello"},
        }

        result = format_translator.convert_bedrock_stream_chunk_to_openai(chunk, "claude-3.5-sonnet", "resp_123")

        assert result is not None
        assert result["choices"][0]["delta"]["content"] == "Hello"

    def test_openai_message_delta_chunk(self, format_translator: FormatTranslator) -> None:
        """Test conversion of message_delta to OpenAI format."""
        chunk = {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
        }

        result = format_translator.convert_bedrock_stream_chunk_to_openai(chunk, "claude-3.5-sonnet", "resp_123")

        assert result is not None
        assert result["choices"][0]["finish_reason"] == "stop"

    def test_openai_message_stop_chunk(self, format_translator: FormatTranslator) -> None:
        """Test conversion of message_stop to OpenAI format."""
        chunk = {"type": "message_stop"}

        result = format_translator.convert_bedrock_stream_chunk_to_openai(chunk, "claude-3.5-sonnet", "resp_123")

        # message_stop should return None to signal end
        assert result is None

    def test_anthropic_pass_through(self, format_translator: FormatTranslator) -> None:
        """Test Anthropic streaming is mostly pass-through."""
        chunk = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello"},
        }

        result = format_translator.convert_bedrock_stream_chunk_to_anthropic(chunk, "claude-3-5-sonnet-20241022", "msg_123")

        assert result == chunk  # Should be pass-through


class TestErrorHandling:
    """Test error handling in format translation."""

    def test_invalid_openai_message_content(self, format_translator: FormatTranslator) -> None:
        """Test handling of edge cases in OpenAI content conversion."""
        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",
            messages=[OpenAIMessage(role=OpenAIRole.USER, content=None)],
            max_tokens=1024,
        )

        # Should handle None content gracefully
        result = format_translator.openai_to_bedrock(request, "anthropic.claude-3-5-sonnet-20241022-v2:0")
        assert result is not None

    def test_empty_bedrock_content(self, format_translator: FormatTranslator) -> None:
        """Test handling of empty content in Bedrock response."""
        response = BedrockInvokeResponse(
            id="msg_test",
            content=[],
            model="anthropic.claude-3-5-sonnet-20241022-v2:0",
            stop_reason="end_turn",
            usage={"input_tokens": 10, "output_tokens": 0},
        )

        result = format_translator.bedrock_to_openai(response, "claude-3.5-sonnet")
        # Empty content returns None per OpenAI convention
        assert result.choices[0].message.content is None
