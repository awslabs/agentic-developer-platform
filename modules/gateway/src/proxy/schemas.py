"""Pydantic schemas for the Proxy component.

Defines request/response models for OpenAI, Anthropic, and Bedrock API formats.
"""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

# ============================================================================
# Common Enums and Types
# ============================================================================


class OpenAIRole(str, Enum):
    """OpenAI message role types."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class AnthropicRole(str, Enum):
    """Anthropic message role types."""

    USER = "user"
    ASSISTANT = "assistant"


class FinishReason(str, Enum):
    """Reason the model stopped generating."""

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"


# ============================================================================
# OpenAI Format Schemas
# ============================================================================


class OpenAIMessage(BaseModel):
    """OpenAI chat message."""

    role: OpenAIRole
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class OpenAIFunctionDefinition(BaseModel):
    """Function definition for tools."""

    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class OpenAITool(BaseModel):
    """OpenAI tool definition."""

    type: Literal["function"] = "function"
    function: OpenAIFunctionDefinition


class OpenAIChatCompletionRequest(BaseModel):
    """OpenAI chat completion request."""

    model: str
    messages: list[OpenAIMessage]
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1)
    stop: str | list[str] | None = None
    tools: list[OpenAITool] | None = None
    tool_choice: str | dict[str, Any] | None = None
    user: str | None = None


class OpenAIUsage(BaseModel):
    """Token usage statistics."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class OpenAIChoiceMessage(BaseModel):
    """Message in completion choice."""

    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class OpenAIChoice(BaseModel):
    """Completion choice."""

    index: int
    message: OpenAIChoiceMessage
    finish_reason: FinishReason | None = None


class OpenAIChatCompletionResponse(BaseModel):
    """OpenAI chat completion response."""

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[OpenAIChoice]
    usage: OpenAIUsage | None = None


class OpenAIDelta(BaseModel):
    """Delta for streaming response."""

    role: str | None = None
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class OpenAIStreamChoice(BaseModel):
    """Streaming choice."""

    index: int
    delta: OpenAIDelta
    finish_reason: FinishReason | None = None


class OpenAIChatCompletionChunk(BaseModel):
    """OpenAI streaming chunk."""

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[OpenAIStreamChoice]


# ============================================================================
# Anthropic Format Schemas
# ============================================================================


class AnthropicTextContent(BaseModel):
    """Anthropic text content block."""

    type: Literal["text"] = "text"
    text: str


class AnthropicImageSource(BaseModel):
    """Anthropic image source."""

    type: Literal["base64"] = "base64"
    media_type: str
    data: str


class AnthropicImageContent(BaseModel):
    """Anthropic image content block."""

    type: Literal["image"] = "image"
    source: AnthropicImageSource


class AnthropicToolUseContent(BaseModel):
    """Anthropic tool use content block."""

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class AnthropicToolResultContent(BaseModel):
    """Anthropic tool result content block."""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | list[dict[str, Any]]


AnthropicContent = AnthropicTextContent | AnthropicImageContent | AnthropicToolUseContent | AnthropicToolResultContent


# Type alias for Anthropic content types
AnthropicContentType = AnthropicTextContent | AnthropicImageContent | AnthropicToolUseContent | AnthropicToolResultContent


class AnthropicMessage(BaseModel):
    """Anthropic message."""

    role: AnthropicRole
    content: str | list[AnthropicContentType]


class AnthropicToolInput(BaseModel):
    """Anthropic tool input schema."""

    type: Literal["object"] = "object"
    properties: dict[str, Any] | None = None
    required: list[str] | None = None


class AnthropicTool(BaseModel):
    """Anthropic tool definition."""

    name: str
    description: str | None = None
    input_schema: AnthropicToolInput


class AnthropicMessagesRequest(BaseModel):
    """Anthropic messages API request."""

    model: str
    messages: list[AnthropicMessage]
    max_tokens: int
    system: str | list[dict[str, Any]] | None = None
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=1)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = None
    stop_sequences: list[str] | None = None
    tools: list[AnthropicTool] | None = None
    tool_choice: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class AnthropicResponseContent(BaseModel):
    """Anthropic response content block."""

    type: Literal["text"] = "text"
    text: str


class AnthropicUsage(BaseModel):
    """Anthropic usage statistics.

    Includes prompt-cache token fields per AWS Bedrock prompt-caching docs:
    - cache_read_input_tokens: tokens served from cache (charged ~0.1x input rate)
    - cache_creation_input_tokens: tokens written to cache (charged ~1.25x input rate)

    Issue #1486: Previously only input_tokens/output_tokens were captured,
    causing ~10x cost undercount when prompt caching was active.
    """

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    model_config = {"extra": "allow"}


