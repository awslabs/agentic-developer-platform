"""Proxy component for BedrockGateway.

This module handles all proxy requests across OpenAI, Anthropic, and Bedrock API formats.

User Stories Implemented:
- US-4.1: OpenAI-Compatible Chat Completions
- US-4.2: Anthropic Messages Format
- US-4.3: Bedrock InvokeModel Pass-Through
- US-9.6: Model Not Allowed
"""

from src.proxy.exceptions import (
    BedrockInvocationError,
    FormatTranslationError,
    InvalidRequestError,
    ProxyError,
    StreamingError,
)
from src.proxy.format_translator import FormatTranslator
from src.proxy.model_resolver import ModelResolver
from src.proxy.schemas import (
    AnthropicMessagesRequest,
    AnthropicMessagesResponse,
    BedrockInvokeRequest,
    BedrockInvokeResponse,
    CountTokensRequest,
    CountTokensResponse,
    ModelsListResponse,
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionResponse,
    ProxyRequest,
    ProxyResponse,
)
from src.proxy.stream_handler import StreamHandler

__all__ = [
    # Main service
    "ProxyService",
    # Components
    "FormatTranslator",
    "ModelResolver",
    "StreamHandler",
    # Router
    "router",
    # Schemas - OpenAI
    "OpenAIChatCompletionRequest",
    "OpenAIChatCompletionResponse",
    # Schemas - Anthropic
    "AnthropicMessagesRequest",
    "AnthropicMessagesResponse",
    # Schemas - Bedrock
    "BedrockInvokeRequest",
    "BedrockInvokeResponse",
    # Schemas - Internal
    "ProxyRequest",
    "ProxyResponse",
    # Schemas - Other
    "ModelsListResponse",
    "CountTokensRequest",
    "CountTokensResponse",
    # Exceptions
    "FormatTranslationError",
    "StreamingError",
    "InvalidRequestError",
    "ProxyError",
    "BedrockInvocationError",
]


def __getattr__(name: str):
    # Policy-only consumers (including the tick Lambda) need ModelResolver
    # without initializing HTTP authentication or budget routes.
    if name == "router":
        from src.proxy.routes import router

        return router
    if name == "ProxyService":
        from src.proxy.service import ProxyService

        return ProxyService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
