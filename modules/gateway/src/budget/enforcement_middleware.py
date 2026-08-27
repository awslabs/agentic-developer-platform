"""
Budget Enforcement Middleware (pure ASGI).

This middleware intercepts proxy requests to check budget constraints
before forwarding to Bedrock. It supports cascading enforcement across
the entity hierarchy (user → team → department → organization).

IMPORTANT: This is a raw ASGI middleware — NOT BaseHTTPMiddleware.
BaseHTTPMiddleware has a known Starlette bug where returning a response
from dispatch() without calling call_next() causes the response to hang
indefinitely. By using raw ASGI, we write the 402 response directly via
the ASGI `send` callable, which is guaranteed to reach the client.

Issue #234: Removed inline usage recording — now handled by S3-triggered
            Lambda for accurate cost tracking from chat logs.

Issue #249: Added agent-level budget checking via X-Agent-BudgetConfigId header.
            Agent budgets are checked BEFORE team/org hierarchy (most specific wins).

Issue #4287: the pre-request estimate is model- and size-aware instead of a flat
            $0.05. Still no body read — see _estimate_cost.
"""

import json
from decimal import Decimal

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from src.shared.enforced_paths import ENFORCED_PATHS
from src.shared.logging import get_logger
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import DenyReason, EnforcementResult
from src.shared.timing import get_timings

from .enforcement_service import BudgetEnforcementService, budget_enforcement_service

logger = get_logger(__name__)

# Fallback pre-request estimate (USD), used only when neither the model nor the
# request size can be determined from the ASGI scope — e.g. a chunked upload with
# no content-length on a route that does not carry the model in its path.
#
# Issue #4287: this used to be the estimate for EVERY request, flat, regardless of
# model or size. A single large call against an expensive model could therefore
# overshoot a cap the check had just passed, because the check priced it at 5
# cents. It is now the last resort, not the rule.
_DEFAULT_ESTIMATE_USD = Decimal("0.05")

# Retry-After for check-failure denials (Issue #4075). Short on purpose: the
# ledger is expected back in seconds, and the whole point of using 503 over 402
# is that the client can recover unaided.
_CHECK_UNAVAILABLE_RETRY_AFTER = b"5"