class AnthropicMessagesResponse(BaseModel):
    """Anthropic messages API response."""

    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: list[AnthropicResponseContent | AnthropicToolUseContent]
    model: str
    stop_reason: str | None = None
    stop_sequence: str | None = None
    usage: AnthropicUsage


# ============================================================================
# Anthropic Streaming Schemas
# ============================================================================


class AnthropicMessageStart(BaseModel):
    """Anthropic message_start event."""

    type: Literal["message_start"] = "message_start"
    message: dict[str, Any]


class AnthropicContentBlockStart(BaseModel):
    """Anthropic content_block_start event."""

    type: Literal["content_block_start"] = "content_block_start"
    index: int
    content_block: dict[str, Any]


class AnthropicContentBlockDelta(BaseModel):
    """Anthropic content_block_delta event."""

    type: Literal["content_block_delta"] = "content_block_delta"
    index: int
    delta: dict[str, Any]


class AnthropicContentBlockStop(BaseModel):
    """Anthropic content_block_stop event."""

    type: Literal["content_block_stop"] = "content_block_stop"
    index: int


class AnthropicMessageDelta(BaseModel):
    """Anthropic message_delta event."""

    type: Literal["message_delta"] = "message_delta"
    delta: dict[str, Any]
    usage: dict[str, Any] | None = None


class AnthropicMessageStop(BaseModel):
    """Anthropic message_stop event."""

    type: Literal["message_stop"] = "message_stop"


# ============================================================================
# Bedrock Format Schemas
# ============================================================================


class BedrockMessage(BaseModel):
    """Bedrock message format (Claude model)."""

    role: str
    content: list[dict[str, Any]] | str


class BedrockInvokeRequest(BaseModel):
    """Bedrock InvokeModel request body for Claude models."""

    anthropic_version: str = "bedrock-2023-05-31"
    max_tokens: int
    messages: list[BedrockMessage]
    system: str | list[dict[str, Any]] | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop_sequences: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | None = None

    model_config = {"extra": "allow"}


class BedrockInvokeResponse(BaseModel):
    """Bedrock InvokeModel response body for Claude models."""

    id: str
    type: str = "message"
    role: str = "assistant"
    content: list[dict[str, Any]]
    model: str
    stop_reason: str | None = None
    stop_sequence: str | None = None
    usage: dict[str, Any]


class BedrockStreamChunk(BaseModel):
    """Bedrock streaming response chunk."""

    type: str
    message: dict[str, Any] | None = None
    index: int | None = None
    content_block: dict[str, Any] | None = None
    delta: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None


# ============================================================================
# Internal Proxy Schemas
# ============================================================================


class ProxyRequest(BaseModel):
    """Internal proxy request representation."""

    api_format: Literal["openai", "anthropic", "bedrock"]
    model: str
    bedrock_model_id: str
    messages: list[dict[str, Any]]
    system: str | None = None
    max_tokens: int
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop_sequences: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | None = None
    # Anthropic-specific headers
    anthropic_version: str | None = None
    anthropic_beta: list[str] | None = None


class ProxyResponse(BaseModel):
    """Internal proxy response representation."""

    id: str
    model: str
    content: list[dict[str, Any]]
    stop_reason: str | None = None
    stop_sequence: str | None = None
    input_tokens: int
    output_tokens: int
    latency_ms: float | None = None


class ModelInfo(BaseModel):
    """Model information."""

    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str


class ModelsListResponse(BaseModel):
    """Response for GET /v1/models."""

    object: Literal["list"] = "list"
    data: list[ModelInfo]


# ============================================================================
# Error Schemas
# ============================================================================


class ErrorDetail(BaseModel):
    """Error detail for API errors."""

    error: str
    message: str
    details: dict[str, Any] | None = None


class ModelNotAllowedErrorResponse(BaseModel):
    """Response for model not allowed error (US-9.6)."""

    error: Literal["model_not_allowed"] = "model_not_allowed"
    model: str
    allowed_models: list[str]
    message: str = "Your team does not have access to this model."


# ============================================================================
# Token Count Schemas
# ============================================================================


class CountTokensRequest(BaseModel):
    """Request for token counting endpoint."""

    model: str
    messages: list[AnthropicMessage]
    system: str | list[dict[str, Any]] | None = None


class CountTokensResponse(BaseModel):
    """Response for token counting endpoint."""

    input_tokens: int
