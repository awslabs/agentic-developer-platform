"""FastAPI routes for the Proxy component.

Implements:
- US-4.1: OpenAI-compatible chat completions (/v1/chat/completions, /v1/models)
- US-4.2: Anthropic Messages format (/v1/messages, /v1/messages/count_tokens)
- US-4.3: Bedrock InvokeModel pass-through (/bedrock/invoke, /bedrock/invoke-with-response-stream)
- US-9.6: Model access control errors

Issue #119: Unified Cognito JWT Auth
- All proxy routes now use Cognito JWT validation
- Auth context extracted from JWT claims (org_id, team_id, etc.)
- Supports both human users (PKCE) and agents (client_credentials)

Issue #143: Async Chat Logging with PII Scrubbing
- All proxy requests are logged asynchronously to S3
- Sensitive data is scrubbed (headers, regex patterns, Comprehend PII)
- Fire-and-forget pattern for zero latency impact

Issue #144: Added timing instrumentation for auth and model_resolve segments
"""

import asyncio
import fnmatch
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from src.auth.middleware import validate_cognito_jwt
from src.chat_logging.service import ChatLoggingService, create_streaming_logging_wrapper
from src.proxy.bedrock_streaming_response import BedrockStreamingResponse
from src.proxy.client_tool import normalize_client_tool
from src.proxy.eventstream_codec import EVENTSTREAM_CONTENT_TYPE, EVENTSTREAM_KEEPALIVE, sse_to_eventstream
from src.proxy.mantle_service import MantlePassthroughService, MantleUpstreamError
from src.proxy.model_resolver import ModelResolver, production_model_resolver
from src.proxy.pricing_capture import PricingCapture
from src.proxy.schemas import (
    AnthropicMessagesRequest,
    AnthropicMessagesResponse,
    CountTokensRequest,
    CountTokensResponse,
    ModelsListResponse,
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionResponse,
)
from src.proxy.service import ProxyService, _current_agent_run_id, _current_client_tool
from src.proxy.stream_handler import merge_with_keepalive
from src.shared.config import get_settings
from src.shared.exceptions import BedrockGatewayError, ModelNotAllowedError
from src.shared.schemas.auth import TokenContext
from src.shared.timing import get_timings

logger = logging.getLogger(__name__)
_failure_finalizers: set[asyncio.Task] = set()

router = APIRouter(tags=["proxy"])

# Standard headers for every streaming (SSE) response. "no-cache" / no proxy
# buffering so chunks reach the client as produced.
_SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def sse_streaming_response(
    stream: AsyncIterator[bytes],
    *,
    extra_headers: dict[str, str] | None = None,
) -> StreamingResponse:
    """Wrap a ``text/event-stream`` byte stream with a silence keep-alive.

    The human CLI reaches the gateway through CloudFront, whose ~60s origin idle
    timeout severs a connection that goes quiet for that long — which a model
    routinely does mid-response while thinking. ``merge_with_keepalive`` injects
    an SSE-comment keep-alive during silence so that timer never fires; the
    comment is ignored by SSE parsers, so the payload is unchanged. See
    ``merge_with_keepalive`` for the underlying mechanism.
    """
    headers = dict(_SSE_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    return StreamingResponse(
        merge_with_keepalive(stream),
        media_type="text/event-stream",
        headers=headers,
    )


# ============================================================================
# Dependency Injection
# ============================================================================


# Placeholder for actual service dependency injection
# In production, this would be provided by the app's dependency injection system
_proxy_service: ProxyService | None = None
_model_resolver: ModelResolver | None = None
_chat_logging_service: ChatLoggingService | None = None
# Issue #2709: bedrock-mantle passthrough for OpenAI Responses-API traffic.
_mantle_service: MantlePassthroughService | None = None


def get_proxy_service() -> ProxyService:
    """Get the proxy service instance.

    This is a placeholder - in production, dependency injection would be configured
    at the app level.
    """
    if _proxy_service is None:
        raise HTTPException(status_code=503, detail="Proxy service not initialized")
    return _proxy_service


def get_model_resolver() -> ModelResolver:
    """Get the explicit test override or the deployed policy-backed resolver."""
    if _model_resolver is None:
        return production_model_resolver()
    return _model_resolver


def set_proxy_service(service: ProxyService) -> None:
    """Set the proxy service instance (for dependency injection)."""
    global _proxy_service
    _proxy_service = service


def set_model_resolver(resolver: ModelResolver) -> None:
    """Set the model resolver instance (for dependency injection)."""
    global _model_resolver
    _model_resolver = resolver


def get_chat_logging_service() -> ChatLoggingService:
    """Get the chat logging service instance."""
    global _chat_logging_service
    if _chat_logging_service is None:
        _chat_logging_service = ChatLoggingService()
    return _chat_logging_service


def set_chat_logging_service(service: ChatLoggingService) -> None:
    """Set the chat logging service instance (for dependency injection)."""
    global _chat_logging_service
    _chat_logging_service = service


def get_mantle_service() -> MantlePassthroughService:
    """Get the bedrock-mantle passthrough service instance (Issue #2709).

    Returns 503 when unconfigured/disabled — the route is inert until the
    operator sets BG_MANTLE_ENABLED and the API-key secret.
    """
    if _mantle_service is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "mantle_disabled", "message": "OpenAI (bedrock-mantle) passthrough is not enabled"},
        )
    return _mantle_service


