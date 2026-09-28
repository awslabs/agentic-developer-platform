"""Proxy service implementing IProxyService interface.

Handles all proxy requests across OpenAI, Anthropic, and Bedrock API formats.
"""

import asyncio
import contextvars
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

from src.budget.enforcement_service import reconcile_budget_reservation
from src.budget.pricing_decisions import price_completed_usage
from src.proxy.bedrock_enforcement import RoutingDecision, resolve_routing_decision
from src.proxy.bedrock_routing import BedrockTarget, resolve_shadow_target
from src.proxy.bedrock_routing_errors import (
    REASON_MODEL_NOT_ENABLED,
    BedrockAccountUnavailableError,
)
from src.proxy.exceptions import (
    BedrockInvocationError,
)
from src.proxy.format_translator import FormatTranslator
from src.proxy.model_resolver import ModelResolver
from src.proxy.pricing_capture import PricingCapture
from src.proxy.schemas import (
    AnthropicMessagesRequest,
    AnthropicMessagesResponse,
    BedrockInvokeRequest,
    BedrockInvokeResponse,
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionResponse,
)
from src.proxy.stream_handler import StreamHandler
from src.shared.database import get_session_factory
from src.shared.interfaces.pool import IPoolService
from src.shared.interfaces.proxy import IProxyService
from src.shared.logging import get_logger
from src.shared.metrics import emit_error_count, emit_request_metrics
from src.shared.schemas.auth import TokenContext
from src.usage.service import UsageService

logger = get_logger(__name__)

# Pricing has its own bounded read/fallback. Only subsequent best-effort DB and
# reservation work shares this budget, so a blocked write cannot withhold the
# already-computed durable settlement event after a client disconnect.
CLAUDE_PERSISTENCE_TIMEOUT_SECONDS = 10.0

# Issue #1074: Context variable for request_id so internal methods can
# propagate it to usage_logs without threading through every signature.
_current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("_current_request_id", default=None)

# Issue #1616: Context variable for agent_run_id (from X-Agent-RunId header)
# so _log_usage can write it to usage_logs for per-run cost traceability.
_current_agent_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("_current_agent_run_id", default=None)

# Issue #4398: Context variable for the normalised client tool (derived from the
# User-Agent by `src/proxy/client_tool.py`) so _log_usage can stamp it onto
# usage_logs without threading a new parameter through every proxy signature.
#
# Set by the ASYNC route dependency `set_client_tool_from_header`. The async-ness
# is load-bearing, not stylistic: a contextvar set inside a SYNC (`def`)
# dependency is lost before the endpoint body runs, because Starlette executes
# sync dependencies in a threadpool via `run_in_threadpool`, which copies the
# context — so the mutation lands on a throwaway copy. That is precisely why
# `agent_run_id` (set by the sync `set_agent_run_id_from_header`) read as NULL on
# 100% of rows until #1755 patched around it by threading the value explicitly
# through eight call sites. An async dependency runs on the request's own task
# context, so the value survives to _log_usage — including into a
# StreamingResponse generator's `finally`, which is where the streaming paths log.
_current_client_tool: contextvars.ContextVar[str | None] = contextvars.ContextVar("_current_client_tool", default=None)


def _classify_bedrock_failure(
    exc: Exception,
    *,
    model_id: str,
    target: BedrockTarget | None,
) -> Exception:
    """Turn a raw Bedrock failure into the right error class (§5.1, §5.2).

    Issue #4744. Before this, ``_invoke_bedrock`` caught bare ``Exception`` and raised
    ``BedrockInvocationError(str(e))``, which collapsed three genuinely different
    situations — "your account lacks this model", "your role cannot be assumed", and
    "Bedrock is down" — into one opaque 502. §5.1 calls that discrimination a
    **prerequisite** rather than a nicety, because without it the routing feature's most
    common failure mode is indistinguishable from an outage, and the operator's first
    instinct is to page someone rather than enable a model.

    Two rules, in order:

    1. **A fail-closed error passes through untouched.** ``BedrockAccountUnavailableError``
       already names its account, cause and fix; re-wrapping it would replace that with a
       generic transport message and lose the whole point.
    2. **On a ROUTED call, ``AccessDeniedException`` becomes ``model_not_enabled``.**
       Bedrock model access is per-account, and a routed call has already passed every
       ADP-side check (``check_model_access`` is a glob match against the caller's
       allowed-model config — it knows nothing about what the *destination* account has
       enabled). So the overwhelmingly likely cause is an unenabled model in the
       destination.

       Stated honestly, because it is an inference and not a certainty: a
       missing ``bedrock:InvokeModel`` on the role produces the same error code. The
       message therefore leads with the model-enablement fix — the far more common cause
       once R1's routing-capable template is what created the role, since that template
       grants InvokeModel explicitly — and the ``account_id`` in the payload is what lets
       an operator check the other possibility. A wrong-but-actionable message naming the
       right account beats a correct-but-opaque one.

    Everything else — transport failures, throttling, ``ValidationException`` for a
    malformed body — keeps main's exact behaviour, including on routed calls. Reclassifying
    those would change platform-account error handling, which this issue has no mandate to
    touch.
    """
    if isinstance(exc, BedrockAccountUnavailableError):
        return exc

    error_code = ""
    if isinstance(exc, ClientError):
        error_code = exc.response.get("Error", {}).get("Code", "") or ""

    if target is not None and not target.is_platform and error_code in ("AccessDeniedException", "AccessDenied"):
        logger.error(
            "Routed Bedrock call denied by the destination account",
            extra={
                "bedrock_account_id": target.account_id,
                "rung": target.rung,
                "model_id": model_id,
                "bedrock_error_code": error_code,
            },
        )
        return BedrockAccountUnavailableError(
            reason=REASON_MODEL_NOT_ENABLED,
            account_id=target.account_id or "unknown",
            scope=target.rung,  # type: ignore[arg-type]
            model_id=model_id,
        )

    logger.error(f"Bedrock invocation error: {exc}")
    # Main's behaviour, preserved exactly — including the error code now that we have
    # parsed one, which costs nothing and makes a transport failure debuggable.
    return BedrockInvocationError(str(exc), bedrock_error_code=error_code or None)


