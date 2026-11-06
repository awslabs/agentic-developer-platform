"""bedrock-mantle passthrough service for OpenAI Responses-API traffic (Issue #2709).

Proxies ``POST /openai/v1/responses`` to the upstream ``bedrock-mantle`` endpoint
so Codex (and any future OpenAI-model client) rides the same per-tenant metering
and model-allowlist governance that Claude traffic gets today.

Successful response bytes pass through unchanged. If an upstream stream fails
before its terminal event, an SSE error is appended when at a record boundary;
an interrupted partial record is closed without injecting more malformed data.
It intentionally does NOT touch ``format_translator.py``.

The one exception is the ``model`` field: bedrock-runtime's OpenAI path serves
models only via cross-region inference profiles, so the forwarded body's model
id is prefixed with the configured geo prefix (``openai.gpt-6-astra`` →
``us.openai.gpt-6-astra``) before signing. The caller's bare id is preserved for
metering/pricing. See ``_apply_inference_profile``.

Governance handled here:
- **Metering**: the Responses-API ``usage`` block is extracted and written to
  ``usage_logs`` via the same ``UsageService`` the Bedrock proxy uses.
- **Auth**: upstream auth is produced by a ``MantleAuth`` strategy; the resulting
  Authorization header is NEVER logged.
- **Error mapping**: upstream 4xx/5xx status + body are passed back to the caller
  unchanged; the gateway request-id rides along in a response header.

The GPT-5.5 model-card quirk is honored: mantle serves the Responses API on the
``/openai/v1/responses`` path (distinct from ``/v1/responses`` used by other
mantle models).
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

import anyio
import boto3
import httpx

from pricing_policy import RoutingEvidence, normalize_billing_model_id
from pricing_policy.policy import MissingUsageError, geography_from_model_prefix
from src.budget.enforcement_service import reconcile_budget_reservation
from src.budget.metering_admission import (
    MeteringAccountingUnresolvedError,
    MeteringAdmission,
    MeteringAdmissionError,
    MeteringBudgetExceededError,
    MeteringDispatchPermit,
    PreparedMeteringAttempt,
)
from src.budget.pricing_decisions import price_completed_usage
from src.chat_logging.service import ChatLoggingService
from src.proxy.bedrock_enforcement import RoutingDecision, resolve_routing_decision
from src.proxy.conversation_identity import _current_conversation
from src.proxy.mantle_auth import MantleAuth, SigV4MantleAuth
from src.proxy.service import _current_client_tool
from src.proxy.stream_handler import SSEBoundary
from src.shared.accounting_health import METERING_UNCONFIRMED, METERING_WRITE_FAILURES, record_health_event
from src.shared.database import get_accounting_session_factory as get_session_factory
from src.shared.exceptions import BedrockGatewayError
from src.shared.metrics import emit_stream_outcome
from src.shared.schemas.auth import TokenContext
from src.usage.metering_journal import MeteringObjectReceipt
from src.usage.persistence import persist_usage_with_retry, usage_recovery_record, usage_write_error_details
from src.usage.service import UsageService

if TYPE_CHECKING:  # Imported lazily at call time to keep the proxy import graph acyclic.
    from src.orchestration.provider_quotes import TrustedUsage

logger = logging.getLogger(__name__)

# Retain settlement tasks after a streaming client disconnects. Cancellation of
# the HTTP response must not discard measured usage or strand budget holds.
_usage_finalizers: set[asyncio.Task[bool]] = set()


def _usage_finalized(task: asyncio.Task[bool]) -> None:
    _usage_finalizers.discard(task)
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("Gateway usage finalization failed: %s", task.get_name(), exc_info=(type(error), error, error.__traceback__))


def register_usage_finalizer(task: asyncio.Task[bool]) -> None:
    _usage_finalizers.add(task)
    task.add_done_callback(_usage_finalized)


async def drain_usage_finalizers(timeout_seconds: float = 30.0) -> int:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while pending := {task for task in _usage_finalizers if not task.done()}:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            logger.error("Gateway shutdown left %d usage settlements pending", len(pending))
            return len(pending)
        await asyncio.wait(pending, timeout=remaining)
    return 0


# GPT-5.5 quirk: mantle serves the Responses API on this path.
MANTLE_RESPONSES_PATH = "/openai/v1/responses"

# Model-family dimension recorded on usage_logs.model for OpenAI-model traffic
# so metering can distinguish it from Claude/Bedrock rows.
USAGE_MODEL_FAMILY = "openai"


@dataclass(frozen=True)
class RoutedMantleRequest:
    """Per-request signing state; never mutate the shared service's signer."""

    auth: MantleAuth = field(repr=False)
    base_url: str
    inference_profile_prefix: str
    decision: RoutingDecision = field(repr=False)

    @property
    def upstream_url(self) -> str:
        return self.base_url + MANTLE_RESPONSES_PATH


class MeteredResponseTransport(Protocol):
    async def dispatch_once(
        self,
        client: httpx.AsyncClient,
        request: httpx.Request,
        permit: MeteringDispatchPermit,
        *,
        model: str,
        stream: bool,
    ) -> httpx.Response:
        """#7033: consume after pool/TLS/credential waits at physical submission.

        Return a response or a typed proven-unsent/ambiguous failure. Never use
        an awaited HTTPX send as the final guard: no await or queue may occur
        between permit.consume and submission of the bound request bytes.
        """
        ...


# Bedrock cross-region inference-profile geo prefixes. A model id already starting
# with one of these is a fully-qualified inference profile — the route must NOT
# prefix it again (that would produce e.g. "us.us.openai.*").
_GEO_PREFIXES = ("us.", "eu.", "apac.", "in.", "global.", "us-gov.")

# Safety valve for the per-stream partial-line buffer (issue #2828). A well-formed
# `response.completed` event is well under this; if a line ever exceeds it (e.g. a
# missing newline upstream), we drop the buffer with a WARN rather than grow it
# unboundedly — metering must never threaten gateway pod memory or the stream.
_MAX_SNIFF_BUFFER_BYTES = 1024 * 1024  # 1 MiB
_TERMINAL_EVENTS = {"response.completed", "response.incomplete", "response.failed", "error"}
_SSE_EVENT_END = re.compile(rb"(?:\r\n|\r(?!\n)|(?<!\r)\n){2}")


def _settlement_error(request_id: str | None) -> bytes:
    return json.dumps(
        {"error": "usage_settlement_unconfirmed", "message": "Model usage could not be confirmed.", "retryable": False, "request_id": request_id}
    ).encode()


@dataclass
class MantleResponse:
    """Result of a non-streaming mantle passthrough call."""

    status_code: int
    content: bytes
    media_type: str = "application/json"
    # Token usage extracted from the Responses-API `usage` block (best-effort).
    usage: dict[str, Any] = field(default_factory=dict)


class MantleUpstreamError(Exception):
    """Upstream mantle call failed before any stream bytes reached the client.

    Raised only from the streaming path's eager-connect phase, where the
    route can still map it to a real HTTP status. Without this, a
    ``StreamingResponse`` has already sent its 200 header by the time the
    upstream failure surfaces, so the client sees a 200 whose stream dies
    instantly and the gateway logs nothing (the silent-failure mode behind
    the #3897 Codex outage).

    Attributes:
        status_code: Upstream HTTP status (502 for connect/transport errors).
        content: Upstream error body (or synthesized JSON for transport errors).
    """

    def __init__(self, status_code: int, content: bytes, media_type: str = "application/json") -> None:
        super().__init__(f"mantle upstream error: HTTP {status_code}")
        self.status_code = status_code
        self.content = content
        self.media_type = media_type


class _CapturedUsage(dict):
    """Usage remains a mapping; routing evidence is server metadata, not tokens."""

    def __init__(self, usage, routing, provider_request_id=None):
        super().__init__(usage)
        self.routing = routing
        self.provider_request_id = provider_request_id


