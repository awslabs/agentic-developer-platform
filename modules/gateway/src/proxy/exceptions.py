"""Custom exceptions for the Proxy component.

Note: Some exceptions are re-exported from src.shared.exceptions for convenience.
"""

from src.shared.exceptions import (
    BedrockGatewayError,
    ModelNotAllowedError,
    NoHealthyAccountsError,
)

# Re-export shared exceptions
__all__ = [
    "BedrockGatewayError",
    "ModelNotAllowedError",
    "NoHealthyAccountsError",
    "FormatTranslationError",
    "StreamingError",
    "InvalidRequestError",
    "ProxyError",
    "BedrockInvocationError",
]


class FormatTranslationError(BedrockGatewayError):
    """Error during format translation between API formats."""

    def __init__(
        self,
        message: str,
        source_format: str,
        target_format: str,
    ):
        super().__init__(
            error="format_translation_error",
            message=f"Failed to translate from {source_format} to {target_format}: {message}",
            status_code=400,
            details={
                "source_format": source_format,
                "target_format": target_format,
            },
        )


class StreamingError(BedrockGatewayError):
    """Error during SSE streaming."""

    def __init__(
        self,
        message: str,
        chunk_index: int | None = None,
    ):
        super().__init__(
            error="streaming_error",
            message=message,
            status_code=500,
            details={"chunk_index": chunk_index} if chunk_index is not None else None,
        )


class InvalidRequestError(BedrockGatewayError):
    """Error for invalid or malformed requests."""

    def __init__(
        self,
        message: str,
        field: str | None = None,
    ):
        super().__init__(
            error="invalid_request",
            message=message,
            status_code=400,
            details={"field": field} if field else None,
        )


class ProxyError(BedrockGatewayError):
    """Generic proxy error."""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        details: dict | None = None,
    ):
        super().__init__(
            error="proxy_error",
            message=message,
            status_code=status_code,
            details=details,
        )


class BedrockInvocationError(BedrockGatewayError):
    """Error from Bedrock InvokeModel call."""

    def __init__(
        self,
        message: str,
        bedrock_error_code: str | None = None,
        bedrock_request_id: str | None = None,
    ):
        super().__init__(
            error="bedrock_invocation_error",
            message=message,
            status_code=502,
            details={
                "bedrock_error_code": bedrock_error_code,
                "bedrock_request_id": bedrock_request_id,
            },
        )