def set_mantle_service(service: MantlePassthroughService | None) -> None:
    """Set the mantle passthrough service instance (for dependency injection)."""
    global _mantle_service
    _mantle_service = service


async def set_agent_run_id_from_header(request: Request) -> str | None:
    """Extract X-Agent-RunId header and set the contextvar for _log_usage.

    Async so the contextvar is set in the request task, not a worker thread.
    Issue #1616: The agent-worker sigv4-proxy injects this header on every
    Bedrock call so we can attribute usage_logs rows to the originating run.
    Returns the value (or None) for transparency; the contextvar side-effect
    is what matters.
    """
    agent_run_id = request.headers.get("x-agent-runid")
    if agent_run_id:
        _current_agent_run_id.set(agent_run_id)
    else:
        _current_agent_run_id.set(None)
    return agent_run_id


async def set_client_tool_from_header(request: Request) -> str | None:
    """Derive the normalised client tool from User-Agent and set the contextvar.

    Issue #4398 (FR-6.1): capture WHICH tool made each proxied request — Claude
    Code, Codex CLI, Cursor, the web chat — onto the cost record. Mirrors the
    ``agent_run_id`` precedent above (capture in the route, read at the
    ``_log_usage`` write site) with two deliberate differences:

    1. **This is `async def`, and that is load-bearing.** A contextvar set inside
       a SYNC dependency is lost before the endpoint body runs: Starlette executes
       sync dependencies in a threadpool, which copies the context, so the
       mutation lands on a throwaway copy. That is exactly why ``agent_run_id``
       — set by the sync ``set_agent_run_id_from_header`` — read as NULL on 100%
       of rows until #1755 worked around it by threading the value explicitly
       through eight call sites. Declaring this dependency ``async`` makes the
       contextvar reliable at the write site with no new parameter on any
       signature, so the workaround is unnecessary rather than repeated. Do not
       "simplify" this to ``def``: that silently reintroduces the #1755 bug, and
       the row just goes quietly NULL with no error anywhere.

    2. **The value is normalised here, never raw.** Only a member of the closed
       set in ``client_tool.py`` (or ``None``) is ever persisted, so a future
       breakdown cannot fragment across version/platform spellings.

    The contextvar is set **unconditionally**, including to ``None``. ContextVars
    can carry a previous request's value into a task that did not set one, so a
    conditional ``if tool:`` would let a UA-less request inherit the last request's
    tool and mis-attribute it. Always writing makes each request self-contained.

    Never raises: ``normalize_client_tool`` is total, so an absent, malformed or
    novel User-Agent yields ``None`` ("not captured") rather than failing a
    request on the hot path. A reporting field must not be an availability risk.
    """
    client_tool = normalize_client_tool(request.headers.get("user-agent"))
    _current_client_tool.set(client_tool)
    return client_tool


async def get_token_context(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
) -> TokenContext:
    """Extract token context from request.

    Issue #119: Updated to use Cognito JWT validation.
    Issue #131: Sets token_context in request.state for enforcement middleware.
    Issue #144: Instrumented with timing for 'auth' segment.
    Issue #3985: the #240 X-Auth-Source / X-Agent-* branch was removed — it built
    a TokenContext from headers alone, so any caller able to reach this service
    could assert an arbitrary org_id. IAM-authenticated agents are handled
    upstream by TokenContextMiddleware (X-Caller-Identity -> agent registry) and
    arrive here via request.state.token_context.

    Authenticates via Cognito JWT, accepting the token from either:
    - Authorization: Bearer <token>  (standard)
    - X-Api-Key: <token>  (Claude Code / Anthropic SDK)

    Args:
        request: FastAPI request object
        authorization: Authorization header (Bearer <token>)
        x_api_key: X-Api-Key header (Anthropic SDK sends token here)

    Returns:
        TokenContext: User/service account context from JWT claims, or the
            context already set on request.state by TokenContextMiddleware

    Raises:
        HTTPException: If token is missing, invalid, or expired
    """
    # Try to get from request state first (set by middleware)
    if hasattr(request.state, "token_context"):
        return request.state.token_context

    # Issue #144: Time the auth segment
    auth_start = time.monotonic()
    try:
        # Validate Cognito JWT token from Authorization header
        if authorization:
            token_context = await validate_cognito_jwt(authorization)
            # Issue #131: Store in request.state for enforcement middleware
            request.state.token_context = token_context
            return token_context

        # Fallback: accept token from X-Api-Key header (Claude Code sends it here)
        if x_api_key:
            token_context = await validate_cognito_jwt(f"Bearer {x_api_key}")
            request.state.token_context = token_context
            return token_context

        # No token found - raise 401
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_token", "message": "Authorization header required"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    finally:
        # Issue #144: Record auth timing
        elapsed_ms = (time.monotonic() - auth_start) * 1000
        try:
            timings = get_timings(request)
            timings.record("auth", elapsed_ms)
        except Exception:
            pass  # Don't let timing errors break auth