class _StreamUsageSniffer:
    """Stateful, per-stream sniffer for the Responses-API ``usage`` block (#2828).

    The terminal ``response.completed`` SSE event embeds the full accumulated
    response object, so for any non-trivial output its ``data:`` line is larger
    than one TCP chunk and arrives split across ``aiter_bytes`` chunks. Parsing
    each chunk independently never sees a complete line, so usage was lost.

    This sniffer buffers the trailing partial line **at the byte level** (so a
    split mid-UTF-8 sequence reassembles correctly) and only parses ``data:``
    lines once a newline completes them. It NEVER touches the bytes yielded to
    the client — buffering is for sniffing only. Failures are swallowed;
    metering must never disrupt the passthrough.
    """

    def __init__(self) -> None:
        self.usage: dict[str, Any] = {}
        self._buffer = b""
        self.metadata: dict[str, str] = {}
        self.terminal_event: str | None = None
        self.sequence_number = -1
        self._event_name = ""
        self._event_terminal: str | None = None
        self._oversized_data = False
        self._dropping_line = False
        self._trailing_cr = False

    def finish(self) -> None:
        if self._buffer:
            self._parse_line(self._buffer)
            self._buffer = b""

    def feed(self, chunk: bytes) -> None:
        """Consume one upstream chunk, updating ``usage`` from any complete lines."""
        if not chunk:
            return
        # SSE permits LF, CRLF and CR, including CRLF split across reads.
        if self._trailing_cr and chunk.startswith(b"\n"):
            chunk = chunk[1:]
        self._trailing_cr = chunk.endswith(b"\r")
        chunk = chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        # Limit each line, not an entire HTTP chunk: one chunk can contain many
        # events. After an oversized line, discard until its newline rather than
        # trying to parse its tail as the start of a new event.
        parts = chunk.split(b"\n")
        for index, part in enumerate(parts):
            complete = index < len(parts) - 1
            if not self._dropping_line:
                self._buffer += part
                if len(self._buffer) > _MAX_SNIFF_BUFFER_BYTES:
                    self._oversized_data |= self._buffer.startswith(b"data:")
                    logger.warning("mantle usage sniffer buffer exceeded cap; dropping partial line")
                    self._buffer = b""
                    self._dropping_line = True
                elif complete:
                    self._parse_line(self._buffer)
                    self._buffer = b""
            if complete:
                self._dropping_line = False

    def _parse_line(self, raw: bytes) -> None:
        try:
            # Only an empty line dispatches an SSE event. Whitespace or an
            # invalid UTF-8 byte must not manufacture a completion delimiter.
            line = raw.decode("utf-8", errors="replace")
            if not line:
                # A terminal data line is not delivered as an SSE event until
                # the blank-line delimiter arrives. EOF mid-record is failure.
                terminal = self._event_terminal
                if self._oversized_data and self._event_name in _TERMINAL_EVENTS:
                    terminal = self._event_name
                if terminal:
                    self.terminal_event = terminal
                self._event_name = ""
                self._event_terminal = None
                self._oversized_data = False
                return
            if line.startswith("event:"):
                self._event_name = line[6:].strip()
                return
            if not line.startswith("data:"):
                return
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]" or not payload.startswith("{"):
                return
            data = json.loads(payload)
            if not isinstance(data, dict):
                return
            event_type = data.get("type", self._event_name)
            if isinstance(event_type, str) and event_type in _TERMINAL_EVENTS:
                self._event_terminal = event_type
            sequence = data.get("sequence_number")
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                self.sequence_number = max(self.sequence_number, sequence)
            # usage may be top-level or nested under a "response" object.
            found = data.get("usage")
            if found is None and isinstance(data.get("response"), dict):
                found = data["response"].get("usage")
            if data.get("type") in (None, "response.completed", "response.incomplete", "response.failed"):
                response = data.get("response", data)
                if isinstance(response, dict):
                    self.metadata.update(MantlePassthroughService._response_metadata(response))
            parsed = MantlePassthroughService._usage_from_dict(found)
            if parsed:
                self.usage.update(parsed)
        except (json.JSONDecodeError, ValueError, UnicodeError):
            pass  # never disrupt the stream for usage extraction


class MantlePassthroughService:
    """Forwards Responses-API requests to bedrock-mantle and meters usage.

    Args:
        auth: Strategy that produces the upstream Authorization header.
        base_url: Mantle base URL WITHOUT the responses path (e.g.
            ``https://bedrock-mantle.us-east-1.api.aws``).
        http_client: Optional pre-built httpx.AsyncClient (injected in tests).
        timeout: Non-streaming request / streaming write timeout in seconds.
        stream_read_timeout: Maximum upstream silence during a stream. Connect
            and pool acquisition have separate ten-second deadlines.
    """

    def __init__(
        self,
        auth: MantleAuth,
        base_url: str,
        *,
        inference_profile_prefix: str = "",
        on_demand_models: str = "",
        http_client: httpx.AsyncClient | None = None,
        operation_timeout: float | None = None,
        timeout: float = 120.0,
        stream_read_timeout: float = 600.0,
        stream_finalization_timeout: float = 3.0,
        metering: MeteringAdmission | None = None,
        metered_transport: MeteredResponseTransport | None = None,
    ) -> None:
        self._auth = auth
        self._base_url = base_url.rstrip("/")
        self._inference_profile_prefix = inference_profile_prefix.strip().rstrip(".")
        # Glob patterns of models invoked on-demand with the BARE id (no inference
        # profile exists for them) — these must NOT be geo-prefixed.
        self._on_demand_patterns = [p.strip() for p in on_demand_models.split(",") if p.strip()]
        self._timeout = timeout
        self._stream_timeout = httpx.Timeout(timeout, read=stream_read_timeout, connect=10.0, pool=10.0)
        self._operation_timeout = operation_timeout
        self._stream_finalization_timeout = stream_finalization_timeout
        self._http_client = http_client
        self._metering = metering
        self._metered_transport = metered_transport
        self._chat_logger: ChatLoggingService | None = None

    @property
    def upstream_url(self) -> str:
        return f"{self._base_url}{MANTLE_RESPONSES_PATH}"

    def _apply_inference_profile(self, body: bytes, *, prefix: str | None = None) -> bytes:
        """Rewrite the forwarded body's ``model`` to its inference-profile id.

        bedrock-runtime's OpenAI path serves models ONLY via cross-region
        inference profiles (bare ``openai.gpt-6-astra`` → 400 "on-demand
        throughput isn't supported"), so the byte-for-byte passthrough would
        fail for every caller using a bare id. To keep the caller-facing id
        stable — Codex and the setup UI keep emitting ``openai.gpt-6-astra`` —
        we prefix ONLY the forwarded body's model with the configured geo prefix
        (``openai.gpt-6-astra`` → ``us.openai.gpt-6-astra``). The bare id the
        caller sent is still what the route passes to metering/pricing, so this
        rewrite is invisible to usage_logs, the allowlist, and cost lookup.

        Returns the body unchanged when: no prefix is configured, the body is not
        JSON/​is empty, there is no string ``model``, the model is already a
        fully-qualified inference profile (already carries a geo prefix), or the
        model is an on-demand model (matches an ``on_demand_models`` glob) — those
        are invoked with the bare id and have no inference profile, so prefixing
        them would produce an invalid id. This is the one point where the
        passthrough re-serializes the body; the signature is computed over these
        returned bytes, so callers sign what they forward.
        """
        prefix = self._inference_profile_prefix if prefix is None else prefix
        if not prefix:
            return body
        try:
            data = json.loads(body) if body else None
        except (json.JSONDecodeError, ValueError):
            return body
        if not isinstance(data, dict):
            return body
        model = data.get("model")
        if not isinstance(model, str) or not model:
            return body
        if model.startswith(_GEO_PREFIXES):
            return body
        if any(fnmatch.fnmatch(model, pat) for pat in self._on_demand_patterns):
            return body
        data["model"] = f"{prefix}.{model}"
        return json.dumps(data).encode()

    def _headers(self, body: bytes, routed: RoutedMantleRequest | None = None) -> dict[str, str]:
        """Build outbound headers, SigV4-signing the exact body bytes.

        The signature is computed over ``body`` and the upstream URL, so callers
        MUST send these same bytes unchanged (any re-serialization breaks the
        signature). Signed auth headers (Authorization, X-Amz-Date,
        X-Amz-Security-Token) are sensitive — never log them.
        """
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        auth = routed.auth if routed else self._auth
        url = routed.upstream_url if routed else self.upstream_url
        headers.update(auth.sign("POST", url, body))
        return headers

    def _client(self) -> httpx.AsyncClient:
        return self._http_client or httpx.AsyncClient(timeout=self._timeout)

    def _routed_request(self, decision: RoutingDecision) -> RoutedMantleRequest:
        credentials = decision.credentials
        if credentials is None:
            return RoutedMantleRequest(self._auth, self._base_url, self._inference_profile_prefix, decision)
        region = credentials.region
        parsed = urlparse(self._base_url)
        match = re.fullmatch(r"(bedrock-runtime|bedrock-mantle)\.[a-z0-9-]+\.(amazonaws\.com(?:\.cn)?|api\.aws)", parsed.hostname or "")
        if parsed.scheme != "https" or match is None or parsed.username or parsed.password:
            raise BedrockGatewayError(
                "bedrock_routing_endpoint_unavailable", "The configured OpenAI endpoint does not support AWS account routing.", 503
            )
        suffix = "api.aws" if match.group(1) == "bedrock-mantle" else "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
        base_url = parsed._replace(netloc=f"{match.group(1)}.{region}.{suffix}").geturl()
        auth = SigV4MantleAuth(region, session=boto3.Session(region_name=region, **credentials.as_boto3_kwargs()))
        # Bare model IDs use the destination geography. Explicit model/profile
        # IDs are preserved by _apply_inference_profile and validated upstream.
        prefix = self._inference_profile_prefix
        if prefix and prefix != "global":
            if region.startswith("us-gov-"):
                prefix = "us-gov"
            else:
                prefix = {"us": "us", "eu": "eu", "ap": "apac"}.get(region.split("-")[0], prefix)
        return RoutedMantleRequest(auth, base_url, prefix, decision)

    async def create_response(
        self,
        body: bytes,
        context: TokenContext,
        *,
        stream: bool,
        model: str,
        request_id: str | None = None,
        agent_run_id: str | None = None,
    ) -> MantleResponse | AsyncIterator[bytes]:
        """Proxy a Responses call with an absolute deadline from request entry."""
        operation_deadline = asyncio.get_running_loop().time() + self._operation_timeout if self._operation_timeout is not None else float("inf")

        async def create() -> MantleResponse | AsyncIterator[bytes]:
            return await self._create_response(
                body,
                context,
                stream=stream,
                model=model,
                request_id=request_id,
                agent_run_id=agent_run_id,
                operation_deadline=operation_deadline,
            )

        if self._operation_timeout is None:
            return await create()
        task = asyncio.create_task(create())
        try:
            completed, _ = await asyncio.wait({task}, timeout=max(0, operation_deadline - asyncio.get_running_loop().time()))
            if completed:
                return task.result()
            task.cancel()
            register_usage_finalizer(task)
            raise MantleUpstreamError(504, b'{"error":"upstream_operation_timeout","retryable":false}')
        except asyncio.CancelledError:
            task.cancel()
            register_usage_finalizer(task)
            raise

    async def _create_response(
        self,
        body: bytes,
        context: TokenContext,
        *,
        stream: bool,
        model: str,
        request_id: str | None = None,
        agent_run_id: str | None = None,
        operation_deadline: float,
    ) -> MantleResponse | AsyncIterator[bytes]:
        """Proxy a Responses-API request to mantle.

        Args:
            body: Raw request body, forwarded byte-for-byte.
            context: Tenant auth context for metering.
            stream: Whether the client requested a streaming response.
            model: The requested model id (for usage_logs).
            request_id: Gateway request id (for usage_logs correlation).
            agent_run_id: Optional agent run id (per-run cost attribution).

        Returns:
            A ``MantleResponse`` for non-streaming calls, or an async byte
            iterator that yields upstream chunks verbatim for streaming calls.

        Structured service lifecycle logs join usage by request_id. Their start
        excludes auth and budget middleware; the request-end log reports full
        gateway latency. Upstream durations include local transport, network
        and client backpressure, not just provider compute. Stream terminal
        timing starts only after a complete SSE terminal record; it cannot be
        included in a response-start timing header.
        """
        # Forward the profile-qualified body (bedrock-runtime needs it); keep the
        # caller's bare `model` for metering/pricing/logging (passed through below).
        service_start = time.monotonic()
        request_id = request_id or str(uuid4())
        decision = await resolve_routing_decision(context, agent_run_id=agent_run_id)
        routed = self._routed_request(decision)
        upstream_body = self._apply_inference_profile(body, prefix=routed.inference_profile_prefix)
        attempt = None
        if self._metering is not None:
            if self._metered_transport is None:
                raise MantleUpstreamError(503, b'{"error":"metering_transport_unavailable","retryable":false}')
            try:
                attempt = await self._metering.prepare(
                    context=context,
                    request_id=request_id,
                    model=model,
                    original=body,
                    submitted=upstream_body,
                    deadline=min(asyncio.get_running_loop().time() + 5, operation_deadline),
                    completion_timeout_seconds=(self._operation_timeout or 720) + self._stream_finalization_timeout + 5,
                )
            except MeteringBudgetExceededError as exc:
                raise MantleUpstreamError(402, b'{"error":"budget_exceeded","retryable":false}') from exc
            except MeteringAccountingUnresolvedError as exc:
                raise MantleUpstreamError(503, b'{"error":"accounting_unresolved","retryable":false}') from exc
            except Exception as exc:
                logger.warning("Responses metering admission unavailable", extra={"request_id": request_id, "error_type": type(exc).__name__})
                raise MantleUpstreamError(503, b'{"error":"metering_admission_unavailable","retryable":false}') from exc
        if stream:
            try:
                return await self._stream(
                    upstream_body,
                    context,
                    model=model,
                    request_id=request_id,
                    agent_run_id=agent_run_id,
                    routed=routed,
                    attempt=attempt,
                    service_start=service_start,
                    operation_deadline=operation_deadline,
                )
            except BaseException as exc:
                logger.warning(
                    "mantle stream setup lifecycle",
                    extra={
                        "request_id": request_id,
                        "status_code": 499 if isinstance(exc, asyncio.CancelledError) else getattr(exc, "status_code", 502),
                        "stream_outcome": "setup_failed",
                        "terminal_event": None,
                        "service_before_dispatch_ms": None,
                        "upstream_headers_ms": None,
                        "upstream_terminal_ms": None,
                        "service_after_upstream_ms": None,
                        "service_lifecycle_ms": round((time.monotonic() - service_start) * 1000, 1),
                    },
                )
                raise
        return await self._invoke(
            upstream_body,
            context,
            model=model,
            request_id=request_id,
            agent_run_id=agent_run_id,
            routed=routed,
            attempt=attempt,
            service_start=service_start,
        )

    async def _arm_metering(
        self,
        attempt: PreparedMeteringAttempt | None,
        request_id: str,
        *,
        body: bytes,
        model: str,
        destination: str,
    ) -> MeteringDispatchPermit | None:
        if attempt is None:
            return None
        try:
            return await self._metering.arm(attempt, submitted=body, model=model, destination=destination)
        except Exception as exc:
            try:
                await asyncio.wait_for(self._metering.cancel_unarmed(attempt), timeout=1)
            except Exception as cancel_error:
                logger.warning(
                    "Responses prepared exposure remains pending", extra={"request_id": request_id, "error_type": type(cancel_error).__name__}
                )
            logger.warning("Responses metering dispatch unavailable", extra={"request_id": request_id, "error_type": type(exc).__name__})
            raise MantleUpstreamError(503, b'{"error":"metering_dispatch_unavailable","retryable":false}') from exc

    async def _cancel_armed_unsent(self, attempt: PreparedMeteringAttempt | None, proof: object, request_id: str | None) -> None:
        if attempt is None:
            return
        try:
            with anyio.CancelScope(shield=True):
                await asyncio.wait_for(self._metering.cancel_armed_unsent(attempt, proof), timeout=5)
        except Exception as error:
            logger.warning("Responses armed exposure remains pending", extra={"request_id": request_id, "error_type": type(error).__name__})

    def _schedule_receipted_settlement(self, attempt: PreparedMeteringAttempt, receipt: MeteringObjectReceipt, request_id: str) -> None:
        async def settle() -> bool:
            confirmed = await self._metering.settle_receipt(attempt, receipt)
            if not confirmed:
                logger.info("Responses receipt already settled", extra={"request_id": request_id})
            return confirmed

        task = asyncio.create_task(settle(), name=f"mantle_receipted_settlement_{request_id}")
        register_usage_finalizer(task)

    async def _write_final_metering(
        self,
        attempt: PreparedMeteringAttempt | None,
        usage: dict[str, Any],
        provider_request_id: str | None,
        request_id: str,
        *,
        deadline: float | None = None,
        on_receipt: Callable[[MeteringObjectReceipt], None] | None = None,
        routing: RoutingEvidence | None = None,
        latency_ms: int = 0,
    ) -> bool:
        if attempt is None:
            return True

        async def persist() -> bool:
            trusted = await self._trusted_usage(usage)
            if not trusted.known or routing is None:
                raise MeteringAdmissionError("trusted routing and usage are required for final settlement")
            decision = self._metering.price_final(
                pricing_basis=attempt.pricing_basis,
                usage=usage,
                routing=routing,
                request_id=attempt.receipt.request_id,
                tenant_id=attempt.receipt.tenant_id,
                bound=attempt.quote.maximum_cost_usd,
            )
            receipt = await self._metering.write_final(
                attempt=attempt,
                usage=usage,
                trusted=trusted,
                provider_request_id=provider_request_id,
                decision=decision,
                latency_ms=latency_ms,
            )
            if on_receipt is not None:
                on_receipt(receipt)
            return True

        async def persist_or_mark_unknown() -> bool:
            try:
                return await persist()
            except Exception:
                try:
                    remaining = 1 if deadline is None else max(0, deadline - asyncio.get_running_loop().time())
                    await asyncio.wait_for(self._metering.mark_unknown(attempt), timeout=remaining)
                except Exception as mark_error:
                    logger.warning(
                        "Responses unknown containment unavailable",
                        extra={"request_id": request_id, "error_type": type(mark_error).__name__},
                    )
                raise

        try:
            if deadline is None:
                return await persist_or_mark_unknown()
            task = asyncio.create_task(persist_or_mark_unknown(), name=f"mantle_final_receipt_{request_id}")
            register_usage_finalizer(task)
            return await asyncio.wait_for(asyncio.shield(task), timeout=max(0, deadline - asyncio.get_running_loop().time()))
        except Exception as exc:
            logger.warning("Responses final usage receipt unavailable", extra={"request_id": request_id, "error_type": type(exc).__name__})
            return False

    async def _invoke(
        self,
        body: bytes,
        context: TokenContext,
        *,
        model: str,
        request_id: str | None,
        agent_run_id: str | None,
        routed: RoutedMantleRequest,
        attempt: PreparedMeteringAttempt | None = None,
        service_start: float,
    ) -> MantleResponse:
        start = time.monotonic()
        upstream_start: float | None = None
        upstream_end: float | None = None
        status_code = 502
        explicit_server_failure = False
        usage: dict[str, Any] = {}
        metadata: dict[str, str] = {}
        failure: BaseException | None = None
        from src.proxy.metered_transport import MeteredProvenUnsentError, MeteredUnsentCancelled

        try:
            headers = self._headers(body, routed)
            client = self._client()
            try:
                from src.agentauth.native_test_model import begin_native_model_dispatch

                await begin_native_model_dispatch(
                    context,
                    body=body,
                    model=model,
                    route=MANTLE_RESPONSES_PATH,
                    request_id=request_id,
                    client=client,
                )
                if context._native_test_handoff is not None:
                    context._native_test_handoff.before_submit()
                permit = await self._arm_metering(attempt, request_id, body=body, model=model, destination=routed.upstream_url)
                context._budget_provider_started = True
                upstream_start = time.monotonic()
                if permit is None:
                    resp = await client.post(routed.upstream_url, content=body, headers=headers)
                else:
                    upstream_request = client.build_request("POST", routed.upstream_url, content=body, headers=headers)
                    resp = await self._metered_transport.dispatch_once(client, upstream_request, permit, model=model, stream=False)
                status_code = resp.status_code
                explicit_server_failure = 500 <= status_code < 600
                metadata["provider_request_id"] = resp.headers.get("x-amzn-requestid") or resp.headers.get("x-request-id")
                content = resp.content
                upstream_end = time.monotonic()
                # Only extract usage on success bodies; upstream errors pass through untouched.
                if 200 <= status_code < 300:
                    usage = self._extract_usage(content)
                    metadata.update(self._response_metadata_from_bytes(content))
                result = MantleResponse(
                    status_code=status_code,
                    content=content,
                    media_type=resp.headers.get("content-type", "application/json"),
                    usage=usage,
                )
            except MeteredUnsentCancelled as exc:
                await self._cancel_armed_unsent(attempt, exc, request_id)
                raise
            except MeteredProvenUnsentError as exc:
                await self._cancel_armed_unsent(attempt, exc, request_id)
                raise MantleUpstreamError(503, b'{"error":"metering_dispatch_unavailable","retryable":false}') from exc
            except MeteringAdmissionError as exc:
                raise MantleUpstreamError(503, b'{"error":"metering_dispatch_unavailable","retryable":false}') from exc
            finally:
                if self._http_client is None:
                    if attempt is None:
                        await client.aclose()
                    else:

                        async def close_owned_client() -> bool:
                            await client.aclose()
                            return True

                        close_task = asyncio.create_task(close_owned_client(), name=f"mantle_owned_client_close_{request_id}")
                        register_usage_finalizer(close_task)
                latency_ms = (time.monotonic() - start) * 1000
                receipted = True
                if 200 <= status_code < 300 and attempt is not None:
                    captured = self._capture_usage(usage, body, model, metadata, base_url=routed.base_url)
                    receipted = await self._write_final_metering(
                        attempt,
                        usage,
                        metadata.get("provider_request_id"),
                        request_id,
                        deadline=asyncio.get_running_loop().time() + self._stream_finalization_timeout,
                        on_receipt=lambda receipt: self._schedule_receipted_settlement(attempt, receipt, request_id),
                        routing=captured.routing,
                        latency_ms=int(latency_ms),
                    )
                    settled = receipted
                elif attempt is not None:
                    settled = True
                elif upstream_end is None and asyncio.current_task().cancelling():
                    settled = False
                else:
                    try:
                        settled = await self._log_usage(
                            context,
                            model,
                            self._capture_usage(usage, body, model, metadata, base_url=routed.base_url),
                            int(latency_ms),
                            status_code,
                            request_id,
                            agent_run_id,
                            routing_decision=routed.decision,
                            **({"retain_failed_bound": True} if explicit_server_failure else {}),
                        )
                    except Exception:
                        logger.exception("Mantle usage settlement could not be confirmed", extra={"request_id": request_id})
                        settled = False
                if 200 <= status_code < 300 and not settled:
                    raise MantleUpstreamError(502, _settlement_error(request_id))
            return result
        except asyncio.CancelledError as exc:
            failure = exc
            if upstream_end is None and attempt is None:
                await self._log_usage(
                    context,
                    model,
                    {},
                    int((time.monotonic() - start) * 1000),
                    499,
                    request_id,
                    agent_run_id,
                    routing_decision=routed.decision,
                    retain_failed_bound=True,
                )
            raise
        except BaseException as exc:
            failure = exc
            raise
        finally:
            finished = time.monotonic()
            logger.info(
                "mantle response lifecycle",
                extra={
                    "request_id": request_id,
                    "status_code": (499 if isinstance(failure, asyncio.CancelledError) else getattr(failure, "status_code", status_code)),
                    "service_complete": failure is None,
                    "service_before_dispatch_ms": round((upstream_start - service_start) * 1000, 1) if upstream_start is not None else None,
                    "upstream_response_ms": round((upstream_end - upstream_start) * 1000, 1)
                    if upstream_end is not None and upstream_start is not None
                    else None,
                    "service_after_upstream_ms": round((finished - upstream_end) * 1000, 1) if upstream_end is not None else None,
                    "service_lifecycle_ms": round((finished - service_start) * 1000, 1),
                },
            )

    async def _stream(
        self,
        body: bytes,
        context: TokenContext,
        *,
        model: str,
        request_id: str | None,
        agent_run_id: str | None,
        routed: RoutedMantleRequest,
        attempt: PreparedMeteringAttempt | None = None,
        service_start: float,
        operation_deadline: float | None = None,
    ) -> AsyncIterator[bytes]:
        from src.proxy.metered_transport import MeteredProvenUnsentError, MeteredUnsentCancelled

        if operation_deadline is None:
            operation_deadline = asyncio.get_running_loop().time() + self._operation_timeout if self._operation_timeout is not None else float("inf")
        start = time.monotonic()
        upstream_start: float | None = None
        headers = self._headers(body, routed)
        client = self._client()
        owns_client = self._http_client is None
        pending_send: asyncio.Task[httpx.Response] | None = None
        resp: httpx.Response | None = None

        # Eager connect + status check BEFORE handing a generator to the route.
        # A StreamingResponse sends its 200 header before the generator's first
        # iteration, so any upstream failure surfaced from inside the generator
        # reaches the client as a 200 whose stream dies instantly — invisible in
        # gateway logs and indistinguishable (to the caller) from a broken
        # stream. Connecting here lets the route return the real upstream
        # status, and guarantees the failure is logged (#3897).
        try:
            upstream_request = client.build_request("POST", routed.upstream_url, content=body, headers=headers, timeout=self._stream_timeout)
            from src.agentauth.native_test_model import begin_native_model_dispatch

            await begin_native_model_dispatch(
                context,
                body=body,
                model=model,
                route=MANTLE_RESPONSES_PATH,
                request_id=request_id,
                client=client,
                stream=True,
            )
            if context._native_test_handoff is not None:
                context._native_test_handoff.before_submit()
            permit = await self._arm_metering(attempt, request_id, body=body, model=model, destination=routed.upstream_url)
            context._budget_provider_started = True
            upstream_start = time.monotonic()
            if permit is None:
                pending_send = asyncio.create_task(client.send(upstream_request, stream=True))
                resp = await asyncio.shield(pending_send)
                pending_send = None
            else:
                resp = await self._metered_transport.dispatch_once(client, upstream_request, permit, model=model, stream=True)
            upstream_headers = time.monotonic()
        except MeteredUnsentCancelled as exc:

            async def cancel_unsent(failure: MeteredUnsentCancelled = exc) -> bool:
                try:
                    if owns_client:
                        await client.aclose()
                finally:
                    await self._cancel_armed_unsent(attempt, failure, request_id)
                return True

            cleanup_task = asyncio.create_task(cancel_unsent(), name=f"mantle_unsent_cleanup_{request_id}")
            register_usage_finalizer(cleanup_task)
            with anyio.CancelScope(shield=True):
                await asyncio.wait({cleanup_task}, timeout=max(0, operation_deadline - asyncio.get_running_loop().time()))
            raise
        except asyncio.CancelledError:
            late_task = None
            if pending_send is not None:
                pending_send.cancel()

                async def close_late_response() -> bool:
                    try:
                        late_response = await pending_send
                    except (asyncio.CancelledError, Exception):
                        return False
                    await late_response.aclose()
                    return True

                late_task = asyncio.create_task(close_late_response(), name=f"mantle_late_headers_close_{request_id}")
            elif resp is not None:
                late_task = asyncio.create_task(resp.aclose(), name=f"mantle_cancelled_headers_close_{request_id}")
            if late_task is not None:
                register_usage_finalizer(late_task)

            async def finish_interrupted_setup() -> bool:
                try:
                    if owns_client:
                        if late_task is not None:
                            await asyncio.wait({late_task}, timeout=min(0.05, max(0, operation_deadline - asyncio.get_running_loop().time())))
                        await client.aclose()
                finally:
                    if attempt is None:
                        await self._log_usage(
                            context,
                            model,
                            {},
                            int((time.monotonic() - start) * 1000),
                            499,
                            request_id,
                            agent_run_id,
                            routing_decision=routed.decision,
                            retain_failed_bound=True,
                        )
                return True

            cleanup_task = asyncio.create_task(finish_interrupted_setup(), name=f"mantle_setup_cleanup_{request_id}")
            register_usage_finalizer(cleanup_task)
            with anyio.CancelScope(shield=True):
                await asyncio.wait({cleanup_task}, timeout=max(0, operation_deadline - asyncio.get_running_loop().time()))
            raise
        except MeteredProvenUnsentError as exc:
            if owns_client:
                await client.aclose()
            await self._cancel_armed_unsent(attempt, exc, request_id)
            raise MantleUpstreamError(503, b'{"error":"metering_dispatch_unavailable","retryable":false}') from exc
        except MeteringAdmissionError as exc:
            if owns_client:
                await client.aclose()
            raise MantleUpstreamError(503, b'{"error":"metering_dispatch_unavailable","retryable":false}') from exc
        except MantleUpstreamError:
            if owns_client:
                await client.aclose()
            raise
        except httpx.HTTPError as exc:
            if owns_client:
                await client.aclose()
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.error(
                "mantle stream connect failed: %s (model=%s request_id=%s latency_ms=%d)",
                exc,
                model,
                request_id,
                latency_ms,
            )
            if attempt is None:
                await self._log_usage(context, model, {}, latency_ms, 502, request_id, agent_run_id, routing_decision=routed.decision)
            raise MantleUpstreamError(
                502,
                json.dumps({"error": "mantle_upstream_unreachable", "message": str(exc)}).encode(),
            ) from exc

        if not (200 <= resp.status_code < 300):
            status_code = resp.status_code
            error_body = await resp.aread()
            await resp.aclose()
            if owns_client:
                await client.aclose()
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.error(
                "mantle stream upstream error: HTTP %d (model=%s request_id=%s latency_ms=%d body=%s)",
                status_code,
                model,
                request_id,
                latency_ms,
                error_body[:512].decode("utf-8", errors="replace"),
            )
            metadata = {"provider_request_id": resp.headers.get("x-amzn-requestid") or resp.headers.get("x-request-id")}
            if attempt is None:
                await self._log_usage(
                    context,
                    model,
                    self._capture_usage({}, body, model, metadata, base_url=routed.base_url),
                    latency_ms,
                    status_code,
                    request_id,
                    agent_run_id,
                    routing_decision=routed.decision,
                    **({"retain_failed_bound": True} if 500 <= status_code < 600 else {}),
                )
            raise MantleUpstreamError(
                status_code,
                error_body,
                resp.headers.get("content-type", "application/json"),
            )

        status_code = resp.status_code

        async def _passthrough() -> AsyncIterator[bytes]:
            sniffer = _StreamUsageSniffer()
            sniffer.metadata["provider_request_id"] = resp.headers.get("x-amzn-requestid") or resp.headers.get("x-request-id")
            boundary = SSEBoundary()
            outcome = "interrupted"
            result_status = 502
            settled = False
            finalizer_task: asyncio.Task[bool] | None = None
            pending_event = bytearray()
            terminal_deadline: float | None = None
            upstream_terminal: float | None = None
            terminal_emitted = False
            closing_reader: asyncio.Task[bytes | None] | None = None

            def error_event(code: str, message: str) -> bytes:
                # No response.completed fabrication or automatic replay after
                # partial output. The client receives the actual failure.
                payload = {
                    "type": "error",
                    "code": code,
                    "message": f"{message} Request ID: {request_id}",
                    "param": None,
                    "sequence_number": sniffer.sequence_number + 1,
                }
                return b"event: error\ndata: " + json.dumps(payload).encode() + b"\n\n"

            async def finalize_usage(final_status: int, *, deadline: float | None = None) -> bool:
                nonlocal finalizer_task
                if attempt is not None:
                    return False
                if finalizer_task is not None:
                    return await asyncio.shield(finalizer_task)
                finalizer_task = asyncio.create_task(
                    self._log_usage(
                        context,
                        model,
                        self._capture_usage(sniffer.usage, body, model, sniffer.metadata, base_url=routed.base_url),
                        int((time.monotonic() - start) * 1000),
                        final_status,
                        request_id,
                        agent_run_id,
                        routing_decision=routed.decision,
                        **({"retain_failed_bound": True} if sniffer.terminal_event is None else {}),
                    ),
                    name=f"mantle_usage_finalize_{request_id}",
                )
                register_usage_finalizer(finalizer_task)
                try:
                    remaining = max(
                        0,
                        min(deadline or operation_deadline, operation_deadline, asyncio.get_running_loop().time() + self._stream_finalization_timeout)
                        - asyncio.get_running_loop().time(),
                    )
                    return await asyncio.wait_for(asyncio.shield(finalizer_task), timeout=remaining)
                except TimeoutError:
                    logger.warning("Mantle usage finalization exceeded stream deadline", extra={"request_id": request_id})
                    return False
                except Exception:
                    logger.exception("Mantle usage settlement could not be confirmed", extra={"request_id": request_id})
                    return False

            try:
                upstream_chunks = resp.aiter_bytes()
                while True:
                    read_task = asyncio.create_task(anext(upstream_chunks, None))
                    try:
                        completed, _ = await asyncio.wait({read_task}, timeout=max(0, operation_deadline - asyncio.get_running_loop().time()))
                    except asyncio.CancelledError:
                        if resp.is_closed:
                            closing_reader = read_task
                        else:
                            read_task.cancel()
                        register_usage_finalizer(read_task)
                        raise
                    if not completed:
                        if resp.is_closed:
                            closing_reader = read_task
                        else:
                            read_task.cancel()
                        register_usage_finalizer(read_task)
                        raise TimeoutError("upstream operation deadline expired")
                    chunk = read_task.result()
                    if chunk is not None:
                        pending_event.extend(chunk)
                    while match := _SSE_EVENT_END.search(pending_event):
                        if chunk is not None and match.end() == len(pending_event) and pending_event[-1:] == b"\r":
                            break
                        if match.end() > _MAX_SNIFF_BUFFER_BYTES:
                            raise httpx.RemoteProtocolError("Upstream SSE event exceeds settlement buffer limit")
                        event = bytes(pending_event[: match.end()])
                        del pending_event[: match.end()]
                        if asyncio.get_running_loop().time() >= operation_deadline:
                            raise TimeoutError("upstream operation deadline expired")
                        sniffer.feed(event)
                        if sniffer.terminal_event is not None and upstream_terminal is None:
                            upstream_terminal = time.monotonic()
                        if sniffer.terminal_event == "response.completed":
                            terminal_deadline = min(operation_deadline, asyncio.get_running_loop().time() + self._stream_finalization_timeout)
                            if attempt is not None:
                                captured = self._capture_usage(sniffer.usage, body, model, sniffer.metadata, base_url=routed.base_url)
                                receipted = await self._write_final_metering(
                                    attempt,
                                    sniffer.usage,
                                    sniffer.metadata.get("provider_request_id"),
                                    request_id,
                                    deadline=terminal_deadline,
                                    on_receipt=lambda receipt: self._schedule_receipted_settlement(attempt, receipt, request_id),
                                    routing=captured.routing,
                                    latency_ms=int((time.monotonic() - start) * 1000),
                                )
                                settled = receipted
                            else:
                                settled = await finalize_usage(status_code, deadline=terminal_deadline)
                            if not settled:
                                outcome = "settlement_unconfirmed"
                                result_status = 502
                                yield error_event("usage_settlement_unconfirmed", "Model usage could not be confirmed; do not retry this model call.")
                                return
                        if asyncio.get_running_loop().time() >= operation_deadline:
                            raise TimeoutError("upstream operation deadline expired")
                        boundary.feed(event)
                        if sniffer.terminal_event is not None:
                            terminal_emitted = True
                        yield event
                        if attempt is not None and sniffer.terminal_event is not None:
                            break
                    if attempt is not None and sniffer.terminal_event is not None:
                        pending_event.clear()
                        break
                    if len(pending_event) > _MAX_SNIFF_BUFFER_BYTES:
                        raise httpx.RemoteProtocolError("Upstream SSE event exceeds settlement buffer limit")
                    if chunk is None:
                        break
                if pending_event and not (sniffer.terminal_event and not pending_event.strip()):
                    outcome = "premature_eof"
                    raise httpx.RemoteProtocolError("Upstream ended inside a Responses event")
                if sniffer.terminal_event is None:
                    outcome = "premature_eof"
                    if not boundary.at_boundary:
                        raise httpx.RemoteProtocolError("Upstream ended inside a Responses event")
                    yield error_event("upstream_stream_incomplete", "The model stream ended before a terminal response event.")
            except httpx.HTTPError as exc:
                if sniffer.terminal_event is None:
                    if outcome != "premature_eof":
                        outcome = "read_timeout" if isinstance(exc, httpx.ReadTimeout) else "transport_error"
                    result_status = 504 if isinstance(exc, httpx.ReadTimeout) else 502
                    # A partial data line has already reached the client. An
                    # injected error here would corrupt that line as well.
                    if pending_event or not boundary.at_boundary or "settlement buffer limit" in str(exc):
                        raise
                    code = "upstream_stream_timeout" if isinstance(exc, httpx.ReadTimeout) else "upstream_stream_error"
                    message = "Timed out waiting for model output." if isinstance(exc, httpx.ReadTimeout) else "The upstream model connection failed."
                    yield error_event(code, message)
            except TimeoutError:
                if not terminal_emitted:
                    outcome = "operation_timeout"
                    result_status = 504
                    if pending_event or not boundary.at_boundary:
                        raise httpx.ReadTimeout("upstream operation deadline expired")
                    yield error_event("upstream_operation_timeout", "The model operation exceeded its time limit.")
            except (asyncio.CancelledError, GeneratorExit):
                outcome = "client_cancelled"
                result_status = 499
                raise
            finally:
                # Starlette's disconnect cancellation must not interrupt
                # closing the upstream or preserving this request's spend.
                with anyio.CancelScope(shield=True):
                    sniffer.finish()

                    async def close_upstream() -> bool:
                        try:
                            if closing_reader is not None:
                                await asyncio.gather(closing_reader, return_exceptions=True)
                            await resp.aclose()
                        finally:
                            if owns_client:
                                await client.aclose()
                        return True

                    close_task = asyncio.create_task(close_upstream(), name=f"mantle_upstream_close_{request_id}")
                    register_usage_finalizer(close_task)
                    close_budget = (
                        max(0, terminal_deadline - asyncio.get_running_loop().time())
                        if terminal_deadline is not None
                        else max(0, min(1.0, self._stream_finalization_timeout, operation_deadline - asyncio.get_running_loop().time()))
                    )
                    try:
                        await asyncio.wait_for(asyncio.shield(close_task), timeout=close_budget)
                    except TimeoutError:
                        await asyncio.sleep(0)
                        logger.warning("Responses upstream close exceeded stream deadline", extra={"request_id": request_id})
                    except Exception as exc:
                        logger.warning("Responses upstream close failed", extra={"request_id": request_id, "error_type": type(exc).__name__})
                    latency_ms = (time.monotonic() - start) * 1000
                    if sniffer.terminal_event and outcome not in {"settlement_unconfirmed", "operation_timeout", "client_cancelled"}:
                        outcome = sniffer.terminal_event.removeprefix("response.")
                        result_status = 502 if sniffer.terminal_event in {"error", "response.failed"} else status_code
                    if finalizer_task is None and not settled and outcome != "settlement_unconfirmed":
                        await finalize_usage(result_status)
                    finished = time.monotonic()
                    logger.log(
                        logging.WARNING if result_status >= 400 else logging.INFO,
                        "mantle stream %s (model=%s request_id=%s latency_ms=%d)",
                        outcome,
                        model,
                        request_id,
                        int(latency_ms),
                        extra={
                            "stream_outcome": outcome,
                            "terminal_event": sniffer.terminal_event,
                            "stream_read_timeout_seconds": self._stream_timeout.read,
                            "upstream_status": status_code,
                            "status_code": result_status,
                            "request_id": request_id,
                            "service_before_dispatch_ms": round((upstream_start - service_start) * 1000, 1) if upstream_start is not None else None,
                            "upstream_headers_ms": round((upstream_headers - upstream_start) * 1000, 1) if upstream_start is not None else None,
                            "upstream_terminal_ms": round((upstream_terminal - upstream_start) * 1000, 1)
                            if upstream_terminal is not None and upstream_start is not None
                            else None,
                            "service_after_upstream_ms": round((finished - upstream_terminal) * 1000, 1) if upstream_terminal is not None else None,
                            "service_lifecycle_ms": round((finished - service_start) * 1000, 1),
                        },
                    )
                    emit_stream_outcome(outcome)

        return _passthrough()

    # ------------------------------------------------------------------
    # Usage extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_usage(content: bytes) -> dict[str, Any]:
        """Extract token counts from a non-streaming Responses-API body.

        The Responses API returns ``usage: {input_tokens, output_tokens, ...}``.
        Failures are swallowed — metering must never break the passthrough.
        """
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return {}
        return MantlePassthroughService._usage_from_dict(data.get("usage") if isinstance(data, dict) else None)

    @staticmethod
    def _usage_from_dict(found: object) -> dict[str, Any]:
        """Normalize a Responses-API usage object into input/output token counts."""
        if not isinstance(found, dict):
            return {}
        # Keep invalid and absent counters as evidence; policy validation, not
        # int() coercion, decides whether they can be billed.
        #
        # Issue #5226: output_tokens_details is retained for the same reason. It
        # is not priced separately (reasoning bills as output, already inside
        # output_tokens), but it is the evidence that shows whether the reported
        # counts agree with each other — reasoning_tokens exceeding output_tokens
        # means they do not, and the quote contract must not release a
        # reservation against totals that only agree after clamping. Dropping it
        # here would make that conflict invisible downstream.
        return {
            name: found[name]
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "input_tokens_details",
                "output_tokens_details",
            )
            if name in found
        }

    @staticmethod
    def _response_metadata(response: dict[str, Any]) -> dict[str, str]:
        return {name: response[name] for name in ("service_tier", "execution_region") if isinstance(response.get(name), str)}

    @staticmethod
    def _response_metadata_from_bytes(content: bytes) -> dict[str, str]:
        try:
            response = json.loads(content)
            return MantlePassthroughService._response_metadata(response) if isinstance(response, dict) else {}
        except (ValueError, UnicodeError):
            return {}

    def _capture_usage(self, usage, forwarded_body, original_model, metadata, *, base_url=None):
        try:
            forwarded = json.loads(forwarded_body)
            forwarded = forwarded if isinstance(forwarded, dict) else {}
        except (ValueError, UnicodeError):
            forwarded = {}
        forwarded_model = forwarded.get("model", original_model)
        forwarded_model = forwarded_model if isinstance(forwarded_model, str) else original_model
        host = urlparse(base_url or self._base_url).hostname
        matched = re.fullmatch(r"bedrock-(?:mantle|runtime)\.([a-z]{2}(?:-[a-z]+)+-\d)\.(?:api\.aws|amazonaws\.com(?:\.cn)?)", host or "")
        region = matched.group(1) if matched else None
        geography = geography_from_model_prefix(forwarded_model)
        if geography is None and region is not None and forwarded_model.startswith("openai."):
            geography = "in_region"
        if region and region.startswith("us-gov-") and geography is not None:
            geography = "govcloud"
        requested_tier = forwarded.get("service_tier")
        evidence = RoutingEvidence(
            original_model_id=original_model,
            billing_model_id=normalize_billing_model_id(original_model),
            forwarded_model_id=forwarded_model,
            endpoint_host=host,
            endpoint_region=region,
            execution_region=metadata.get("execution_region"),
            geography=geography,
            requested_service_tier=requested_tier if isinstance(requested_tier, str) else None,
            served_service_tier_raw=metadata.get("service_tier"),
        )
        return _CapturedUsage(usage, evidence, metadata.get("provider_request_id"))

    # ------------------------------------------------------------------
    # Metering
    # ------------------------------------------------------------------

    @staticmethod
    async def _trusted_usage(usage: dict[str, Any]) -> TrustedUsage:
        """Ask the route's own quote adapter whether this usage can settle a hold.

        Routed through the registry rather than calling the Responses adapter
        directly so the answer always comes from whichever adapter actually
        issued the bound for this endpoint. If no adapter owns the route there is
        no reservation to protect, so ordinary metering is unaffected.
        """
        from src.orchestration.provider_quotes import TrustedUsage, adapter_for

        adapter = adapter_for(MANTLE_RESPONSES_PATH)
        if adapter is None:
            return TrustedUsage(input_tokens=0, output_tokens=0, known=True)
        try:
            return await adapter.reconcile({"usage": dict(usage)})
        except Exception:
            # A failure to establish trust is not a grant of it.
            logger.exception("Responses usage trust check failed; treating usage as unknown")
            return TrustedUsage.unknown("usage trust check failed")

    async def _log_usage(
        self,
        context: TokenContext,
        model: str,
        usage: dict[str, Any],
        latency_ms: int,
        status_code: int,
        request_id: str | None,
        agent_run_id: str | None,
        *,
        routing_decision: RoutingDecision | None = None,
        retain_failed_bound: bool = False,
    ) -> bool:
        """Write a usage_logs row for this passthrough call.

        Mirrors ``ProxyService._log_usage``: failures are swallowed so metering
        never impacts the proxy hot path. The recorded ``model`` carries the
        OpenAI family so billing can distinguish it from Claude rows.

        Issue #4287: this is the SECOND usage-logging call-site, and it needs the
        reservation reconcile hook just as much as the Bedrock one. Without it,
        every mantle passthrough would hold its pre-charge until the reservation
        expired — so a burst of them would be denied against their own stale
        estimates instead of their real cost.

        Issue #4398: for the same "second call-site" reason, client_tool is read
        here too. Wiring only the Bedrock path would leave client_tool NULL on
        100% of OpenAI passthrough rows — indistinguishable from "not captured",
        so a future breakdown would under-report this route with nothing saying so.

        The account is taken from the decision used before signing. Never
        resolve it again after the request: rules may have changed meanwhile.
        """
        decision = None
        persisted = False
        request_id = request_id or str(uuid4())
        try:
            evidence = getattr(usage, "routing", None) or self._capture_usage(usage, b"{}", model, {}).routing
            decision = await price_completed_usage(
                request_id=request_id,
                org_id=context.attributed_org_id,
                raw_usage=usage,
                evidence=evidence,
                session_factory=get_session_factory(),
            )
        except MissingUsageError:
            record_health_event(METERING_UNCONFIRMED)
            logger.warning("Mantle response has absent or invalid usage; no settlement emitted", extra={"request_id": request_id, "model": model})
        except Exception as exc:
            # Session acquisition can fail before the helper's own guarded read.
            # Build from retained state without allowing a DB outage to lose spend.
            try:
                from pricing_policy import normalize_usage
                from src.budget.pricing_decisions import decision_from_state
                from src.budget.pricing_v2_reader import cached_rate_state, record_connection_failure

                record_connection_failure(exc)
                decision = decision_from_state(
                    request_id=request_id,
                    org_id=context.attributed_org_id,
                    usage=normalize_usage(usage, api_format="openai"),
                    evidence=evidence,
                    state=cached_rate_state(),
                )
            except Exception:
                logger.exception("Failed to create Mantle pricing decision", extra={"request_id": request_id, "model": model})
        input_tokens = decision.usage["uncached_input_tokens"] if decision else 0
        output_tokens = decision.usage["output_tokens"] if decision else 0
        cost_usd = decision.ledger_cost if decision else Decimal("0")

        # Issue #5226: settling a policy reservation needs a STRICTER test than
        # "a decision exists". `normalize_usage` is deliberately forgiving — it
        # bounds contradictory counters (cached > input) and records the conflict
        # as `valid=False` rather than refusing, because the usage_logs row is
        # worth keeping either way. But the quote contract may not release
        # reserved headroom on counters it cannot trust: a partly-invented total
        # would replace a real hold with an understated charge. So the adapter
        # that issued the bound decides whether this usage is settleable, and an
        # untrusted block keeps the hold exactly as an absent one does.
        trusted = await self._trusted_usage(usage)
        if decision is not None and not trusted.known:
            logger.warning(
                "Mantle usage rejected by the Responses quote contract; retaining reservation",
                extra={"request_id": request_id, "model": model, "reason": trusted.reason},
            )

        try:
            # Issue #2792: compute real cost via the shared pricing table instead
            # of the previous hardcoded 0.0. Unknown models fall back to the
            # table's conservative "default" pricing (same as the Bedrock proxy).
            async def write_usage() -> None:
                session_factory = get_session_factory()
                async with session_factory() as session:
                    usage_service = UsageService(session)
                    await usage_service.log_request(
                        context=context,
                        model=model or USAGE_MODEL_FAMILY,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_usd=cost_usd,
                        cache_read_input_tokens=decision.usage["cache_read_input_tokens"]
                        if decision and decision.usage["raw"]["cache_read_input_tokens"] is not None
                        else None,
                        cache_creation_input_tokens=decision.usage["cache_creation_input_tokens"]
                        if decision and decision.usage["raw"]["cache_creation_input_tokens"] is not None
                        else None,
                        latency_ms=latency_ms,
                        status_code=status_code,
                        request_id=request_id,
                        agent_run_id=agent_run_id,
                        # Issue #4398: already normalised (or None) by the route
                        # dependency; None persists as NULL = "not captured".
                        client_tool=_current_client_tool.get(),
                        harness=_current_conversation.get().harness,
                        conversation_id=_current_conversation.get().conversation_id,
                        origin=_current_conversation.get().origin,
                        conversation_title=_current_conversation.get().title,
                        owner_user_id=getattr(context, "_crew_owner_user_id", None),
                        bedrock_account_id=routing_decision.target.account_id if routing_decision and routing_decision.target else None,
                        pricing_decision=decision,
                        reservation_usage_known=decision is not None and trusted.known,
                        provider_request_id=getattr(usage, "provider_request_id", None),
                        destination_region=evidence.endpoint_region if evidence else None,
                    )

            await persist_usage_with_retry(write_usage, request_id=request_id, retry_safe=decision is not None)
            persisted = True
        except Exception as exc:  # noqa: BLE001 - metering must not break the proxy
            record_health_event(METERING_WRITE_FAILURES)
            logger.warning("Failed to write mantle usage_logs row", extra={"request_id": request_id, **usage_write_error_details(exc)})
            await reconcile_budget_reservation(
                context=context,
                request_id=request_id,
                model_id=model,
                input_tokens=0,
                output_tokens=0,
                actual_cost_usd=Decimal("0"),
                usage_known=False,
                retain_failed_bound=True,
            )
        else:
            await reconcile_budget_reservation(
                context=context,
                request_id=request_id,
                model_id=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                actual_cost_usd=cost_usd,
                usage_known=decision is not None and trusted.known,
                retain_failed_bound=retain_failed_bound or (status_code < 400 and (decision is None or not trusted.known)),
            )

        # Optional transcript emission shares the already-committed SQL receipt.
        # The tracker can recover a failed SQL write without duplicating a debit.
        # Usage-only payload: no prompt, response text, or credentials are needed
        # for settlement. Missing usage is not a measured zero.
        if decision is not None:
            try:
                if self._chat_logger is None:
                    self._chat_logger = ChatLoggingService()
                recovery_event = dict(
                    request_id=request_id or str(uuid4()),
                    timestamp=context._budget_request_timestamp,
                    org_id=context.attributed_org_id,
                    user_id=context.user_id,
                    team_id=context.team_id,
                    department_id=context.department_id,
                    root_human_id=context.attributed_user_id,
                    account_type="service" if context.account_type == "service" else "human",
                    model=model,
                    api_format="openai",
                    latency_ms=latency_ms,
                    request_body={},
                    response_body={
                        "model": model,
                        "usage": {
                            "input_tokens": decision.usage["total_input_tokens"],
                            "output_tokens": output_tokens,
                            "cache_read_input_tokens": decision.usage["cache_read_input_tokens"]
                            if decision.usage["raw"]["cache_read_input_tokens"] is not None
                            else None,
                            "cache_creation_input_tokens": decision.usage["cache_creation_input_tokens"]
                            if decision.usage["raw"]["cache_creation_input_tokens"] is not None
                            else None,
                        },
                    },
                    pricing_decision=decision.to_dict(),
                    usage_recovery=usage_recovery_record(context, agent_run_id, status_code) if trusted.known else None,
                )
                if not persisted and trusted.known:
                    # Metering evidence is required even when optional transcript
                    # logging is disabled. Await S3 acknowledgement on this path.
                    await self._chat_logger.write_usage_recovery(**recovery_event)
                else:
                    self._chat_logger.log_chat_async(**recovery_event)

            except Exception as exc:  # noqa: BLE001 - settlement must not break the proxy or usage logging
                logger.warning("Failed to schedule mantle budget settlement", extra={"error": str(exc), "model": model})
        return persisted and decision is not None and trusted.known