class ProxyService(IProxyService):
    """Service for proxying requests to Bedrock.

    Implements IProxyService interface and handles:
    - US-4.1: OpenAI-compatible chat completions
    - US-4.2: Anthropic Messages format
    - US-4.3: Bedrock InvokeModel pass-through
    - US-9.6: Model access control
    """

    def __init__(
        self,
        pool_service: IPoolService,
        model_resolver: ModelResolver | None = None,
        format_translator: FormatTranslator | None = None,
        stream_handler: StreamHandler | None = None,
    ) -> None:
        """Initialize the proxy service.

        Args:
            pool_service: Pool service for getting Bedrock clients
            model_resolver: Optional custom model resolver
            format_translator: Optional custom format translator
            stream_handler: Optional custom stream handler
        """
        self._pool_service = pool_service
        self._model_resolver = model_resolver or ModelResolver()
        self._translator = format_translator or FormatTranslator()
        self._stream_handler = stream_handler or StreamHandler()

    # =========================================================================
    # IProxyService Interface Implementation
    # =========================================================================

    async def invoke(
        self,
        request: dict[str, Any],
        context: TokenContext,
    ) -> dict[str, Any]:
        """Invoke Bedrock model with the given request.

        Args:
            request: The request dictionary (format depends on api_format field)
            context: Authentication context

        Returns:
            Response dictionary in the same format as the request
        """
        api_format = request.get("api_format", "bedrock")
        start_time = time.time()
        model = request.get("model", "unknown")

        try:
            # Resolve model and check access
            bedrock_model_id = self._model_resolver.resolve_model(model)
            self._model_resolver.check_model_access(bedrock_model_id, context)

            # Convert request to Bedrock format
            bedrock_request = self._prepare_bedrock_request(request, bedrock_model_id, api_format)

            # Issue #4744: decide where this call is signed for, then get the matching
            # client. `decision.credentials is None` (unmapped, or org not opted in)
            # returns the ambient IRSA client — identical to main.
            decision = await resolve_routing_decision(context)

            # Get Bedrock client from pool
            client = await self._client_for_request(decision, context)

            # Invoke Bedrock
            response = await self._invoke_bedrock(client, bedrock_model_id, bedrock_request, decision.target, context=context)

            # Calculate latency
            latency_ms = (time.time() - start_time) * 1000

            # Extract token usage from response for metrics
            # Issue #1486: Read from response.usage dict (not top-level attrs)
            tokens_in = response.usage.get("input_tokens", 0) or 0
            tokens_out = response.usage.get("output_tokens", 0) or 0
            cost_usd = 0.0  # Cost calculation would be done by budget service

            # Emit metrics
            emit_request_metrics(
                # Issue #4132: metric org dimension is attribution (per-tenant
                # usage dashboards), not an authorization decision.
                org_id=context.attributed_org_id,
                model=model,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                success=True,
            )

            logger.info(
                "Proxy invoke completed",
                extra={
                    "model": model,
                    "bedrock_model_id": bedrock_model_id,
                    "latency_ms": round(latency_ms, 2),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                },
            )

            # Convert response to original format
            return self._convert_response(response, model, api_format, latency_ms)

        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            error_type = type(e).__name__

            # Emit error metrics
            emit_error_count(
                # Issue #4132: attribution — see emit_request_metrics above.
                org_id=context.attributed_org_id,
                model=model,
                error_type=error_type,
            )

            logger.error(
                "Error in proxy invoke",
                extra={
                    "model": model,
                    "error": str(e),
                    "error_type": error_type,
                    "latency_ms": round(latency_ms, 2),
                },
            )
            raise

    async def invoke_stream(
        self,
        request: dict[str, Any],
        context: TokenContext,
    ) -> AsyncIterator[bytes]:
        """Invoke Bedrock model with streaming response.

        Args:
            request: The request dictionary
            context: Authentication context

        Yields:
            SSE formatted response chunks
        """
        api_format = request.get("api_format", "bedrock")
        model = request.get("model", "")
        start_time = time.time()
        usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        status_code = 200
        # Issue #4744: initialised before the try so the `finally` can always record
        # which account served the call — including when the failure was the routing
        # decision itself, where an unset local would raise inside the settlement path
        # and lose the usage row entirely.
        decision = RoutingDecision()

        try:
            # Resolve model and check access
            bedrock_model_id = self._model_resolver.resolve_model(model)
            self._model_resolver.check_model_access(bedrock_model_id, context)

            # Convert request to Bedrock format
            bedrock_request = self._prepare_bedrock_request(request, bedrock_model_id, api_format)

            # Issue #4744: routing decision, then the matching client (see `invoke`).
            decision = await resolve_routing_decision(context)

            # Get Bedrock client from pool
            client = await self._client_for_request(decision, context)

            # Invoke Bedrock with streaming
            response_id = str(uuid.uuid4())
            bedrock_stream = await self._invoke_bedrock_stream(client, bedrock_model_id, bedrock_request, decision.target, context=context)

            # Convert stream to target format
            async for chunk in self._stream_handler.create_sse_response(bedrock_stream, api_format, model, response_id):
                self._extract_usage_from_sse_chunk(chunk, usage)
                yield chunk

        except Exception as e:
            status_code = 500
            logger.error(f"Error in proxy invoke_stream: {e}")
            raise
        finally:
            latency_ms = (time.time() - start_time) * 1000
            await self._log_usage(
                context=context,
                model=model,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cost_usd=0.0,
                latency_ms=int(latency_ms),
                status_code=status_code,
                # Issue #4180: absent keys stay absent in the accumulator, so
                # .get() yields None for "provider never reported it".
                cache_read_input_tokens=usage.get("cache_read_input_tokens"),
                cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
                routing_decision=decision,
            )

    # =========================================================================
    # API Format-Specific Methods
    # =========================================================================

    async def chat_completions(
        self,
        request: OpenAIChatCompletionRequest,
        context: TokenContext,
        anthropic_version: str | None = None,
        anthropic_beta: list[str] | None = None,
        pricing_capture: PricingCapture | None = None,
    ) -> OpenAIChatCompletionResponse | AsyncIterator[bytes]:
        """Handle OpenAI-compatible chat completions (US-4.1).

        Args:
            request: OpenAI format request
            context: Authentication context
            anthropic_version: Optional Anthropic version header
            anthropic_beta: Optional Anthropic beta features

        Returns:
            OpenAI format response or SSE stream
        """
        pricing_capture = pricing_capture or PricingCapture(str(uuid.uuid4()), request.model)

        # Resolve model
        bedrock_model_id = self._model_resolver.resolve_model(request.model)
        self._model_resolver.check_model_access(bedrock_model_id, context)

        # Convert to Bedrock format
        bedrock_request = self._translator.openai_to_bedrock(request, bedrock_model_id)

        if request.stream:
            return self._stream_openai_response(bedrock_request, bedrock_model_id, request.model, context, pricing_capture=pricing_capture)
        else:
            return await self._invoke_openai_response(bedrock_request, bedrock_model_id, request.model, context, pricing_capture=pricing_capture)

    async def messages(
        self,
        request: AnthropicMessagesRequest,
        context: TokenContext,
        anthropic_version: str | None = None,
        anthropic_beta: list[str] | None = None,
        request_id: str | None = None,
        pricing_capture: PricingCapture | None = None,
    ) -> AnthropicMessagesResponse | AsyncIterator[bytes]:
        """Handle Anthropic Messages format (US-4.2).

        Args:
            request: Anthropic format request
            context: Authentication context
            anthropic_version: Version header value
            anthropic_beta: Beta features to enable
            request_id: Optional request ID for usage_logs correlation (Issue #1074)

        Returns:
            Anthropic format response or SSE stream
        """
        # Issue #1074: Set request_id in contextvar for _log_usage to pick up
        if request_id:
            _current_request_id.set(request_id)

        pricing_capture = pricing_capture or PricingCapture(request_id or str(uuid.uuid4()), request.model)

        # Resolve model
        bedrock_model_id = self._model_resolver.resolve_model(request.model)
        self._model_resolver.check_model_access(bedrock_model_id, context)

        # Convert to Bedrock format
        bedrock_request = self._translator.anthropic_to_bedrock(request, anthropic_version, anthropic_beta)

        if request.stream:
            return self._stream_anthropic_response(bedrock_request, bedrock_model_id, request.model, context, pricing_capture=pricing_capture)
        else:
            return await self._invoke_anthropic_response(bedrock_request, bedrock_model_id, request.model, context, pricing_capture=pricing_capture)

    async def invoke_model(
        self,
        model_id: str,
        body: dict[str, Any],
        context: TokenContext,
        stream: bool = False,
        request_id: str | None = None,
        agent_run_id: str | None = None,
        pricing_capture: PricingCapture | None = None,
    ) -> dict[str, Any] | AsyncIterator[bytes]:
        """Handle Bedrock InvokeModel pass-through (US-4.3).

        Args:
            model_id: Bedrock model ID
            body: Request body to pass through
            context: Authentication context
            stream: Whether to use streaming
            request_id: Optional request ID for usage_logs correlation (Issue #1074)
            agent_run_id: Optional agent run id for per-run cost attribution.
                Issue #1755: threaded explicitly because the route-dependency
                contextvar does not survive across the service-call boundary to
                _log_usage (mirrors the request_id fix from #1074).

        Returns:
            Bedrock response or SSE stream
        """
        # Issue #1074: Set request_id in contextvar for _log_usage to pick up
        if request_id:
            _current_request_id.set(request_id)
        # Issue #1755: re-set agent_run_id contextvar in THIS (service) context so
        # _log_usage reads it reliably — the route-set value is lost across the call.
        if agent_run_id:
            _current_agent_run_id.set(agent_run_id)

        pricing_capture = pricing_capture or PricingCapture(request_id or str(uuid.uuid4()), model_id)

        # Resolve model (in case it's an alias)
        bedrock_model_id = self._model_resolver.resolve_model(model_id)
        self._model_resolver.check_model_access(bedrock_model_id, context)

        # Create Bedrock request from body
        bedrock_request = BedrockInvokeRequest(**body)

        if stream:
            return self._stream_bedrock_response(bedrock_request, bedrock_model_id, context, pricing_capture=pricing_capture)
        else:
            return await self._invoke_bedrock_response(bedrock_request, bedrock_model_id, context, pricing_capture=pricing_capture)

    # =========================================================================
    # Usage Logging (Issue #992)
    # =========================================================================

    async def _log_usage(self, **kwargs) -> None:
        capture = kwargs.get("pricing_capture")
        if capture is None or capture.routing is None:
            await self._log_usage_impl(**kwargs)
            return
        # A disconnect may arrive after measured usage while pricing or usage
        # persistence is awaiting I/O. Keep this one request's completion alive;
        # the logging wrapper awaits it before emitting the durable S3 event.
        task = asyncio.create_task(self._log_usage_impl(**kwargs), name=f"pricing_finalize_{capture.request_id}")
        capture.finalization_task = task
        await asyncio.shield(task)

    async def _log_usage_impl(
        self,
        context: TokenContext,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float | Decimal,
        latency_ms: int,
        status_code: int,
        request_id: str | None = None,
        cache_read_input_tokens: int | None = None,
        cache_creation_input_tokens: int | None = None,
        routing_decision: RoutingDecision | None = None,
        pricing_capture: PricingCapture | None = None,
    ) -> None:
        """Write a row to usage_logs for admin dashboard visibility.

        Issue #1074: Now includes request_id (from contextvar or explicit param)
        so the budget-usage-tracker Lambda can bridge calculated cost back to
        usage_logs.cost_usd.

        Issue #1616: Now includes agent_run_id (from contextvar, set by route
        handler from X-Agent-RunId header) for per-run cost traceability.

        Issue #4180: Now includes the prompt-cache token counters. Callers must
        pass None (not 0) when the provider did not report them — see
        ``_cache_tokens_from_usage``.

        Issue #4398: Now includes client_tool (from contextvar, set by the async
        route dependency from the User-Agent). None is written as NULL and means
        "not captured", never "unknown tool".

        Issue #4287: also reconciles this request's budget reservation. Every
        caller invokes this from a ``finally``, so it is the one point that runs
        on success AND on failure — which makes it both the "charge the real
        cost" hook and the "release what a failed request was holding" hook.

        Issue #4743: now also records the Bedrock account this call *would* have
        been routed to (shadow mode, §8.2 phase 1). It is resolved here rather
        than at client-construction time precisely because nothing about signing
        changes: this method is the settlement point, the column it writes has
        existed unwritten since 001, and resolving alongside the other metering
        reads keeps the routing decision out of the invoke path entirely. When
        the flag is off, or resolution fails, the column stays NULL — "not
        captured", never a fabricated account.

        Failures are swallowed to avoid impacting the proxy hot path.
        """
        # Issue #1074: Use contextvar if no explicit request_id provided
        if request_id is None:
            request_id = _current_request_id.get()

        # Issue #1616: Pick up agent_run_id from contextvar
        agent_run_id = _current_agent_run_id.get()

        # Issue #4398: Pick up the normalised client tool from its contextvar.
        # Already normalised to the closed set (or None) by the route dependency,
        # so nothing here can raise and no raw User-Agent can reach the column.
        # None means "not captured" and is written as NULL — never a placeholder.
        client_tool = _current_client_tool.get()

        actual_cost = None
        pricing_failed = False
        pricing_decision = None
        rejection = pricing_capture.no_inference_rejection if pricing_capture is not None else None
        if rejection is not None and status_code >= 400 and input_tokens == output_tokens == 0:
            # This is a provider rejection receipt, not missing usage interpreted
            # as zero. Keep the error record and provider identity; do not invent
            # a successful pricing decision or reset any other request's charge.
            request_id = pricing_capture.request_id
            actual_cost = cost_usd = Decimal("0")
            cache_read_input_tokens = cache_creation_input_tokens = None
            logger.info(
                "Settling verified Bedrock rejection before inference",
                extra={"request_id": request_id, "bedrock_error_code": rejection.code, "operation": rejection.operation},
            )
        elif pricing_capture is not None and pricing_capture.routing is not None:
            request_id = pricing_capture.request_id
            try:
                priced = await price_completed_usage(
                    request_id=request_id,
                    org_id=context.attributed_org_id,
                    raw_usage=pricing_capture.raw_usage,
                    evidence=pricing_capture.routing,
                    api_format="anthropic",
                )
                pricing_capture.decision = priced.to_dict()
                pricing_decision = priced
                actual_cost = cost_usd = priced.ledger_cost
                input_tokens = priced.usage["uncached_input_tokens"]
                output_tokens = priced.usage["output_tokens"]
                cache_read_input_tokens = (
                    priced.usage["cache_read_input_tokens"] if priced.usage["raw"]["cache_read_input_tokens"] is not None else None
                )
                cache_creation_input_tokens = (
                    priced.usage["cache_creation_input_tokens"] if priced.usage["raw"]["cache_creation_input_tokens"] is not None else None
                )
            except Exception as exc:
                logger.warning("Provider usage could not be priced", extra={"request_id": request_id, "error": str(exc)})
                # Do not invent zero-token success or emit a legacy-priced event
                # when the provider omitted usage or source verification failed.
                pricing_failed = True
                cost_usd = Decimal("0")

        if pricing_capture is not None and pricing_capture.routing is None and rejection is None:
            pricing_failed = True

        async def persist_usage():
            if pricing_failed:
                await reconcile_budget_reservation(
                    context=context,
                    request_id=request_id,
                    model_id=model,
                    input_tokens=0,
                    output_tokens=0,
                    actual_cost_usd=Decimal("0"),
                    usage_known=False,
                )
                if status_code < 400:
                    return
                # Keep the existing diagnostic record for a failed invocation.
                # Its error status and zero charge are not a measured success;
                # no pricing decision exists, so no settlement event is emitted.

            # Issue #4743/#4744: which Bedrock account this call belongs to.
            #
            # Two sources, and the distinction is the whole audit value of the column:
            #
            #  - ENFORCED (routing_decision.is_enforced): the caller already resolved the
            #    target AND signed with it, so this is the account that ACTUALLY served the
            #    call. Reuse it — re-resolving here would walk the ladder a second time per
            #    request and, worse, could disagree with what was signed if a mapping
            #    changed mid-request, putting a wrong account in the audit trail.
            #  - SHADOW (no decision, or enforcement not active for this org): resolve the
            #    would-be target for observation only. The request was signed with ambient
            #    IRSA before this ran, exactly as on main.
            #
            # In both cases None persists as NULL, meaning "not captured" — never a
            # fabricated account id.
            if routing_decision is not None and routing_decision.is_enforced:
                bedrock_account_id = routing_decision.target.account_id if routing_decision.target else None
            else:
                shadow_target = await resolve_shadow_target(context)
                bedrock_account_id = shadow_target.account_id if shadow_target else None

            try:
                session_factory = get_session_factory()
                async with session_factory() as session:
                    usage_service = UsageService(session)
                    await usage_service.log_request(
                        context=context,
                        model=model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_usd=cost_usd,
                        latency_ms=latency_ms,
                        status_code=status_code,
                        request_id=request_id,
                        agent_run_id=agent_run_id,
                        cache_read_input_tokens=cache_read_input_tokens,
                        cache_creation_input_tokens=cache_creation_input_tokens,
                        client_tool=client_tool,
                        # Issue #4743 (shadow) / #4744 (enforced): see the resolution above.
                        bedrock_account_id=bedrock_account_id,
                        pricing_decision=pricing_decision,
                        provider_request_id=pricing_capture.provider_request_id if pricing_capture else None,
                        destination_region=pricing_capture.routing.endpoint_region if pricing_capture and pricing_capture.routing else None,
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to write usage_logs row",
                    extra={"error": str(exc), "model": model},
                )
                await reconcile_budget_reservation(
                    context=context,
                    request_id=request_id,
                    model_id=model,
                    input_tokens=0,
                    output_tokens=0,
                    actual_cost_usd=Decimal("0"),
                    usage_known=False,
                )
            else:
                if not pricing_failed:
                    await reconcile_budget_reservation(
                        context=context,
                        request_id=request_id,
                        model_id=model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        **({"actual_cost_usd": actual_cost} if actual_cost is not None else {}),
                    )

        if pricing_capture is not None and pricing_capture.routing is not None:
            try:
                async with asyncio.timeout(CLAUDE_PERSISTENCE_TIMEOUT_SECONDS):
                    await persist_usage()
            except TimeoutError:
                # The existing estimate remains held; never perform an unbounded
                # second Redis attempt after this finalizer's deadline.
                context._budget_accounting_incomplete = True
                logger.warning(
                    "Claude usage persistence timed out; keeping measured pricing decision for settlement",
                    extra={"request_id": request_id, "pricing_decision_available": pricing_capture.decision is not None},
                )
        else:
            await persist_usage()

    @staticmethod
    def _cache_tokens_from_usage(usage: dict[str, Any]) -> tuple[int | None, int | None]:
        """Read the prompt-cache counters out of a raw provider usage payload.

        Issue #4180: the RAW dict is the only null-preserving source in the
        codebase. Every typed producer coerces these to 0 (``AnthropicUsage``
        defaults them, chat_logging uses ``.get(..., 0)``, the tracker Lambda
        does ``int(x or 0)``), so reading from any of those would pin the columns
        at 0 forever and destroy the "unreported vs. reported zero" distinction
        the hit-rate query depends on. Hence ``.get()`` with NO default.
        """
        return usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens")

    # =========================================================================
    # Internal Methods
    # =========================================================================

    def _prepare_bedrock_request(
        self,
        request: dict[str, Any],
        bedrock_model_id: str,
        api_format: str,
    ) -> BedrockInvokeRequest:
        """Prepare a Bedrock request from the input request.

        Args:
            request: Input request dictionary
            bedrock_model_id: Resolved Bedrock model ID
            api_format: Source API format

        Returns:
            Bedrock invoke request
        """
        if api_format == "openai":
            openai_request = OpenAIChatCompletionRequest(**request)
            return self._translator.openai_to_bedrock(openai_request, bedrock_model_id)
        elif api_format == "anthropic":
            anthropic_request = AnthropicMessagesRequest(**request)
            return self._translator.anthropic_to_bedrock(anthropic_request)
        else:
            # Bedrock pass-through
            return BedrockInvokeRequest(**request)

    def _convert_response(
        self,
        response: BedrockInvokeResponse,
        model: str,
        api_format: str,
        latency_ms: float,
    ) -> dict[str, Any]:
        """Convert Bedrock response to the target format.

        Args:
            response: Bedrock response
            model: Original model name
            api_format: Target API format
            latency_ms: Request latency

        Returns:
            Response dictionary in target format
        """
        if api_format == "openai":
            openai_response = self._translator.bedrock_to_openai(response, model)
            return openai_response.model_dump()
        elif api_format == "anthropic":
            anthropic_response = self._translator.bedrock_to_anthropic(response, model)
            return anthropic_response.model_dump()
        else:
            return response.model_dump()

    async def _client_for_request(self, decision: RoutingDecision, context: TokenContext) -> Any:
        # SDK retries can repeat a billed invocation after an ambiguous response.
        # A bounded request reserves exactly one attempt; retries must return to
        # gateway admission with a new server-owned spend ID.
        if context._policy_flow_target is not None:
            return await self._pool_service.get_client(decision.credentials, single_attempt=True)
        return await self._pool_service.get_client(decision.credentials)

    async def _invoke_bedrock(
        self,
        client: Any,
        model_id: str,
        request: BedrockInvokeRequest,
        target: BedrockTarget | None = None,
        pricing_capture: PricingCapture | None = None,
        context: TokenContext | None = None,
    ) -> BedrockInvokeResponse:
        """Invoke Bedrock model.

        Creates an OTEL span for X-Ray visibility of the Bedrock API call.

        Args:
            client: Bedrock client
            model_id: Model ID
            request: Bedrock request
            target: Issue #4744 — the routed destination, when this call was signed
                with a destination account's credentials. Used only to classify a
                failure: an ``AccessDeniedException`` from a *routed* call is almost
                always "that account has not enabled this model", which needs its own
                error code and its own remediation (§5.1, §5.2). None for a
                platform-account call, whose error handling is unchanged.

        Returns:
            Bedrock response
        """
        if pricing_capture is not None:
            pricing_capture.forwarded(client, model_id, stream=False)

        try:
            from src.shared.tracing import get_tracer

            tracer = get_tracer(__name__)
            with tracer.start_as_current_span(
                "bedrock.invoke_model",
                attributes={"bedrock.model_id": model_id},
            ):
                try:
                    request_body = json.dumps(request.model_dump(exclude_none=True))
                    if context is not None:
                        context._budget_provider_started = True
                    response = await client.invoke_model(
                        modelId=model_id,
                        body=request_body,
                        contentType="application/json",
                        accept="application/json",
                    )
                except Exception as exc:
                    if pricing_capture is not None:
                        pricing_capture.initial_rejection(exc, operation="InvokeModel")
                    raise
                response_body = json.loads(response["body"].read())
                if pricing_capture is not None:
                    pricing_capture.response(response_body, response)
                return BedrockInvokeResponse(**response_body)

        except Exception as e:
            # Issue #4744 (§5.1): discriminate the error classes BEFORE collapsing them
            # into a generic BedrockInvocationError. On a routed call, "your account has
            # not enabled this model" and "Bedrock is down" need different messages and
            # different remediations; the bare-Exception catch below made them
            # indistinguishable, which meant the feature's single most common failure
            # mode read as an outage.
            raise _classify_bedrock_failure(e, model_id=model_id, target=target) from e

    async def _invoke_bedrock_stream(
        self,
        client: Any,
        model_id: str,
        request: BedrockInvokeRequest,
        target: BedrockTarget | None = None,
        pricing_capture: PricingCapture | None = None,
        context: TokenContext | None = None,
    ) -> AsyncIterator[bytes]:
        """Invoke Bedrock model with streaming.

        Creates an OTEL span covering the full stream lifecycle (from API call
        to last chunk received) for X-Ray visibility.

        Args:
            client: Bedrock client
            model_id: Model ID
            request: Bedrock request
            target: Issue #4744 — the routed destination, for failure classification.
                See :meth:`_invoke_bedrock`. Streaming needs it for the same reason:
                a model-not-enabled ``AccessDeniedException`` arrives on the initial
                call, before any chunk, so it is fully classifiable here.

        Yields:
            Raw response chunks
        """
        from src.shared.tracing import get_tracer

        tracer = get_tracer(__name__)
        span_ctx = tracer.start_as_current_span(
            "bedrock.invoke_model_stream",
            attributes={"bedrock.model_id": model_id},
        )
        span = span_ctx.__enter__()

        if pricing_capture is not None:
            pricing_capture.forwarded(client, model_id, stream=True)

        event_stream = None
        try:
            try:
                request_body = json.dumps(request.model_dump(exclude_none=True))
                if context is not None:
                    context._budget_provider_started = True
                response = await client.invoke_model_with_response_stream(
                    modelId=model_id,
                    body=request_body,
                    contentType="application/json",
                    accept="application/json",
                )
            except Exception as exc:
                if pricing_capture is not None:
                    pricing_capture.initial_rejection(exc, operation="InvokeModelWithResponseStream")
                raise

            if pricing_capture is not None:
                pricing_capture.response({}, response)
            event_stream = response.get("body")
            if event_stream is not None:
                chunk_count = 0
                iterator = iter(event_stream)
                while True:
                    # Pull one event at a time off the event loop. This preserves
                    # backpressure and propagates SDK reader exceptions directly,
                    # without a background reader writing to an asyncio.Queue.
                    event = await asyncio.to_thread(next, iterator, None)
                    if event is None:
                        break
                    chunk = event.get("chunk", {}).get("bytes")
                    if not chunk:
                        continue
                    chunk_count += 1
                    if pricing_capture is not None:
                        pricing_capture.chunk(chunk)
                    yield chunk

                span.set_attribute("bedrock.stream_chunks", chunk_count)

        except Exception as e:
            span.record_exception(e)
            # Issue #4744 (§5.1): same discrimination as the non-streaming path.
            raise _classify_bedrock_failure(e, model_id=model_id, target=target) from e
        finally:
            # Closing the SDK body also releases a blocking read on disconnect.
            close = getattr(event_stream, "close", None)
            if close is not None:
                with suppress(Exception):
                    close()
            span_ctx.__exit__(None, None, None)

    async def _invoke_openai_response(
        self,
        bedrock_request: BedrockInvokeRequest,
        bedrock_model_id: str,
        model: str,
        context: TokenContext,
        pricing_capture: PricingCapture | None = None,
    ) -> OpenAIChatCompletionResponse:
        """Invoke Bedrock and return OpenAI format response.

        Args:
            bedrock_request: Bedrock request
            bedrock_model_id: Bedrock model ID
            model: Original model name
            context: Authentication context for usage logging

        Returns:
            OpenAI format response
        """
        start_time = time.time()
        tokens_in = 0
        tokens_out = 0
        cache_read: int | None = None
        cache_creation: int | None = None
        status_code = 200
        # Issue #4744: see `invoke_stream` — set before the try so the `finally`
        # settlement path always has a decision to record, even if resolution failed.
        decision = RoutingDecision()
        try:
            decision = await resolve_routing_decision(context)
            client = await self._client_for_request(decision, context)
            bedrock_response = await self._invoke_bedrock(
                client, bedrock_model_id, bedrock_request, decision.target, pricing_capture=pricing_capture, context=context
            )
            # Issue #1486: Read from response.usage dict (not top-level attrs)
            tokens_in = bedrock_response.usage.get("input_tokens", 0) or 0
            tokens_out = bedrock_response.usage.get("output_tokens", 0) or 0
            # Issue #4180: raw dict, no default — preserves unreported-vs-zero
            cache_read, cache_creation = self._cache_tokens_from_usage(bedrock_response.usage)
            return self._translator.bedrock_to_openai(bedrock_response, model)
        except Exception:
            status_code = 500
            raise
        finally:
            latency_ms = (time.time() - start_time) * 1000
            await self._log_usage(
                context=context,
                model=model,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cost_usd=0.0,
                latency_ms=int(latency_ms),
                status_code=status_code,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_creation,
                routing_decision=decision,
                pricing_capture=pricing_capture,
            )

    async def _stream_openai_response(
        self,
        bedrock_request: BedrockInvokeRequest,
        bedrock_model_id: str,
        model: str,
        context: TokenContext,
        pricing_capture: PricingCapture | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream OpenAI format response.

        Args:
            bedrock_request: Bedrock request
            bedrock_model_id: Bedrock model ID
            model: Original model name
            context: Authentication context for usage logging

        Yields:
            SSE formatted chunks
        """
        start_time = time.time()
        usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        status_code = 200
        # Issue #4744: see `invoke_stream` — set before the try so the `finally`
        # settlement path always has a decision to record, even if resolution failed.
        decision = RoutingDecision()
        try:
            decision = await resolve_routing_decision(context)
            client = await self._client_for_request(decision, context)
            response_id = str(uuid.uuid4())

            bedrock_stream = self._invoke_bedrock_stream(
                client, bedrock_model_id, bedrock_request, decision.target, pricing_capture=pricing_capture, context=context
            )

            async for chunk in self._stream_handler.create_sse_response(bedrock_stream, "openai", model, response_id):
                # Extract usage from streaming chunks for logging
                self._extract_usage_from_sse_chunk(chunk, usage)
                yield chunk
        except Exception:
            status_code = 500
            raise
        finally:
            latency_ms = (time.time() - start_time) * 1000
            await self._log_usage(
                context=context,
                model=model,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cost_usd=0.0,
                latency_ms=int(latency_ms),
                status_code=status_code,
                # Issue #4180: absent keys stay absent in the accumulator, so
                # .get() yields None for "provider never reported it".
                cache_read_input_tokens=usage.get("cache_read_input_tokens"),
                cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
                routing_decision=decision,
                pricing_capture=pricing_capture,
            )

    async def _invoke_anthropic_response(
        self,
        bedrock_request: BedrockInvokeRequest,
        bedrock_model_id: str,
        model: str,
        context: TokenContext,
        pricing_capture: PricingCapture | None = None,
    ) -> AnthropicMessagesResponse:
        """Invoke Bedrock and return Anthropic format response.

        Args:
            bedrock_request: Bedrock request
            bedrock_model_id: Bedrock model ID
            model: Original model name
            context: Authentication context for usage logging

        Returns:
            Anthropic format response
        """
        start_time = time.time()
        tokens_in = 0
        tokens_out = 0
        cache_read: int | None = None
        cache_creation: int | None = None
        status_code = 200
        # Issue #4744: see `invoke_stream` — set before the try so the `finally`
        # settlement path always has a decision to record, even if resolution failed.
        decision = RoutingDecision()
        try:
            decision = await resolve_routing_decision(context)
            client = await self._client_for_request(decision, context)
            bedrock_response = await self._invoke_bedrock(
                client, bedrock_model_id, bedrock_request, decision.target, pricing_capture=pricing_capture, context=context
            )
            # Issue #1486: Read from response.usage dict (not top-level attrs)
            tokens_in = bedrock_response.usage.get("input_tokens", 0) or 0
            tokens_out = bedrock_response.usage.get("output_tokens", 0) or 0
            # Issue #4180: raw dict, no default — preserves unreported-vs-zero
            cache_read, cache_creation = self._cache_tokens_from_usage(bedrock_response.usage)
            return self._translator.bedrock_to_anthropic(bedrock_response, model)
        except Exception:
            status_code = 500
            raise
        finally:
            latency_ms = (time.time() - start_time) * 1000
            await self._log_usage(
                context=context,
                model=model,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cost_usd=0.0,
                latency_ms=int(latency_ms),
                status_code=status_code,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_creation,
                routing_decision=decision,
                pricing_capture=pricing_capture,
            )

    async def _stream_anthropic_response(
        self,
        bedrock_request: BedrockInvokeRequest,
        bedrock_model_id: str,
        model: str,
        context: TokenContext,
        pricing_capture: PricingCapture | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream Anthropic format response.

        Args:
            bedrock_request: Bedrock request
            bedrock_model_id: Bedrock model ID
            model: Original model name
            context: Authentication context for usage logging

        Yields:
            SSE formatted chunks
        """
        start_time = time.time()
        usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        status_code = 200
        # Issue #4744: see `invoke_stream` — set before the try so the `finally`
        # settlement path always has a decision to record, even if resolution failed.
        decision = RoutingDecision()
        try:
            decision = await resolve_routing_decision(context)
            client = await self._client_for_request(decision, context)
            response_id = str(uuid.uuid4())

            bedrock_stream = self._invoke_bedrock_stream(
                client, bedrock_model_id, bedrock_request, decision.target, pricing_capture=pricing_capture, context=context
            )

            async for chunk in self._stream_handler.create_sse_response(bedrock_stream, "anthropic", model, response_id):
                self._extract_usage_from_sse_chunk(chunk, usage)
                yield chunk
        except Exception:
            status_code = 500
            raise
        finally:
            latency_ms = (time.time() - start_time) * 1000
            await self._log_usage(
                context=context,
                model=model,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cost_usd=0.0,
                latency_ms=int(latency_ms),
                status_code=status_code,
                # Issue #4180: absent keys stay absent in the accumulator, so
                # .get() yields None for "provider never reported it".
                cache_read_input_tokens=usage.get("cache_read_input_tokens"),
                cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
                routing_decision=decision,
                pricing_capture=pricing_capture,
            )

    async def _invoke_bedrock_response(
        self,
        bedrock_request: BedrockInvokeRequest,
        bedrock_model_id: str,
        context: TokenContext,
        pricing_capture: PricingCapture | None = None,
    ) -> dict[str, Any]:
        """Invoke Bedrock and return raw response.

        Args:
            bedrock_request: Bedrock request
            bedrock_model_id: Bedrock model ID
            context: Authentication context for usage logging

        Returns:
            Bedrock response dictionary
        """
        start_time = time.time()
        tokens_in = 0
        tokens_out = 0
        cache_read: int | None = None
        cache_creation: int | None = None
        status_code = 200
        # Issue #4744: see `invoke_stream` — set before the try so the `finally`
        # settlement path always has a decision to record, even if resolution failed.
        decision = RoutingDecision()
        try:
            decision = await resolve_routing_decision(context)
            client = await self._client_for_request(decision, context)
            bedrock_response = await self._invoke_bedrock(
                client, bedrock_model_id, bedrock_request, decision.target, pricing_capture=pricing_capture, context=context
            )
            # Issue #1486: Read from response.usage dict (not top-level attrs)
            tokens_in = bedrock_response.usage.get("input_tokens", 0) or 0
            tokens_out = bedrock_response.usage.get("output_tokens", 0) or 0
            # Issue #4180: raw dict, no default — preserves unreported-vs-zero
            cache_read, cache_creation = self._cache_tokens_from_usage(bedrock_response.usage)
            return bedrock_response.model_dump()
        except Exception:
            status_code = 500
            raise
        finally:
            latency_ms = (time.time() - start_time) * 1000
            await self._log_usage(
                context=context,
                model=bedrock_model_id,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cost_usd=0.0,
                latency_ms=int(latency_ms),
                status_code=status_code,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_creation,
                routing_decision=decision,
                pricing_capture=pricing_capture,
            )

    async def _stream_bedrock_response(
        self,
        bedrock_request: BedrockInvokeRequest,
        bedrock_model_id: str,
        context: TokenContext,
        pricing_capture: PricingCapture | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream Bedrock format response.

        Args:
            bedrock_request: Bedrock request
            bedrock_model_id: Bedrock model ID
            context: Authentication context for usage logging

        Yields:
            SSE formatted chunks
        """
        start_time = time.time()
        usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        status_code = 200
        # Issue #4744: see `invoke_stream` — set before the try so the `finally`
        # settlement path always has a decision to record, even if resolution failed.
        decision = RoutingDecision()
        try:
            decision = await resolve_routing_decision(context)
            client = await self._client_for_request(decision, context)
            response_id = str(uuid.uuid4())

            bedrock_stream = self._invoke_bedrock_stream(
                client, bedrock_model_id, bedrock_request, decision.target, pricing_capture=pricing_capture, context=context
            )

            has_chunks = False
            async with aclosing(self._stream_handler.create_sse_response(bedrock_stream, "bedrock", bedrock_model_id, response_id)) as stream:
                async for chunk in stream:
                    has_chunks = True
                    self._extract_usage_from_sse_chunk(chunk, usage)
                    yield chunk
            if not has_chunks:
                raise BedrockInvocationError("Bedrock returned an empty stream")
        except (asyncio.CancelledError, GeneratorExit):
            status_code = 499
            raise
        except Exception as error:
            status_code = getattr(error, "status_code", 500)
            raise
        finally:
            latency_ms = (time.time() - start_time) * 1000
            await self._log_usage(
                context=context,
                model=bedrock_model_id,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cost_usd=0.0,
                latency_ms=int(latency_ms),
                status_code=status_code,
                # Issue #4180: absent keys stay absent in the accumulator, so
                # .get() yields None for "provider never reported it".
                cache_read_input_tokens=usage.get("cache_read_input_tokens"),
                cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
                routing_decision=decision,
                pricing_capture=pricing_capture,
            )

    # =========================================================================
    # Streaming Usage Extraction
    # =========================================================================

    def _extract_usage_from_sse_chunk(self, chunk: bytes, usage: dict[str, int]) -> None:
        """Extract token usage from an SSE chunk and accumulate into usage dict.

        Parses the SSE data payload looking for Anthropic/Bedrock usage fields
        (message_start → input_tokens + cache tokens, message_delta → output_tokens).
        Failures are silently ignored to avoid disrupting the stream.

        Issue #1486: Also captures cache_read_input_tokens and
        cache_creation_input_tokens from the message_start event.

        Args:
            chunk: Raw SSE bytes (e.g. b'event: ...\\ndata: {...}\\n\\n')
            usage: Mutable dict to accumulate token counts
        """
        try:
            chunk_str = chunk.decode("utf-8", errors="ignore")
            # Find JSON data payload in SSE chunk
            for line in chunk_str.split("\n"):
                if line.startswith("data: ") and line[6:7] == "{":
                    data = json.loads(line[6:])
                    # message_start carries input_tokens + cache tokens
                    if data.get("type") == "message_start":
                        msg_usage = data.get("message", {}).get("usage", {})
                        if msg_usage.get("input_tokens"):
                            usage["input_tokens"] = msg_usage["input_tokens"]
                        # Issue #1486: Capture cache token counts
                        # Issue #4180: `is not None`, not truthiness. A provider
                        # that explicitly reports 0 cache tokens is asserting
                        # "no cache activity"; truthiness would discard that and
                        # the row would read NULL = "unknown" instead.
                        if msg_usage.get("cache_read_input_tokens") is not None:
                            usage["cache_read_input_tokens"] = msg_usage["cache_read_input_tokens"]
                        if msg_usage.get("cache_creation_input_tokens") is not None:
                            usage["cache_creation_input_tokens"] = msg_usage["cache_creation_input_tokens"]
                    # message_delta carries output_tokens
                    elif data.get("type") == "message_delta":
                        delta_usage = data.get("usage", {})
                        if delta_usage.get("output_tokens"):
                            usage["output_tokens"] = delta_usage["output_tokens"]
        except Exception:
            pass  # Never disrupt the stream for usage extraction

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def get_available_models(self, context: TokenContext) -> list[dict[str, Any]]:
        """Get list of available models for the given context.

        Args:
            context: Authentication context

        Returns:
            List of model information

        Note:
            Issue #4744 (#4692 · R3), design note §5.2 requirement 4 — **knowingly
            unaddressed here, recorded so it is not rediscovered as a bug.** This list
            comes from the caller's allowed-model configuration, which is
            account-independent. Once a principal is routed, the models that actually
            work are those enabled in the *destination* account, so this list can
            legitimately advertise a model whose invoke then fails
            ``model_not_enabled``. Closing the gap means a per-destination model-access
            read (a cross-account ``bedrock:ListFoundationModels``, cached), which is
            new AWS calls on a new permission and out of this issue's scope. The
            §2.6 error names the account and the fix, so the failure is diagnosable in
            the meantime. Follow-up: filter this list by destination.
        """
        return self._model_resolver.get_available_models(context)

    @property
    def model_resolver(self) -> ModelResolver:
        """Get the model resolver instance."""
        return self._model_resolver

    @property
    def format_translator(self) -> FormatTranslator:
        """Get the format translator instance."""
        return self._translator

    @property
    def stream_handler(self) -> StreamHandler:
        """Get the stream handler instance."""
        return self._stream_handler