# ============================================================================
# Error Handling
# ============================================================================


def handle_proxy_error(error: Exception) -> HTTPException:
    """Convert proxy errors to HTTP exceptions."""
    if isinstance(error, ModelNotAllowedError):
        return HTTPException(
            status_code=403,
            detail={
                "error": error.error,
                "model": error.details.get("model") if error.details else None,
                "allowed_models": error.details.get("allowed_models") if error.details else [],
                "message": error.message,
            },
        )
    elif isinstance(error, BedrockGatewayError):
        return HTTPException(
            status_code=error.status_code,
            detail={
                "error": error.error,
                "message": error.message,
                "details": error.details,
            },
        )
    else:
        logger.error(f"Unexpected error: {type(error).__name__}: {error}")
        return HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": str(error) or type(error).__name__},
        )


# ============================================================================
# OpenAI-Compatible Routes (US-4.1)
# ============================================================================


def _pricing_log_args(request: Request, context: TokenContext, capture: PricingCapture, body: dict) -> dict:
    """Add settlement to compatibility routes only for newly priced Claude calls."""
    return {
        "request_id": capture.request_id,
        "timestamp": datetime.now(UTC),
        "org_id": context.attributed_org_id,
        "user_id": context.user_id,
        "team_id": context.team_id,
        "root_human_id": context.attributed_user_id,
        "account_type": "service" if context.account_type == "service" else "human",
        "model": capture.original_model,
        "api_format": "bedrock",
        "request_body": body,
        "headers": dict(request.headers),
        "pricing_capture": capture,
        "only_priced": True,
    }


async def _invoke_with_failure_logging(invoke, request: Request, context: TokenContext, capture: PricingCapture, body: dict):
    """Preserve measured Claude spend if translation fails after invocation."""
    started = time.monotonic()
    try:
        return await invoke
    except (Exception, asyncio.CancelledError):
        if not capture.is_claude:
            raise

        async def finalize():
            # Normal returns retain their existing chat log. On an error or
            # cancellation, await the service's measured pricing decision first.
            if capture.finalization_task is not None:
                try:
                    await asyncio.shield(capture.finalization_task)
                except Exception:
                    logger.exception("Failed to finish Claude usage after invocation error")
            try:
                get_chat_logging_service().log_chat_async(
                    **_pricing_log_args(request, context, capture, body),
                    response_body=capture.response_body,
                    latency_ms=(time.monotonic() - started) * 1000,
                )
            except Exception:
                logger.exception("Failed to schedule Claude settlement after invocation error")

        finalizer = asyncio.create_task(finalize(), name=f"chat_failure_finalize_{capture.request_id}")
        _failure_finalizers.add(finalizer)
        finalizer.add_done_callback(_failure_finalizers.discard)
        await asyncio.shield(finalizer)
        raise