class BudgetEnforcementMiddleware:
    """
    Pure ASGI middleware for enforcing budget limits on proxy requests.

    Pre-request: checks budget using model ID from URL path and a fixed
    cost estimate. Does NOT read the request body.

    When budget is exceeded, writes a 402 response directly via ASGI send()
    — no BaseHTTPMiddleware, no Starlette Response objects, no hanging.

    Note (Issue #234): Usage recording is now handled by the budget-usage-tracker
    Lambda, which is triggered by S3 PutObject events when chat logs are written.
    This provides accurate cost tracking from actual Bedrock response token counts.
    """

    def __init__(
        self,
        app: ASGIApp,
        enforcement_service: BudgetEnforcementService | None = None,
    ):
        self.app = app
        self.enforcement_service = enforcement_service or budget_enforcement_service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        if not self._should_enforce(path):
            await self.app(scope, receive, send)
            return

        # Get token_context from scope state (set by TokenContextMiddleware)
        state = scope.get("state", {})
        token_context: TokenContext | None = state.get("token_context")

        if not token_context:
            await self.app(scope, receive, send)
            return

        # Build a Request object for timing access (read-only, no body access)
        request = Request(scope, receive, send)

        timings = get_timings(request)
        with timings.time_segment("budget_check"):
            estimated_cost = self._estimate_cost(scope, path)

            # Issue #249 read the agent-level budget config id from the
            # X-Agent-BudgetConfigId header, which the (now deprecated and
            # unattached) Lambda authorizer was meant to set. Issue #3985
            # removed that: the header was trusted on presence alone under
            # BG_TRUST_APIGW_HEADERS, so a client could name any budget config
            # — including a fresh/unspent one — and dodge its own agent budget.
            # Only the org/team hierarchy check remains, and it derives entirely
            # from the authenticated token_context.
            #
            # Re-adding per-agent budget enforcement requires resolving the
            # config id from the agent registry entry (server-side, keyed off
            # the authenticated identity), not from a request header.
            result = await self.enforcement_service.check_budget_hierarchy(
                token_context,
                estimated_cost,
                # Issue #4287: idempotency key for the live-denominator
                # reservation, so the proxy can adjust THIS request's reservation
                # to its real cost once the response lands. Set by
                # LoggingMiddleware, which runs outside this one.
                request_id=state.get("request_id"),
            )

        if not result.allowed:
            # Drain the request body — some ASGI servers (Uvicorn/h11)
            # require the request body to be fully consumed before a
            # response can be sent, otherwise the connection hangs.
            while True:
                msg = await receive()
                if msg.get("type") == "http.disconnect":
                    # Client disconnected before we could respond
                    return
                if not msg.get("more_body", False):
                    break

            # Write the denial directly via ASGI send(). Issue #4075: which
            # denial matters — a real cap is 402, an unreadable ledger is 503.
            if result.deny_reason == DenyReason.CHECK_UNAVAILABLE:
                await self._send_check_unavailable(send, result)
                logger.info("Budget check unavailable response sent successfully")
            else:
                await self._send_budget_exceeded(send, result)
                logger.info("Budget exceeded response sent successfully")
            return

        # Let the request through to the next middleware/app
        await self.app(scope, receive, send)

        # Issue #234: Usage recording removed from middleware.
        # Actual cost tracking is now handled by the budget-usage-tracker Lambda,
        # which is triggered when chat logs are written to S3. This provides
        # accurate token counts from Bedrock responses rather than estimates.

    def _should_enforce(self, path: str) -> bool:
        return any(path.startswith(p) for p in ENFORCED_PATHS)

    def _estimate_cost(self, scope: Scope, path: str) -> Decimal:
        """Estimate this request's cost from the ASGI scope alone (Issue #4287).

        Two inputs, both available without touching the body:

        * the model, from the URL path (``/model/{model_id}/invoke``)
        * the request size, from the ``content-length`` header

        The body is deliberately NOT read. This is pure ASGI: ``receive()`` is a
        one-shot stream, so consuming it here would starve the downstream handler
        unless it were buffered and replayed — and the mantle route
        (``/openai/v1/responses``) forwards the body byte-for-byte, which
        ``src/shared/enforced_paths.py`` documents as a hard constraint. The cost
        of that constraint is that ``max_tokens`` is invisible, so the output
        estimate falls back to the pricing module's default.

        The remaining gap is closed on the way out, not here: the reservation
        taken against this estimate is adjusted to the request's real token cost
        once the response lands (see
        ``BudgetEnforcementService.reconcile_reservation``). So this only has to
        be a reasonable pre-charge, not an accurate price.

        Falls back to the flat ``_DEFAULT_ESTIMATE_USD`` only when the size is
        unknown, which is strictly better than the pre-#4287 behavior of using it
        for everything.
        """
        content_length = self._content_length(scope)
        if content_length is None:
            return _DEFAULT_ESTIMATE_USD

        # No model in the path (e.g. /v1/chat/completions, where it lives in the
        # unreadable body) — price it with the pricing table's conservative
        # default rather than giving up on size-awareness too.
        model_id = self._extract_model_id_from_path(path) or "default"

        return self.enforcement_service.estimate_cost_from_payload_size(model_id, content_length)

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        """Read ``content-length`` out of the raw ASGI headers.

        Same convention as ``src/admin/middleware.py``. Returns ``None`` when the
        header is absent (chunked upload) or unparseable, so the caller can fall
        back rather than pre-charge a request $0.
        """
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    return None
        return None

    def _extract_model_id_from_path(self, path: str) -> str | None:
        """Extract model ID from /model/{model_id}/invoke style paths.

        Issue #4287: revived. This was dead code after Issue #234 removed inline
        usage recording, kept "for potential future per-model budget
        enforcement" — which is exactly what the model-aware pre-request estimate
        needs, and it is the only model signal available without reading the body.
        """
        if not path.startswith("/model/"):
            return None
        parts = path.split("/")
        if len(parts) < 3 or parts[1] != "model":
            return None
        suffix_parts = []
        for i in range(2, len(parts)):
            if parts[i] in ("invoke", "invoke-with-response-stream"):
                break
            suffix_parts.append(parts[i])
        return "/".join(suffix_parts) if suffix_parts else None

    async def _send_budget_exceeded(self, send: Send, result: EnforcementResult) -> None:
        """Write a 402 JSON response directly via ASGI send().

        This bypasses all Starlette response machinery and writes raw
        HTTP response start + body messages to the ASGI send callable.
        It is impossible for this to hang.
        """
        retry_after = "3600"
        error_body = {
            "error": "budget_exceeded",
            "message": result.blocked_reason or "Budget limit exceeded",
            "details": {
                "entity_type": (result.exceeded_entity_type.value if result.exceeded_entity_type else None),
                "entity_id": result.exceeded_entity_id,
                "budget_usd": (float(result.budget_amount_usd) if result.budget_amount_usd else None),
                "spent_usd": (float(result.current_spend_usd) if result.current_spend_usd else None),
                "enforcement_mode": (result.enforcement_mode.value if result.enforcement_mode else None),
            },
        }

        body_bytes = json.dumps(error_body).encode("utf-8")

        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body_bytes)).encode()),
            (b"retry-after", retry_after.encode()),
            (b"x-budget-remaining", b"0"),
        ]
        if result.budget_amount_usd:
            headers.append((b"x-budget-limit", f"{float(result.budget_amount_usd):.2f}".encode()))

        logger.warning(f"Budget exceeded - blocking request: {error_body['details']}")

        # Use 402 Payment Required instead of 429 Too Many Requests.
        # The AWS SDK auto-retries 429 (throttling), which causes the client
        # to appear "hung" in a retry loop. 402 is not retried by the SDK
        # and semantically correct for "you ran out of budget".
        await send(
            {
                "type": "http.response.start",
                "status": 402,
                "headers": headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body_bytes,
            }
        )

    async def _send_check_unavailable(self, send: Send, result: EnforcementResult) -> None:
        """Write a 503 JSON response when the budget CHECK failed (Issue #4075).

        Deliberately not a 402. Under fail-closed, a DB/IAM fault would
        otherwise present platform-wide as "budget exceeded" with a null budget
        and null spend — which sends operators chasing a billing problem during
        a database incident, and corrupts any dashboard built on 402 rates.

        503 is also retryable where 402 is deliberately not (see the comment in
        _send_budget_exceeded), so clients recover on their own once the ledger
        comes back instead of needing an operator. Nothing here is known to be
        over budget, so no budget headers are emitted.
        """
        body_bytes = json.dumps(
            {
                "error": "budget_check_unavailable",
                "message": ("Budget enforcement is temporarily unable to verify spend for this request. Retry shortly."),
                "details": {"reason": result.blocked_reason},
            }
        ).encode("utf-8")

        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body_bytes)).encode()),
            (b"retry-after", _CHECK_UNAVAILABLE_RETRY_AFTER),
        ]

        logger.error(f"Budget check unavailable - denying request: {result.blocked_reason}")

        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body_bytes,
            }
        )


def create_budget_enforcement_middleware(
    enforcement_service: BudgetEnforcementService | None = None,
):
    """Factory function to create the budget enforcement middleware."""

    def middleware(app: ASGIApp) -> BudgetEnforcementMiddleware:
        return BudgetEnforcementMiddleware(app, enforcement_service=enforcement_service)

    return middleware
