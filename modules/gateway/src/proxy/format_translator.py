"""Format translator for API format conversions.

Handles bidirectional conversion between:
- OpenAI Chat Completions format
- Anthropic Messages format
- Bedrock InvokeModel format
"""

import logging
import time
import uuid
from typing import Any

from src.proxy.schemas import (
    AnthropicMessagesRequest,
    AnthropicMessagesResponse,
    AnthropicResponseContent,
    AnthropicTextContent,
    AnthropicToolUseContent,
    AnthropicUsage,
    BedrockInvokeRequest,
    BedrockInvokeResponse,
    BedrockMessage,
    FinishReason,
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionResponse,
    OpenAIChoice,
    OpenAIChoiceMessage,
    OpenAIMessage,
    OpenAIRole,
    OpenAIUsage,
)

logger = logging.getLogger(__name__)


class FormatTranslationError(Exception):
    """Error during format translation."""

    def __init__(self, message: str, source_format: str, target_format: str):
        self.message = message
        self.source_format = source_format
        self.target_format = target_format
        super().__init__(f"Translation error ({source_format} -> {target_format}): {message}")


class FormatTranslator:
    """Translates between OpenAI, Anthropic, and Bedrock API formats.

    Implements:
    - US-4.1: OpenAI format conversion
    - US-4.2: Anthropic format conversion
    - US-4.3: Bedrock pass-through (minimal transformation)
    """

    def __init__(self) -> None:
        """Initialize the format translator."""
        self._role_mapping_openai_to_bedrock = {
            OpenAIRole.USER: "user",
            OpenAIRole.ASSISTANT: "assistant",
            OpenAIRole.SYSTEM: "user",  # System messages handled separately
            OpenAIRole.TOOL: "user",  # Tool results treated as user messages
        }

    # =========================================================================
    # OpenAI <-> Bedrock Conversions
    # =========================================================================

    def openai_to_bedrock(
        self,
        request: OpenAIChatCompletionRequest,
        bedrock_model_id: str,
    ) -> BedrockInvokeRequest:
        """Convert OpenAI chat completion request to Bedrock format.

        Args:
            request: OpenAI format request
            bedrock_model_id: The resolved Bedrock model ID

        Returns:
            Bedrock InvokeModel request body
        """
        try:
            # Extract system message if present
            system_content = None
            messages: list[BedrockMessage] = []

            for msg in request.messages:
                if msg.role == OpenAIRole.SYSTEM:
                    # Accumulate system messages
                    if isinstance(msg.content, str):
                        if system_content is None:
                            system_content = msg.content
                        else:
                            system_content += "\n" + msg.content
                else:
                    # Convert message content
                    bedrock_content = self._convert_openai_content_to_bedrock(msg)
                    bedrock_role = self._map_openai_role_to_bedrock(msg.role)

                    # Handle consecutive messages of same role by merging
                    if messages and messages[-1].role == bedrock_role:
                        # Merge with previous message
                        prev_content = messages[-1].content
                        if isinstance(prev_content, str):
                            prev_content = [{"type": "text", "text": prev_content}]
                        if isinstance(bedrock_content, str):
                            bedrock_content = [{"type": "text", "text": bedrock_content}]
                        messages[-1] = BedrockMessage(
                            role=bedrock_role,
                            content=prev_content + bedrock_content,
                        )
                    else:
                        messages.append(BedrockMessage(role=bedrock_role, content=bedrock_content))

            # Build Bedrock request
            bedrock_request = BedrockInvokeRequest(
                anthropic_version="bedrock-2023-05-31",
                max_tokens=request.max_tokens or 4096,
                messages=messages,
            )

            if system_content:
                bedrock_request.system = system_content

            if request.temperature is not None:
                bedrock_request.temperature = request.temperature

            if request.top_p is not None:
                bedrock_request.top_p = request.top_p

            if request.stop:
                if isinstance(request.stop, str):
                    bedrock_request.stop_sequences = [request.stop]
                else:
                    bedrock_request.stop_sequences = request.stop

            return bedrock_request

        except Exception as e:
            logger.error(f"Error converting OpenAI to Bedrock: {e}")
            raise FormatTranslationError(str(e), "openai", "bedrock")

    def bedrock_to_openai(
        self,
        response: BedrockInvokeResponse,
        request_model: str,
    ) -> OpenAIChatCompletionResponse:
        """Convert Bedrock response to OpenAI chat completion format.

        Args:
            response: Bedrock InvokeModel response
            request_model: The model name from the original request (for response)

        Returns:
            OpenAI chat completion response
        """
        try:
            # Extract text content from Bedrock response
            content_text = ""
            tool_calls = None

            for block in response.content:
                if block.get("type") == "text":
                    content_text += block.get("text", "")
                elif block.get("type") == "tool_use":
                    if tool_calls is None:
                        tool_calls = []
                    tool_calls.append(
                        {
                            "id": block.get("id"),
                            "type": "function",
                            "function": {
                                "name": block.get("name"),
                                "arguments": str(block.get("input", {})),
                            },
                        }
                    )

            # Map stop reason
            finish_reason = self._map_bedrock_stop_reason_to_openai(response.stop_reason)

            # Build OpenAI response
            choice = OpenAIChoice(
                index=0,
                message=OpenAIChoiceMessage(
                    role="assistant",
                    content=content_text if content_text else None,
                    tool_calls=tool_calls,
                ),
                finish_reason=finish_reason,
            )

            return OpenAIChatCompletionResponse(
                id=f"chatcmpl-{response.id}",
                object="chat.completion",
                created=int(time.time()),
                model=request_model,
                choices=[choice],
                usage=OpenAIUsage(
                    prompt_tokens=response.usage.get("input_tokens", 0),
                    completion_tokens=response.usage.get("output_tokens", 0),
                    total_tokens=response.usage.get("input_tokens", 0) + response.usage.get("output_tokens", 0),
                ),
            )

        except Exception as e:
            logger.error(f"Error converting Bedrock to OpenAI: {e}")
            raise FormatTranslationError(str(e), "bedrock", "openai")

    # =========================================================================
    # Anthropic <-> Bedrock Conversions
    # =========================================================================

    def anthropic_to_bedrock(
        self,
        request: AnthropicMessagesRequest,
        anthropic_version: str | None = None,
        anthropic_beta: list[str] | None = None,
    ) -> BedrockInvokeRequest:
        """Convert Anthropic messages request to Bedrock format.

        The Anthropic format is very close to Bedrock's native format for Claude models,
        so this is mostly a pass-through with minor adjustments.

        Args:
            request: Anthropic format request
            anthropic_version: Version header to include
            anthropic_beta: Beta features to enable

        Returns:
            Bedrock InvokeModel request body
        """
        try:
            # Convert messages
            messages: list[BedrockMessage] = []
            for msg in request.messages:
                content = self._convert_anthropic_content_to_bedrock(msg.content)
                messages.append(BedrockMessage(role=msg.role.value, content=content))

            # Build Bedrock request
            bedrock_request = BedrockInvokeRequest(
                anthropic_version="bedrock-2023-05-31",  # Always use Bedrock version, ignore client's version
                max_tokens=request.max_tokens,
                messages=messages,
            )

            if request.system:
                bedrock_request.system = request.system

            if request.temperature is not None:
                bedrock_request.temperature = request.temperature

            if request.top_p is not None:
                bedrock_request.top_p = request.top_p

            if request.top_k is not None:
                bedrock_request.top_k = request.top_k

            if request.stop_sequences:
                bedrock_request.stop_sequences = request.stop_sequences

            return bedrock_request

        except Exception as e:
            logger.error(f"Error converting Anthropic to Bedrock: {e}")
            raise FormatTranslationError(str(e), "anthropic", "bedrock")

    def bedrock_to_anthropic(
        self,
        response: BedrockInvokeResponse,
        request_model: str,
    ) -> AnthropicMessagesResponse:
        """Convert Bedrock response to Anthropic messages format.

        Args:
            response: Bedrock InvokeModel response
            request_model: The model name from the original request

        Returns:
            Anthropic messages response
        """
        try:
            # Convert content blocks
            content: list[AnthropicResponseContent | AnthropicToolUseContent] = []
            for block in response.content:
                if block.get("type") == "text":
                    content.append(AnthropicResponseContent(type="text", text=block.get("text", "")))
                elif block.get("type") == "tool_use":
                    content.append(
                        AnthropicToolUseContent(
                            type="tool_use",
                            id=block.get("id", str(uuid.uuid4())),
                            name=block.get("name", ""),
                            input=block.get("input", {}),
                        )
                    )

            return AnthropicMessagesResponse(
                id=response.id,
                type="message",
                role="assistant",
                content=content,
                model=request_model,
                stop_reason=response.stop_reason,
                stop_sequence=response.stop_sequence,
                usage=AnthropicUsage(
                    input_tokens=response.usage.get("input_tokens", 0),
                    output_tokens=response.usage.get("output_tokens", 0),
                ),
            )

        except Exception as e:
            logger.error(f"Error converting Bedrock to Anthropic: {e}")
            raise FormatTranslationError(str(e), "bedrock", "anthropic")

    # =========================================================================
    # Streaming Chunk Conversions
    # =========================================================================

    def convert_bedrock_stream_chunk_to_openai(
        self,
        chunk: dict[str, Any],
        model: str,
        response_id: str,
    ) -> dict[str, Any] | None:
        """Convert a Bedrock streaming chunk to OpenAI format.

        Args:
            chunk: Bedrock streaming chunk
            model: The model name for the response
            response_id: The response ID to use

        Returns:
            OpenAI format streaming chunk, or None if chunk should be skipped
        """
        chunk_type = chunk.get("type", "")

        if chunk_type == "message_start":
            # Initial message chunk
            return {
                "id": f"chatcmpl-{response_id}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
            }

        elif chunk_type == "content_block_delta":
            delta = chunk.get("delta", {})
            if delta.get("type") == "text_delta":
                return {
                    "id": f"chatcmpl-{response_id}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": delta.get("text", "")}, "finish_reason": None}],
                }

        elif chunk_type == "message_delta":
            delta = chunk.get("delta", {})
            stop_reason = delta.get("stop_reason")
            if stop_reason:
                finish_reason = self._map_bedrock_stop_reason_to_openai(stop_reason)
                finish_val = finish_reason.value if finish_reason else None
                return {
                    "id": f"chatcmpl-{response_id}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_val}],
                }

        elif chunk_type == "message_stop":
            # Final chunk - send [DONE]
            return None  # Signal end of stream

        return None  # Skip other chunk types

    def convert_bedrock_stream_chunk_to_anthropic(
        self,
        chunk: dict[str, Any],
        model: str,
        response_id: str,
    ) -> dict[str, Any] | None:
        """Convert a Bedrock streaming chunk to Anthropic format.

        Since Bedrock uses Anthropic's native streaming format for Claude models,
        this is mostly a pass-through.

        Args:
            chunk: Bedrock streaming chunk
            model: The model name for the response
            response_id: The response ID to use

        Returns:
            Anthropic format streaming event, or None if chunk should be skipped
        """
        chunk_type = chunk.get("type", "")

        # Anthropic streaming format is very similar to Bedrock's
        # Just pass through with minor adjustments
        if chunk_type == "message_start":
            message = chunk.get("message", {})
            message["model"] = model
            message["id"] = response_id
            return {"type": "message_start", "message": message}

        elif chunk_type in ["content_block_start", "content_block_delta", "content_block_stop"]:
            return chunk

        elif chunk_type == "message_delta":
            return chunk

        elif chunk_type == "message_stop":
            return {"type": "message_stop"}

        return None

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _convert_openai_content_to_bedrock(self, message: OpenAIMessage) -> list[dict[str, Any]]:
        """Convert OpenAI message content to Bedrock format.

        Args:
            message: OpenAI message

        Returns:
            Bedrock content list
        """
        if message.content is None:
            return [{"type": "text", "text": ""}]

        if isinstance(message.content, str):
            # Handle tool messages
            if message.role == OpenAIRole.TOOL and message.tool_call_id:
                return [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.content,
                    }
                ]
            return [{"type": "text", "text": message.content}]

        # Content is a list (multimodal)
        bedrock_content: list[dict[str, Any]] = []
        for item in message.content:
            if item.get("type") == "text":
                bedrock_content.append({"type": "text", "text": item.get("text", "")})
            elif item.get("type") == "image_url":
                # Convert image URL to Bedrock format
                image_url = item.get("image_url", {})
                url = image_url.get("url", "")
                if url.startswith("data:"):
                    # Base64 encoded image
                    parts = url.split(",", 1)
                    if len(parts) == 2:
                        media_type = parts[0].split(";")[0].replace("data:", "")
                        bedrock_content.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": parts[1],
                                },
                            }
                        )

        return bedrock_content if bedrock_content else [{"type": "text", "text": ""}]

    def _convert_anthropic_content_to_bedrock(
        self,
        content: str | list[AnthropicTextContent | Any],
    ) -> list[dict[str, Any]]:
        """Convert Anthropic message content to Bedrock format.

        Args:
            content: Anthropic content (string or list of content blocks)

        Returns:
            Bedrock content list
        """
        if isinstance(content, str):
            return [{"type": "text", "text": content}]

        bedrock_content: list[dict[str, Any]] = []
        for item in content:
            if hasattr(item, "model_dump"):
                bedrock_content.append(item.model_dump())
            elif isinstance(item, dict):
                bedrock_content.append(item)
            else:
                bedrock_content.append({"type": "text", "text": str(item)})

        return bedrock_content

    def _map_openai_role_to_bedrock(self, role: OpenAIRole) -> str:
        """Map OpenAI role to Bedrock role.

        Args:
            role: OpenAI role

        Returns:
            Bedrock role string
        """
        return self._role_mapping_openai_to_bedrock.get(role, "user")

    def _map_bedrock_stop_reason_to_openai(self, stop_reason: str | None) -> FinishReason | None:
        """Map Bedrock stop reason to OpenAI finish reason.

        Args:
            stop_reason: Bedrock stop reason

        Returns:
            OpenAI finish reason
        """
        if stop_reason is None:
            return None

        mapping = {
            "end_turn": FinishReason.STOP,
            "stop_sequence": FinishReason.STOP,
            "max_tokens": FinishReason.LENGTH,
            "tool_use": FinishReason.TOOL_CALLS,
        }

        return mapping.get(stop_reason, FinishReason.STOP)
