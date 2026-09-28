"""bedrock-mantle passthrough service for OpenAI Responses-API traffic (Issue #2709).

Proxies ``POST /openai/v1/responses`` to the upstream ``bedrock-mantle`` endpoint
so Codex (and any future OpenAI-model client) rides the same per-tenant metering
and model-allowlist governance that Claude traffic gets today.

This is a **passthrough, not a translation** — the request body is forwarded
byte-for-byte and the response (including streaming chunks) is returned verbatim.
It intentionally does NOT touch ``format_translator.py``.

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

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from src.budget.pricing import pricing_service
from src.proxy.mantle_auth import MantleAuth
from src.shared.database import get_session_factory
from src.shared.schemas.auth import TokenContext
from src.usage.service import UsageService

logger = logging.getLogger(__name__)

# GPT-5.5 quirk: mantle serves the Responses API on this path.
MANTLE_RESPONSES_PATH = "/openai/v1/responses"

# Model-family dimension recorded on usage_logs.model for OpenAI-model traffic
# so metering can distinguish it from Claude/Bedrock rows.
USAGE_MODEL_FAMILY = "openai"

# Safety valve for the per-stream partial-line buffer (issue #2828). A well-formed
# `response.completed` event is well under this; if a line ever exceeds it (e.g. a
# missing newline upstream), we drop the buffer with a WARN rather than grow it
# unboundedly — metering must never threaten gateway pod memory or the stream.
_MAX_SNIFF_BUFFER_BYTES = 1024 * 1024  # 1 MiB


@dataclass
class MantleResponse:
    """Result of a non-streaming mantle passthrough call."""

    status_code: int
    content: bytes
    media_type: str = "application/json"
    # Token usage extracted from the Responses-API `usage` block (best-effort).
    usage: dict[str, int] = field(default_factory=dict)


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
        self.usage: dict[str, int] = {}
        self._buffer = b""

    def feed(self, chunk: bytes) -> None:
        """Consume one upstream chunk, updating ``usage`` from any complete lines."""
        self._buffer += chunk
        # Cap the carried buffer: on overflow drop it (metering must never break
        # the stream or grow pod memory unboundedly).
        if len(self._buffer) > _MAX_SNIFF_BUFFER_BYTES:
            logger.warning(
                "mantle usage sniffer buffer exceeded cap; dropping partial line",
                extra={"buffer_bytes": len(self._buffer), "cap_bytes": _MAX_SNIFF_BUFFER_BYTES},
            )
            self._buffer = b""
            return
        # Split on newlines; the last element is the (possibly incomplete)
        # trailing fragment, carried forward until its newline arrives.
        *complete, self._buffer = self._buffer.split(b"\n")
        for raw in complete:
            self._parse_line(raw)

    def _parse_line(self, raw: bytes) -> None:
        try:
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line.startswith("data:"):
                return
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]" or not payload.startswith("{"):
                return
            data = json.loads(payload)
            # usage may be top-level or nested under a "response" object.
            found = data.get("usage")
            if found is None and isinstance(data.get("response"), dict):
                found = data["response"].get("usage")
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
        timeout: Upstream request timeout in seconds.
    """

    def __init__(
        self,
        auth: MantleAuth,
        base_url: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._auth = auth
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._http_client = http_client

    @property
    def upstream_url(self) -> str:
        return f"{self._base_url}{MANTLE_RESPONSES_PATH}"

    def _headers(self, body: bytes) -> dict[str, str]:
        """Build outbound headers, SigV4-signing the exact body bytes.

        The signature is computed over ``body`` and the upstream URL, so callers
        MUST send these same bytes unchanged (any re-serialization breaks the
        signature). Signed auth headers (Authorization, X-Amz-Date,
        X-Amz-Security-Token) are sensitive — never log them.
        """
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(self._auth.sign("POST", self.upstream_url, body))
        return headers

    def _client(self) -> httpx.AsyncClient:
        return self._http_client or httpx.AsyncClient(timeout=self._timeout)

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
        """
        if stream:
            return await self._stream(body, context, model=model, request_id=request_id, agent_run_id=agent_run_id)
        return await self._invoke(body, context, model=model, request_id=request_id, agent_run_id=agent_run_id)

    async def _invoke(
        self,
        body: bytes,
        context: TokenContext,
        *,
        model: str,
        request_id: str | None,
        agent_run_id: str | None,
    ) -> MantleResponse:
        start = time.monotonic()
        status_code = 502
        usage: dict[str, int] = {}
        headers = self._headers(body)
        client = self._client()
        try:
            resp = await client.post(self.upstream_url, content=body, headers=headers)
            status_code = resp.status_code
            content = resp.content
            # Only extract usage on success bodies; upstream errors pass through untouched.
            if 200 <= status_code < 300:
                usage = self._extract_usage(content)
            return MantleResponse(
                status_code=status_code,
                content=content,
                media_type=resp.headers.get("content-type", "application/json"),
                usage=usage,
            )
        finally:
            if self._http_client is None:
                await client.aclose()
            latency_ms = (time.monotonic() - start) * 1000
            await self._log_usage(context, model, usage, int(latency_ms), status_code, request_id, agent_run_id)

    async def _stream(
        self,
        body: bytes,
        context: TokenContext,
        *,
        model: str,
        request_id: str | None,
        agent_run_id: str | None,
    ) -> AsyncIterator[bytes]:
        start = time.monotonic()
        headers = self._headers(body)
        client = self._client()
        owns_client = self._http_client is None

        # Eager connect + status check BEFORE handing a generator to the route.
        # A StreamingResponse sends its 200 header before the generator's first
        # iteration, so any upstream failure surfaced from inside the generator
        # reaches the client as a 200 whose stream dies instantly — invisible in
        # gateway logs and indistinguishable (to the caller) from a broken
        # stream. Connecting here lets the route return the real upstream
        # status, and guarantees the failure is logged (#3897).
        try:
            upstream_request = client.build_request("POST", self.upstream_url, content=body, headers=headers)
            resp = await client.send(upstream_request, stream=True)
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
            await self._log_usage(context, model, {}, latency_ms, 502, request_id, agent_run_id)
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
            await self._log_usage(context, model, {}, latency_ms, status_code, request_id, agent_run_id)
            raise MantleUpstreamError(
                status_code,
                error_body,
                resp.headers.get("content-type", "application/json"),
            )

        status_code = resp.status_code

        async def _passthrough() -> AsyncIterator[bytes]:
            sniffer = _StreamUsageSniffer()
            try:
                async for chunk in resp.aiter_bytes():
                    # Passthrough: yield upstream bytes verbatim, sniff usage as
                    # we go. The sniffer buffers partial lines internally; the
                    # yielded bytes are never modified.
                    sniffer.feed(chunk)
                    yield chunk
            finally:
                await resp.aclose()
                if owns_client:
                    await client.aclose()
                latency_ms = (time.monotonic() - start) * 1000
                logger.info(
                    "mantle stream completed: HTTP %d (model=%s request_id=%s latency_ms=%d)",
                    status_code,
                    model,
                    request_id,
                    int(latency_ms),
                )
                await self._log_usage(context, model, sniffer.usage, int(latency_ms), status_code, request_id, agent_run_id)

        return _passthrough()

    # ------------------------------------------------------------------
    # Usage extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_usage(content: bytes) -> dict[str, int]:
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
    def _usage_from_dict(found: object) -> dict[str, int]:
        """Normalize a Responses-API usage object into input/output token counts."""
        if not isinstance(found, dict):
            return {}
        usage: dict[str, int] = {}
        input_tokens = found.get("input_tokens")
        output_tokens = found.get("output_tokens")
        if input_tokens is not None:
            usage["input_tokens"] = int(input_tokens)
        if output_tokens is not None:
            usage["output_tokens"] = int(output_tokens)
        return usage

    # ------------------------------------------------------------------
    # Metering
    # ------------------------------------------------------------------

    async def _log_usage(
        self,
        context: TokenContext,
        model: str,
        usage: dict[str, int],
        latency_ms: int,
        status_code: int,
        request_id: str | None,
        agent_run_id: str | None,
    ) -> None:
        """Write a usage_logs row for this passthrough call.

        Mirrors ``ProxyService._log_usage``: failures are swallowed so metering
        never impacts the proxy hot path. The recorded ``model`` carries the
        OpenAI family so billing can distinguish it from Claude rows.
        """
        try:
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            # Issue #2792: compute real cost via the shared pricing table instead
            # of the previous hardcoded 0.0. Unknown models fall back to the
            # table's conservative "default" pricing (same as the Bedrock proxy).
            cost_usd = float(pricing_service.calculate_cost(model, input_tokens, output_tokens))
            session_factory = get_session_factory()
            async with session_factory() as session:
                usage_service = UsageService(session)
                await usage_service.log_request(
                    context=context,
                    model=model or USAGE_MODEL_FAMILY,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    latency_ms=latency_ms,
                    status_code=status_code,
                    request_id=request_id,
                    agent_run_id=agent_run_id,
                )
        except Exception as exc:  # noqa: BLE001 - metering must not break the proxy
            logger.warning("Failed to write mantle usage_logs row", extra={"error": str(exc), "model": model})
