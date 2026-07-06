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
"""

import json
from decimal import Decimal

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from src.shared.enforced_paths import ENFORCED_PATHS
from src.shared.logging import get_logger
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EnforcementResult
from src.shared.timing import get_timings

from .enforcement_service import BudgetEnforcementService, budget_enforcement_service

logger = get_logger(__name__)

# Conservative cost estimate for pre-request budget check (USD).
_DEFAULT_ESTIMATE_USD = Decimal("0.05")


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
            estimated_cost = _DEFAULT_ESTIMATE_USD

            # Issue #249: Check agent-level budget first (most specific wins)
            # The X-Agent-BudgetConfigId header is set by the Lambda authorizer
            agent_budget_config_id = self._get_agent_budget_config_id(request)

            if agent_budget_config_id:
                # Agent has a budget config - check it first
                result = await self.enforcement_service.check_agent_budget(agent_budget_config_id, estimated_cost)
                if not result.allowed:
                    # Agent budget exceeded - block immediately
                    await self._drain_and_respond_budget_exceeded(receive, send, result)
                    return

            # Fall through to team/org hierarchy check
            result = await self.enforcement_service.check_budget_hierarchy(token_context, estimated_cost)

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

            # Write 402 directly via ASGI send()
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

    def _get_agent_budget_config_id(self, request: Request) -> str | None:
        """Get agent budget config ID from request headers.

        Issue #249: The Lambda authorizer passes X-Agent-BudgetConfigId header
        for IAM-authenticated agents. We only trust this header when
        BG_TRUST_APIGW_HEADERS=true (i.e., behind API Gateway).
        """
        from src.shared.config import get_settings

        settings = get_settings()
        if not settings.trust_apigw_headers:
            return None

        return request.headers.get("x-agent-budgetconfigid")

    async def _drain_and_respond_budget_exceeded(self, receive: Receive, send: Send, result: EnforcementResult) -> None:
        """Drain request body and send 402 response for budget exceeded."""
        # Drain the request body
        while True:
            msg = await receive()
            if msg.get("type") == "http.disconnect":
                return
            if not msg.get("more_body", False):
                break

        # Send 402 response
        await self._send_budget_exceeded(send, result)
        logger.info("Agent budget exceeded response sent successfully")

    def _extract_model_id_from_path(self, path: str) -> str | None:
        """Extract model ID from /model/{model_id}/invoke style paths.

        Currently unused after Issue #234 removed inline usage recording.
        Kept for potential future per-model budget enforcement.
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


def create_budget_enforcement_middleware(
    enforcement_service: BudgetEnforcementService | None = None,
):
    """Factory function to create the budget enforcement middleware."""

    def middleware(app: ASGIApp) -> BudgetEnforcementMiddleware:
        return BudgetEnforcementMiddleware(app, enforcement_service=enforcement_service)

    return middleware
