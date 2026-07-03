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


@dataclass
class MantleResponse:
    """Result of a non-streaming mantle passthrough call."""

    status_code: int
    content: bytes
    media_type: str = "application/json"
    # Token usage extracted from the Responses-API `usage` block (best-effort).
    usage: dict[str, int] = field(default_factory=dict)


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
            return self._stream(body, context, model=model, request_id=request_id, agent_run_id=agent_run_id)
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
        status_code = 502
        usage: dict[str, int] = {}
        headers = self._headers(body)
        client = self._client()
        try:
            async with client.stream("POST", self.upstream_url, content=body, headers=headers) as resp:
                status_code = resp.status_code
                async for chunk in resp.aiter_bytes():
                    # Passthrough: yield upstream bytes verbatim, sniff usage as we go.
                    self._accumulate_stream_usage(chunk, usage)
                    yield chunk
        finally:
            if self._http_client is None:
                await client.aclose()
            latency_ms = (time.monotonic() - start) * 1000
            await self._log_usage(context, model, usage, int(latency_ms), status_code, request_id, agent_run_id)

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

    def _accumulate_stream_usage(self, chunk: bytes, usage: dict[str, int]) -> None:
        """Sniff the ``usage`` block from streamed SSE chunks (best-effort).

        The Responses-API stream emits a terminal ``response.completed`` event
        whose ``response.usage`` carries the final token counts. We parse any
        ``data:`` line that contains a ``usage`` object and keep the latest.
        """
        try:
            text = chunk.decode("utf-8", errors="ignore")
            for line in text.split("\n"):
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload or payload == "[DONE]" or not payload.startswith("{"):
                    continue
                data = json.loads(payload)
                # usage may be top-level or nested under a "response" object.
                found = data.get("usage")
                if found is None and isinstance(data.get("response"), dict):
                    found = data["response"].get("usage")
                parsed = self._usage_from_dict(found)
                if parsed:
                    usage.update(parsed)
        except (json.JSONDecodeError, ValueError, UnicodeError):
            pass  # never disrupt the stream for usage extraction

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
            session_factory = get_session_factory()
            async with session_factory() as session:
                usage_service = UsageService(session)
                await usage_service.log_request(
                    context=context,
                    model=model or USAGE_MODEL_FAMILY,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    cost_usd=0.0,
                    latency_ms=latency_ms,
                    status_code=status_code,
                    request_id=request_id,
                    agent_run_id=agent_run_id,
                )
        except Exception as exc:  # noqa: BLE001 - metering must not break the proxy
            logger.warning("Failed to write mantle usage_logs row", extra={"error": str(exc), "model": model})