@router.post("/v1/chat/completions", response_model=OpenAIChatCompletionResponse)
async def create_chat_completion(
    raw_request: Request,
    request: OpenAIChatCompletionRequest,
    context: Annotated[TokenContext, Depends(get_token_context)],
    proxy_service: Annotated[ProxyService, Depends(get_proxy_service)],
    _agent_run_id: Annotated[str | None, Depends(set_agent_run_id_from_header)],
    # Issue #4398: capture the client tool onto usage_logs (FR-6.1). Attached to
    # exactly the routes that already carry the agent_run_id dependency — i.e. the
    # routes that write a usage_logs row.
    _client_tool: Annotated[str | None, Depends(set_client_tool_from_header)],
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Create a chat completion (OpenAI-compatible).

    Implements US-4.1: OpenAI-Compatible Chat Completions
    """
    try:
        capture = PricingCapture(getattr(raw_request.state, "request_id", None) or str(id(raw_request)), request.model)
        log_args = _pricing_log_args(raw_request, context, capture, request.model_dump(mode="json"))
        started = time.monotonic()
        if request.stream:
            # Return streaming response
            stream = await proxy_service.chat_completions(request, context, pricing_capture=capture)
            wrapped = create_streaming_logging_wrapper(
                stream=stream,
                chat_logger=get_chat_logging_service(),
                start_time=started,
                **log_args,
            )
            return sse_streaming_response(wrapped)
        else:
            # Return regular response
            response = await _invoke_with_failure_logging(
                proxy_service.chat_completions(request, context, pricing_capture=capture),
                raw_request,
                context,
                capture,
                request.model_dump(mode="json"),
            )
            get_chat_logging_service().log_chat_async(
                **log_args,
                response_body=capture.response_body or response.model_dump(mode="json"),
                latency_ms=(time.monotonic() - started) * 1000,
            )
            return Response(
                content=response.model_dump_json(),
                media_type="application/json",
            )

    except BedrockGatewayError as e:
        raise handle_proxy_error(e)
    except Exception as e:
        raise handle_proxy_error(e)


@router.get("/v1/models", response_model=ModelsListResponse)
async def list_models(
    context: Annotated[TokenContext, Depends(get_token_context)],
    model_resolver: Annotated[ModelResolver, Depends(get_model_resolver)],
    authorization: Annotated[str | None, Header()] = None,
) -> ModelsListResponse:
    """List available models (OpenAI-compatible).

    Returns models filtered by the caller's org/team permissions.
    """
    try:
        models = model_resolver.get_available_models(context)
        return ModelsListResponse(object="list", data=models)

    except Exception as e:
        raise handle_proxy_error(e)


# ============================================================================
# Anthropic Messages Routes (US-4.2)
# ============================================================================


@router.post("/v1/messages", response_model=AnthropicMessagesResponse)
async def create_message(
    raw_request: Request,
    request: AnthropicMessagesRequest,
    context: Annotated[TokenContext, Depends(get_token_context)],
    proxy_service: Annotated[ProxyService, Depends(get_proxy_service)],
    _agent_run_id: Annotated[str | None, Depends(set_agent_run_id_from_header)],
    # Issue #4398: capture the client tool onto usage_logs (FR-6.1). Attached to
    # exactly the routes that already carry the agent_run_id dependency — i.e. the
    # routes that write a usage_logs row.
    _client_tool: Annotated[str | None, Depends(set_client_tool_from_header)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
    anthropic_version: Annotated[str | None, Header(alias="anthropic-version")] = None,
    anthropic_beta: Annotated[str | None, Header(alias="anthropic-beta")] = None,
) -> Response:
    """Create a message (Anthropic Messages format).

    Implements US-4.2: Anthropic Messages Format
    Issue #143: Includes async chat logging with PII scrubbing.

    Headers:
    - anthropic-version: Forwarded to Bedrock
    - anthropic-beta: Beta features to enable (comma-separated)
    - Authorization or X-Api-Key: Bearer token
    """
    try:
        # Parse anthropic-beta header (comma-separated list)
        beta_features = anthropic_beta.split(",") if anthropic_beta else None

        # Get request metadata for logging
        request_id = getattr(raw_request.state, "request_id", None) or str(id(raw_request))
        timestamp = datetime.now(UTC)
        pricing_capture = PricingCapture(request_id, request.model)
        request_body_dict = request.model_dump(mode="json")
        t0 = time.monotonic()

        if request.stream:
            # Return streaming response with logging wrapper
            stream = await proxy_service.messages(
                request, context, anthropic_version, beta_features, request_id=request_id, pricing_capture=pricing_capture
            )
            chat_logger = get_chat_logging_service()

            wrapped_stream = create_streaming_logging_wrapper(
                stream=stream,
                chat_logger=chat_logger,
                request_id=request_id,
                timestamp=timestamp,
                # Issue #4132: attribution — chat logs are what the budget-usage-tracker
                # Lambda reads to write budget_usage rows (#234), so this is a billing path.
                org_id=context.attributed_org_id,
                user_id=context.user_id,
                team_id=context.team_id,
                # Issue #4300: the initiating human, so an agent chain's spend
                # debits that person's cumulative budget and not just the agent
                # service account's. Server-resolved; empty if not human-rooted.
                root_human_id=context.attributed_user_id,
                account_type="service" if context.account_type == "service" else "human",
                model=request.model,
                api_format="anthropic",
                request_body=request_body_dict,
                headers=dict(raw_request.headers) if raw_request.headers else None,
                start_time=t0,
                pricing_capture=pricing_capture,
            )

            return sse_streaming_response(wrapped_stream)
        else:
            # Return regular response with logging
            response = await _invoke_with_failure_logging(
                proxy_service.messages(request, context, anthropic_version, beta_features, request_id=request_id, pricing_capture=pricing_capture),
                raw_request,
                context,
                pricing_capture,
                request_body_dict,
            )
            latency_ms = (time.monotonic() - t0) * 1000

            # Fire-and-forget logging
            chat_logger = get_chat_logging_service()
            chat_logger.log_chat_async(
                request_id=request_id,
                timestamp=timestamp,
                # Issue #4132: attribution — chat logs are what the budget-usage-tracker
                # Lambda reads to write budget_usage rows (#234), so this is a billing path.
                org_id=context.attributed_org_id,
                user_id=context.user_id,
                team_id=context.team_id,
                # Issue #4300: the initiating human, so an agent chain's spend
                # debits that person's cumulative budget and not just the agent
                # service account's. Server-resolved; empty if not human-rooted.
                root_human_id=context.attributed_user_id,
                account_type="service" if context.account_type == "service" else "human",
                model=request.model,
                api_format="anthropic",
                latency_ms=latency_ms,
                request_body=request_body_dict,
                response_body=response.model_dump(mode="json"),
                headers=dict(raw_request.headers) if raw_request.headers else None,
                pricing_capture=pricing_capture,
            )

            return Response(
                content=response.model_dump_json(),
                media_type="application/json",
            )

    except BedrockGatewayError as e:
        raise handle_proxy_error(e)
    except Exception as e:
        raise handle_proxy_error(e)


@router.post("/v1/messages/count_tokens", response_model=CountTokensResponse)
async def count_tokens(
    request: CountTokensRequest,
    context: Annotated[TokenContext, Depends(get_token_context)],
    model_resolver: Annotated[ModelResolver, Depends(get_model_resolver)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
) -> CountTokensResponse:
    """Count tokens for a messages request.

    This is an estimate based on the request content.
    """
    try:
        # Resolve model and check access
        bedrock_model_id = model_resolver.resolve_model(request.model)
        model_resolver.check_model_access(bedrock_model_id, context)

        # Estimate token count
        # This is a simple estimation - actual count would require API call
        total_chars = 0
        for msg in request.messages:
            if isinstance(msg.content, str):
                total_chars += len(msg.content)
            else:
                for block in msg.content:
                    if hasattr(block, "text"):
                        total_chars += len(block.text)

        if request.system:
            if isinstance(request.system, str):
                total_chars += len(request.system)

        # Rough estimate: ~4 chars per token
        estimated_tokens = max(1, total_chars // 4)

        return CountTokensResponse(input_tokens=estimated_tokens)

    except BedrockGatewayError as e:
        raise handle_proxy_error(e)
    except Exception as e:
        raise handle_proxy_error(e)


# ============================================================================
# Bedrock Pass-Through Routes (US-4.3)
# ============================================================================


@router.post("/bedrock/invoke")
async def invoke_model(
    request: Request,
    context: Annotated[TokenContext, Depends(get_token_context)],
    proxy_service: Annotated[ProxyService, Depends(get_proxy_service)],
    _agent_run_id: Annotated[str | None, Depends(set_agent_run_id_from_header)],
    # Issue #4398: capture the client tool onto usage_logs (FR-6.1). Attached to
    # exactly the routes that already carry the agent_run_id dependency — i.e. the
    # routes that write a usage_logs row.
    _client_tool: Annotated[str | None, Depends(set_client_tool_from_header)],
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Invoke Bedrock model (pass-through).

    Implements US-4.3: Bedrock InvokeModel Pass-Through

    The request body is forwarded to Bedrock with minimal transformation.
    anthropic_beta and anthropic_version body fields are preserved.
    """
    try:
        # Parse request body
        body = await request.json()
        model_id = body.pop("model", body.pop("modelId", None))

        if not model_id:
            raise HTTPException(status_code=400, detail="model or modelId is required")

        capture = PricingCapture(getattr(request.state, "request_id", None) or str(id(request)), model_id)
        log_args = _pricing_log_args(request, context, capture, body.copy())
        started = time.monotonic()

        # Invoke model
        response = await _invoke_with_failure_logging(
            proxy_service.invoke_model(model_id, body, context, stream=False, pricing_capture=capture), request, context, capture, body.copy()
        )

        get_chat_logging_service().log_chat_async(
            **log_args,
            response_body=capture.response_body or response,
            latency_ms=(time.monotonic() - started) * 1000,
        )
        return Response(
            content=response if isinstance(response, bytes) else str(response).encode(),
            media_type="application/json",
        )

    except BedrockGatewayError as e:
        raise handle_proxy_error(e)
    except HTTPException:
        raise
    except Exception as e:
        raise handle_proxy_error(e)


@router.post("/bedrock/invoke-with-response-stream")
async def invoke_model_with_response_stream(
    request: Request,
    context: Annotated[TokenContext, Depends(get_token_context)],
    proxy_service: Annotated[ProxyService, Depends(get_proxy_service)],
    _agent_run_id: Annotated[str | None, Depends(set_agent_run_id_from_header)],
    # Issue #4398: capture the client tool onto usage_logs (FR-6.1). Attached to
    # exactly the routes that already carry the agent_run_id dependency — i.e. the
    # routes that write a usage_logs row.
    _client_tool: Annotated[str | None, Depends(set_client_tool_from_header)],
    authorization: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Invoke Bedrock model with streaming response (pass-through).

    Implements US-4.3: Bedrock InvokeModel Pass-Through

    The response is streamed back in Bedrock's native format.
    Works with CLAUDE_CODE_SKIP_BEDROCK_AUTH=1.
    """
    try:
        # Parse request body
        body = await request.json()
        model_id = body.pop("model", body.pop("modelId", None))

        if not model_id:
            raise HTTPException(status_code=400, detail="model or modelId is required")

        capture = PricingCapture(getattr(request.state, "request_id", None) or str(id(request)), model_id)
        log_args = _pricing_log_args(request, context, capture, body.copy())
        started = time.monotonic()

        # Invoke model with streaming
        stream = await proxy_service.invoke_model(model_id, body, context, stream=True, pricing_capture=capture)

        wrapped = create_streaming_logging_wrapper(
            stream=stream,
            chat_logger=get_chat_logging_service(),
            start_time=started,
            **log_args,
        )
        return sse_streaming_response(wrapped)

    except BedrockGatewayError as e:
        raise handle_proxy_error(e)
    except HTTPException:
        raise
    except Exception as e:
        raise handle_proxy_error(e)


# ============================================================================
# Bedrock SDK URL Pattern Routes
# ============================================================================
# Claude Code in Bedrock mode sends requests to:
#   POST /model/<model-id>/invoke
#   POST /model/<model-id>/invoke-with-response-stream
# These routes match that pattern and delegate to the existing handlers.


@router.post("/model/{model_id}/invoke")
async def invoke_model_by_path(
    model_id: str,
    request: Request,
    context: Annotated[TokenContext, Depends(get_token_context)],
    proxy_service: Annotated[ProxyService, Depends(get_proxy_service)],
    # Issue #1753: this native-Bedrock URL pattern is what the Claude SDK
    # actually calls (ANTHROPIC_BEDROCK_BASE_URL). The #1616 cost-attribution
    # work added set_agent_run_id_from_header to the OpenAI/Anthropic/bedrock-
    # invoke routes but MISSED this path — so agent_run_id was NULL on 100% of
    # usage_logs rows and per-run cost never linked. Add the dependency here.
    _agent_run_id: Annotated[str | None, Depends(set_agent_run_id_from_header)],
    # Issue #4398: capture the client tool onto usage_logs (FR-6.1). Attached to
    # exactly the routes that already carry the agent_run_id dependency — i.e. the
    # routes that write a usage_logs row.
    _client_tool: Annotated[str | None, Depends(set_client_tool_from_header)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
) -> Response:
    """Invoke Bedrock model (SDK URL pattern: /model/{model_id}/invoke).

    Issue #143: Includes async chat logging with PII scrubbing.
    Issue #144: Instrumented with timing for bedrock and serialize segments.
    """
    timings = get_timings(request)
    try:
        body = await request.json()
        request_body_copy = body.copy()  # Copy for logging
        logger.info(
            "Bedrock invoke request",
            extra={
                "model_id": model_id,
                "body_keys": list(body.keys()),
                "has_tools": "tools" in body,
                "has_tool_choice": "tool_choice" in body,
                "has_anthropic_beta": "anthropic_beta" in body,
                "has_metadata": "metadata" in body,
            },
        )

        # Get request metadata for logging
        request_id = getattr(request.state, "request_id", None) or str(id(request))
        timestamp = datetime.now(UTC)
        pricing_capture = PricingCapture(request_id, model_id)

        # Issue #144: Time bedrock invocation
        with timings.time_segment("bedrock"):
            # Issue #1755: thread agent_run_id explicitly (like request_id) — the
            # route-dependency contextvar does NOT survive to _log_usage across the
            # service-call boundary, so agent_run_id was NULL on 100% of rows even
            # though the header arrives and the dependency (#1781) sets it.
            response = await _invoke_with_failure_logging(
                proxy_service.invoke_model(
                    model_id,
                    body,
                    context,
                    stream=False,
                    request_id=request_id,
                    agent_run_id=_agent_run_id,
                    pricing_capture=pricing_capture,
                ),
                request,
                context,
                pricing_capture,
                request_body_copy,
            )
        bedrock_ms = timings.get("bedrock")

        logger.info(
            "Bedrock invoke response",
            extra={
                "model_id": model_id,
                "bedrock_latency_ms": bedrock_ms,
                "response_keys": list(response.keys()) if isinstance(response, dict) else "non-dict",
            },
        )

        # Issue #144: Time serialization
        with timings.time_segment("serialize"):
            content = json.dumps(response)

        # Issue #143: Fire-and-forget chat logging
        chat_logger = get_chat_logging_service()
        chat_logger.log_chat_async(
            request_id=request_id,
            timestamp=timestamp,
            # Issue #4132: attribution — chat logs are what the budget-usage-tracker
            # Lambda reads to write budget_usage rows (#234), so this is a billing path.
            org_id=context.attributed_org_id,
            user_id=context.user_id,
            team_id=context.team_id,
            # Issue #4300: the initiating human, so an agent chain's spend
            # debits that person's cumulative budget and not just the agent
            # service account's. Server-resolved; empty if not human-rooted.
            root_human_id=context.attributed_user_id,
            account_type="service" if context.account_type == "service" else "human",
            model=model_id,
            api_format="bedrock",
            latency_ms=bedrock_ms,
            request_body=request_body_copy,
            response_body=response if isinstance(response, dict) else {"raw": str(response)},
            headers=dict(request.headers) if request.headers else None,
            pricing_capture=pricing_capture,
        )

        return Response(
            content=content,
            media_type="application/json",
        )
    except BedrockGatewayError as e:
        raise handle_proxy_error(e)
    except HTTPException:
        raise
    except Exception as e:
        raise handle_proxy_error(e)


@router.post("/model/{model_id}/invoke-with-response-stream")
async def invoke_model_stream_by_path(
    model_id: str,
    request: Request,
    context: Annotated[TokenContext, Depends(get_token_context)],
    proxy_service: Annotated[ProxyService, Depends(get_proxy_service)],
    # Issue #1753: same fix as the non-streaming sibling — the SDK's native
    # Bedrock URL pattern must read x-agent-runid so usage_logs.agent_run_id
    # is populated and per-run cost links.
    _agent_run_id: Annotated[str | None, Depends(set_agent_run_id_from_header)],
    # Issue #4398: capture the client tool onto usage_logs (FR-6.1). Attached to
    # exactly the routes that already carry the agent_run_id dependency — i.e. the
    # routes that write a usage_logs row.
    _client_tool: Annotated[str | None, Depends(set_client_tool_from_header)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
) -> StreamingResponse:
    """Invoke Bedrock model with streaming (SDK URL pattern: /model/{model_id}/invoke-with-response-stream).

    Issue #143: Buffers streaming response for async chat logging.
    Issue #144: Instrumented with timing for bedrock_ttfb segment (time to first byte).
    """
    timings = get_timings(request)

    try:
        body = await request.json()
        request_body_copy = body.copy()  # Copy for logging

        # Get request metadata for logging
        request_id = getattr(request.state, "request_id", None) or str(id(request))
        timestamp = datetime.now(UTC)
        pricing_capture = PricingCapture(request_id, model_id)
        t0 = time.monotonic()

        # Issue #144: Time to get the stream object (includes model resolution)
        with timings.time_segment("bedrock_ttfb"):
            # Issue #1755: thread agent_run_id explicitly (see non-streaming route).
            stream = await proxy_service.invoke_model(
                model_id,
                body,
                context,
                stream=True,
                request_id=request_id,
                agent_run_id=_agent_run_id,
                pricing_capture=pricing_capture,
            )
        chat_logger = get_chat_logging_service()

        # Issue #143: Use shared streaming logging wrapper
        wrapped_stream = create_streaming_logging_wrapper(
            stream=stream,
            chat_logger=chat_logger,
            request_id=request_id,
            timestamp=timestamp,
            # Issue #4132: attribution — chat logs are what the budget-usage-tracker
            # Lambda reads to write budget_usage rows (#234), so this is a billing path.
            org_id=context.attributed_org_id,
            user_id=context.user_id,
            team_id=context.team_id,
            # Issue #4300: the initiating human, so an agent chain's spend
            # debits that person's cumulative budget and not just the agent
            # service account's. Server-resolved; empty if not human-rooted.
            root_human_id=context.attributed_user_id,
            account_type="service" if context.account_type == "service" else "human",
            model=model_id,
            api_format="bedrock",
            request_body=request_body_copy,
            headers=dict(request.headers) if request.headers else None,
            start_time=t0,
            pricing_capture=pricing_capture,
        )

        # This is the Bedrock-native URL pattern, so the default wire format is
        # Bedrock's binary eventstream — what every Bedrock-flavored client
        # (the Claude Code SDK on the agent workers, most of all) decodes.
        # Serving text SSE here made claude-cli consume the whole stream, fail
        # to decode it, and silently retry the request non-streaming: every
        # agent turn generated (and billed) twice. Clients that explicitly ask
        # for SSE via Accept keep the old behaviour (curl debugging, tests).
        accept = (request.headers.get("accept") or "").lower()
        if "text/event-stream" in accept:
            return BedrockStreamingResponse(
                merge_with_keepalive(wrapped_stream),
                error_handler=handle_proxy_error,
                media_type="text/event-stream",
                headers=dict(_SSE_HEADERS),
            )

        # Binary AWS-eventstream path (the default for Bedrock-native clients such
        # as the Claude Code SDK). Inject the keep-alive AFTER conversion, at the
        # wire level, as a Bedrock ``ping`` chunk frame — sse_to_eventstream drops
        # SSE comments, so the SSE-comment keep-alive would never survive here.
        return BedrockStreamingResponse(
            merge_with_keepalive(
                sse_to_eventstream(wrapped_stream),
                keepalive=EVENTSTREAM_KEEPALIVE,
                record_aligned=True,
            ),
            error_handler=handle_proxy_error,
            media_type=EVENTSTREAM_CONTENT_TYPE,
            headers=dict(_SSE_HEADERS),
        )
    except BedrockGatewayError as e:
        raise handle_proxy_error(e)
    except HTTPException:
        raise
    except Exception as e:
        raise handle_proxy_error(e)


# ============================================================================
# OpenAI Responses-API Passthrough (bedrock-mantle) — Issue #2709
# ============================================================================
# Proxies OpenAI Responses-API traffic to the bedrock-mantle endpoint so Codex
# (and future OpenAI-model clients) route through the gateway and get the same
# per-tenant metering + model-allowlist governance Claude traffic gets today.
# Passthrough, NOT translation — the body is forwarded verbatim.


def _mantle_model_configured(model: str) -> bool:
    """Check the requested model matches a configured mantle allowlist pattern.

    This is the route-level gate (which model IDs the mantle route will serve at
    all). Per-tenant access is enforced separately via the model allowlist
    (ModelResolver.check_model_access), mirroring the other proxy routes.
    """
    settings = get_settings()
    patterns = [p.strip() for p in settings.mantle_allowed_models.split(",") if p.strip()]
    return any(fnmatch.fnmatch(model, pattern) for pattern in patterns)


@router.post("/openai/v1/responses")
async def create_openai_response(
    request: Request,
    context: Annotated[TokenContext, Depends(get_token_context)],
    mantle_service: Annotated[MantlePassthroughService, Depends(get_mantle_service)],
    model_resolver: Annotated[ModelResolver, Depends(get_model_resolver)],
    _agent_run_id: Annotated[str | None, Depends(set_agent_run_id_from_header)],
    # Issue #4398: capture the client tool onto usage_logs (FR-6.1). Attached to
    # exactly the routes that already carry the agent_run_id dependency — i.e. the
    # routes that write a usage_logs row.
    _client_tool: Annotated[str | None, Depends(set_client_tool_from_header)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
) -> Response:
    """Proxy an OpenAI Responses-API request to bedrock-mantle (Issue #2709).

    The request body is forwarded byte-for-byte; the response (including
    streaming chunks) is returned verbatim. Usage is metered per tenant.

    Auth/allowlist: identical tenant-auth chain as the other proxy routes, plus
    a per-tenant model-allowlist check (tenant needs an ``openai.*`` grant).
    """
    # Read the raw body once — we forward it verbatim (passthrough, no re-encode).
    body = await request.body()

    # Parse just enough to determine model + stream flag; do NOT mutate the body.
    try:
        parsed = json.loads(body) if body else {}
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail={"error": "invalid_request", "message": "request body must be valid JSON"})

    model = parsed.get("model", "")
    if not model:
        raise HTTPException(status_code=400, detail={"error": "invalid_request", "message": "'model' is required"})

    # Route-level gate: is this an OpenAI model this route is configured to serve?
    if not _mantle_model_configured(model):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "message": f"model '{model}' is not served by the OpenAI passthrough route"},
        )

    # Per-tenant model-allowlist enforcement (same middleware as existing routes).
    # A tenant without an openai.* allowlist entry gets 403.
    try:
        model_resolver.check_model_access(model, context)
    except BedrockGatewayError as e:
        raise handle_proxy_error(e)

    stream = bool(parsed.get("stream", False))
    request_id = getattr(request.state, "request_id", None) or str(id(request))

    try:
        result = await mantle_service.create_response(
            body,
            context,
            stream=stream,
            model=model,
            request_id=request_id,
            agent_run_id=_agent_run_id,
        )

        if stream:
            return sse_streaming_response(result, extra_headers={"X-Request-ID": request_id})

        # Non-streaming: pass upstream status + body back verbatim, including
        # upstream 4xx/5xx, with the gateway request-id attached for tracing.
        return Response(
            content=result.content,
            status_code=result.status_code,
            media_type=result.media_type,
            headers={"X-Request-ID": request_id},
        )
    except MantleUpstreamError as e:
        # Streaming path failed before any bytes reached the client — return
        # the real upstream status/body instead of a 200 with a dead stream.
        return Response(
            content=e.content,
            status_code=e.status_code,
            media_type=e.media_type,
            headers={"X-Request-ID": request_id},
        )
    except BedrockGatewayError as e:
        raise handle_proxy_error(e)


# ============================================================================
# Health Check Routes
# ============================================================================


@router.get("/v1/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint for proxy routes."""
    return {"status": "healthy", "service": "proxy"}
