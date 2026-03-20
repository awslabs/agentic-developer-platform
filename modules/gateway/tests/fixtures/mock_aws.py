"""
Mock AWS service responses for testing.

This module provides mock implementations of AWS STS and Bedrock clients
for use in integration and E2E tests without requiring real AWS credentials.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock


class MockSTSClient:
    """
    Mock AWS STS client for testing authentication flows.

    This mock simulates STS API calls including GetCallerIdentity and AssumeRole
    without making real AWS API calls.
    """

    def __init__(
        self,
        *,
        account_id: str = "123456789012",
        role_arn: str = "arn:aws:sts::123456789012:assumed-role/TestRole/session",
        user_id: str = "AIDACKCEVSQ6C2EXAMPLE",
        session_name: str = "test-session",
        should_fail: bool = False,
        error_code: str | None = None,
        error_message: str | None = None,
    ):
        """
        Initialize mock STS client.

        Args:
            account_id: AWS account ID to return
            role_arn: Role ARN to return in caller identity
            user_id: User ID to return
            session_name: Session name to include in ARN
            should_fail: Whether calls should fail
            error_code: Error code to return on failure
            error_message: Error message to return on failure
        """
        self.account_id = account_id
        self.role_arn = role_arn
        self.user_id = user_id
        self.session_name = session_name
        self.should_fail = should_fail
        self.error_code = error_code or "InvalidIdentityToken"
        self.error_message = error_message or "Token is invalid"
        self.call_count = 0
        self._credentials_used: list[dict[str, str]] = []

    async def get_caller_identity(
        self,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
    ) -> dict[str, str]:
        """
        Mock STS GetCallerIdentity API call.

        Args:
            access_key_id: AWS access key ID (recorded for verification)
            secret_access_key: AWS secret access key (recorded for verification)
            session_token: AWS session token (recorded for verification)

        Returns:
            Mock caller identity response

        Raises:
            Exception: If should_fail is True
        """
        self.call_count += 1
        self._credentials_used.append(
            {
                "access_key_id": access_key_id or "",
                "secret_access_key": secret_access_key or "",
                "session_token": session_token or "",
            }
        )

        if self.should_fail:
            raise Exception(f"{self.error_code}: {self.error_message}")

        return {
            "UserId": self.user_id,
            "Account": self.account_id,
            "Arn": self.role_arn,
        }

    async def assume_role(
        self,
        role_arn: str,
        role_session_name: str,
        duration_seconds: int = 3600,
    ) -> dict[str, Any]:
        """
        Mock STS AssumeRole API call.

        Args:
            role_arn: Role ARN to assume
            role_session_name: Session name for the assumed role
            duration_seconds: Duration of assumed credentials

        Returns:
            Mock assumed role credentials

        Raises:
            Exception: If should_fail is True
        """
        self.call_count += 1

        if self.should_fail:
            raise Exception(f"{self.error_code}: {self.error_message}")

        expiration = datetime.now(UTC).isoformat()
        return {
            "Credentials": {
                "AccessKeyId": "ASIATESTACCESSKEY",
                "SecretAccessKey": "testsecretaccesskey",
                "SessionToken": "testsessiontoken",
                "Expiration": expiration,
            },
            "AssumedRoleUser": {
                "AssumedRoleId": f"AROA{self.user_id}:{role_session_name}",
                "Arn": f"arn:aws:sts::{self.account_id}:assumed-role/{role_arn.split('/')[-1]}/{role_session_name}",
            },
        }

    @property
    def credentials_used(self) -> list[dict[str, str]]:
        """Get list of credentials that were used in calls."""
        return self._credentials_used


class MockBedrockClient:
    """
    Mock AWS Bedrock client for testing proxy flows.

    This mock simulates Bedrock InvokeModel and InvokeModelWithResponseStream
    API calls without making real AWS API calls.
    """

    def __init__(
        self,
        *,
        account_id: str = "123456789012",
        should_fail: bool = False,
        should_throttle: bool = False,
        error_code: str | None = None,
        error_message: str | None = None,
        response_text: str = "This is a mock response from Claude.",
        input_tokens: int = 100,
        output_tokens: int = 150,
    ):
        """
        Initialize mock Bedrock client.

        Args:
            account_id: AWS account ID for this client
            should_fail: Whether calls should fail with an error
            should_throttle: Whether calls should return 429 throttling error
            error_code: Error code to return on failure
            error_message: Error message to return on failure
            response_text: Text to return in mock response
            input_tokens: Input token count to return
            output_tokens: Output token count to return
        """
        self.account_id = account_id
        self.should_fail = should_fail
        self.should_throttle = should_throttle
        self.error_code = error_code or "ValidationException"
        self.error_message = error_message or "Validation error"
        self.response_text = response_text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.call_count = 0
        self._requests: list[dict[str, Any]] = []

    async def invoke_model(
        self,
        model_id: str,
        body: dict[str, Any] | str,
        content_type: str = "application/json",
        accept: str = "application/json",
    ) -> dict[str, Any]:
        """
        Mock Bedrock InvokeModel API call.

        Args:
            model_id: Model identifier
            body: Request body
            content_type: Content type header
            accept: Accept header

        Returns:
            Mock model response

        Raises:
            Exception: If should_fail or should_throttle is True
        """
        self.call_count += 1
        self._requests.append(
            {
                "model_id": model_id,
                "body": body if isinstance(body, dict) else json.loads(body),
                "content_type": content_type,
                "accept": accept,
            }
        )

        if self.should_throttle:
            raise ThrottlingException("ThrottlingException: Rate exceeded")

        if self.should_fail:
            raise Exception(f"{self.error_code}: {self.error_message}")

        return mock_bedrock_invoke_response(
            text=self.response_text,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            model_id=model_id,
        )

    async def invoke_model_with_response_stream(
        self,
        model_id: str,
        body: dict[str, Any] | str,
        content_type: str = "application/json",
        accept: str = "application/json",
    ) -> AsyncIterator[bytes]:
        """
        Mock Bedrock InvokeModelWithResponseStream API call.

        Args:
            model_id: Model identifier
            body: Request body
            content_type: Content type header
            accept: Accept header

        Yields:
            Mock streaming response chunks

        Raises:
            Exception: If should_fail or should_throttle is True
        """
        self.call_count += 1
        self._requests.append(
            {
                "model_id": model_id,
                "body": body if isinstance(body, dict) else json.loads(body),
                "content_type": content_type,
                "accept": accept,
                "streaming": True,
            }
        )

        if self.should_throttle:
            raise ThrottlingException("ThrottlingException: Rate exceeded")

        if self.should_fail:
            raise Exception(f"{self.error_code}: {self.error_message}")

        # Yield mock streaming chunks
        async for chunk in mock_bedrock_streaming_response(
            text=self.response_text,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
        ):
            yield chunk

    @property
    def requests(self) -> list[dict[str, Any]]:
        """Get list of requests that were made."""
        return self._requests

    def reset(self) -> None:
        """Reset call count and request history."""
        self.call_count = 0
        self._requests = []


class ThrottlingError(Exception):
    """Exception raised when Bedrock throttles requests."""

    def __init__(self, message: str = "ThrottlingException: Rate exceeded"):
        self.message = message
        super().__init__(message)


# Alias for backward compatibility with test files using ThrottlingException
ThrottlingException = ThrottlingError


class ValidationError(Exception):
    """Exception raised for Bedrock validation errors."""

    def __init__(self, message: str = "ValidationException: Invalid request"):
        self.message = message
        super().__init__(message)


# Alias for backward compatibility
ValidationException = ValidationError


# Helper functions for creating mock responses


def mock_sts_caller_identity(
    *,
    account_id: str = "123456789012",
    role_name: str = "TestRole",
    session_name: str = "test-session",
    user_id: str = "AIDACKCEVSQ6C2EXAMPLE",
) -> dict[str, str]:
    """
    Create a mock STS GetCallerIdentity response.

    Args:
        account_id: AWS account ID
        role_name: IAM role name
        session_name: Session name (often contains email for SSO users)
        user_id: AWS user ID

    Returns:
        Mock GetCallerIdentity response dict
    """
    return {
        "UserId": user_id,
        "Account": account_id,
        "Arn": f"arn:aws:sts::{account_id}:assumed-role/{role_name}/{session_name}",
    }


def mock_sts_error_response(
    error_code: str = "InvalidIdentityToken",
    error_message: str = "Token is invalid or has expired",
) -> dict[str, Any]:
    """
    Create a mock STS error response.

    Args:
        error_code: AWS error code
        error_message: Error message

    Returns:
        Mock error response dict
    """
    return {
        "Error": {
            "Code": error_code,
            "Message": error_message,
        },
    }


def mock_bedrock_invoke_response(
    *,
    text: str = "This is a mock response from Claude.",
    input_tokens: int = 100,
    output_tokens: int = 150,
    model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0",
    stop_reason: str = "end_turn",
) -> dict[str, Any]:
    """
    Create a mock Bedrock InvokeModel response.

    Args:
        text: Response text content
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        model_id: Model identifier
        stop_reason: Reason for stopping generation

    Returns:
        Mock InvokeModel response dict
    """
    return {
        "id": f"msg_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": model_id,
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


async def mock_bedrock_streaming_response(
    *,
    text: str = "This is a mock streaming response from Claude.",
    input_tokens: int = 100,
    output_tokens: int = 150,
    chunk_size: int = 10,
) -> AsyncIterator[bytes]:
    """
    Create a mock Bedrock streaming response generator.

    Args:
        text: Full response text to stream
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        chunk_size: Number of characters per chunk

    Yields:
        Mock streaming response chunks as bytes
    """
    # Send message_start event
    message_start = {
        "type": "message_start",
        "message": {
            "id": f"msg_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        },
    }
    yield f"event: message_start\ndata: {json.dumps(message_start)}\n\n".encode()

    # Send content_block_start event
    content_block_start = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    yield f"event: content_block_start\ndata: {json.dumps(content_block_start)}\n\n".encode()

    # Stream text in chunks
    for i in range(0, len(text), chunk_size):
        chunk_text = text[i : i + chunk_size]
        content_delta = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": chunk_text},
        }
        yield f"event: content_block_delta\ndata: {json.dumps(content_delta)}\n\n".encode()

    # Send content_block_stop event
    content_block_stop = {"type": "content_block_stop", "index": 0}
    yield f"event: content_block_stop\ndata: {json.dumps(content_block_stop)}\n\n".encode()

    # Send message_delta event with final token counts
    message_delta = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": output_tokens},
    }
    yield f"event: message_delta\ndata: {json.dumps(message_delta)}\n\n".encode()

    # Send message_stop event
    message_stop = {"type": "message_stop"}
    yield f"event: message_stop\ndata: {json.dumps(message_stop)}\n\n".encode()


def mock_bedrock_throttling_response() -> dict[str, Any]:
    """
    Create a mock Bedrock throttling error response.

    Returns:
        Mock throttling error response dict
    """
    return {
        "Error": {
            "Code": "ThrottlingException",
            "Message": "Rate exceeded. Please retry after some time.",
        },
    }


def mock_bedrock_validation_error_response(
    field: str = "messages",
    message: str = "Invalid request format",
) -> dict[str, Any]:
    """
    Create a mock Bedrock validation error response.

    Args:
        field: Field that failed validation
        message: Validation error message

    Returns:
        Mock validation error response dict
    """
    return {
        "Error": {
            "Code": "ValidationException",
            "Message": f"{field}: {message}",
        },
    }


def create_mock_bedrock_pool(
    num_accounts: int = 3,
    unhealthy_indices: list[int] | None = None,
) -> list[MockBedrockClient]:
    """
    Create a pool of mock Bedrock clients for testing failover.

    Args:
        num_accounts: Number of accounts in the pool
        unhealthy_indices: Indices of accounts that should be unhealthy/throttling

    Returns:
        List of MockBedrockClient instances
    """
    unhealthy_indices = unhealthy_indices or []
    clients = []

    for i in range(num_accounts):
        account_id = f"{111111111111 + i}"
        should_throttle = i in unhealthy_indices
        clients.append(
            MockBedrockClient(
                account_id=account_id,
                should_throttle=should_throttle,
            )
        )

    return clients


def create_async_mock(**kwargs: Any) -> AsyncMock:
    """
    Create an AsyncMock with specified return values or side effects.

    Args:
        **kwargs: Keyword arguments passed to AsyncMock

    Returns:
        Configured AsyncMock instance
    """
    return AsyncMock(**kwargs)


def create_mock(**kwargs: Any) -> MagicMock:
    """
    Create a MagicMock with specified return values or side effects.

    Args:
        **kwargs: Keyword arguments passed to MagicMock

    Returns:
        Configured MagicMock instance
    """
    return MagicMock(**kwargs)
