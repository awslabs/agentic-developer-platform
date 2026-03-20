import json
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from fastapi import Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware

from src.shared.logging import get_logger
from src.shared.schemas.auth import TokenContext

from .config import budget_config
from .service import BudgetService
from .utils import calculate_model_cost

logger = get_logger(__name__)


class BudgetEnforcementMiddleware(BaseHTTPMiddleware):
    """Middleware for automatic budget checking and enforcement on API requests."""

    def __init__(self, app, budget_service: BudgetService | None = None):
        super().__init__(app)
        self.budget_service = budget_service or BudgetService()
        self.request_costs: dict[str, dict] = {}  # Track request costs for usage recording

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request with budget checking."""

        # Skip budget checks for non-API routes or if budget checking is disabled
        if not budget_config.budget_check_enabled or not self._should_check_budget(request):
            return await call_next(request)

        # Get user context from request
        context = self._extract_user_context(request)
        if not context:
            return await call_next(request)

        # Estimate request cost
        estimated_cost = await self._estimate_request_cost(request)

        # Check budget before processing request
        try:
            enforcement_result = await self.budget_service.check_budget_with_cost(context, estimated_cost)

            # If budget check fails with hard enforcement, block request
            if not enforcement_result.allowed:
                return self._create_budget_exceeded_response(enforcement_result)

            # Add warnings to response headers if any
            if enforcement_result.warnings:
                request.state.budget_warnings = enforcement_result.warnings

        except Exception as e:
            # Log error but don't block request if budget check fails
            logger.warning(
                "Budget check failed",
                extra={"error": str(e), "error_type": type(e).__name__},
            )

        # Record request start time and estimated cost
        request_id = id(request)
        self.request_costs[request_id] = {
            "start_time": time.time(),
            "estimated_cost": estimated_cost,
            "context": context,
        }

        # Process request
        response = await call_next(request)

        # Record actual usage after request completes
        await self._record_usage_after_request(request, response, request_id)

        # Add budget warnings to response headers
        if hasattr(request.state, "budget_warnings"):
            response.headers["X-Budget-Warnings"] = json.dumps(request.state.budget_warnings)

        # Clean up request tracking
        self.request_costs.pop(request_id, None)

        return response

    def _should_check_budget(self, request: Request) -> bool:
        """Determine if budget checking should be applied to this request."""
        path = request.url.path

        # Skip budget checks for:
        # - Health/status endpoints
        # - Budget management endpoints (to avoid circular dependencies)
        # - Authentication endpoints
        # - Static assets
        skip_patterns = [
            "/health",
            "/status",
            "/budgets",
            "/auth",
            "/static",
            "/docs",
            "/openapi",
        ]

        for pattern in skip_patterns:
            if path.startswith(pattern):
                return False

        # Only check for API requests that consume AI models
        # This would typically be determined by checking if the request
        # is going to a Bedrock proxy endpoint or similar
        api_patterns = [
            "/proxy",
            "/models",
            "/chat",
            "/completions",
        ]

        return any(path.startswith(pattern) for pattern in api_patterns)

    def _extract_user_context(self, request: Request) -> TokenContext | None:
        """Extract user context from request."""
        # This would normally extract the TokenContext from JWT token or session
        # For now, return a mock context if available in request state
        if hasattr(request.state, "user_context"):
            return request.state.user_context

        # Mock context for development
        return TokenContext(
            user_id="user-123",
            org_id="org-123",
            team_id="team-123",
            department_id="dept-123",
            account_type="human",
            is_admin=False,
            expires_at=datetime.utcnow(),
        )

    async def _estimate_request_cost(self, request: Request) -> Decimal:
        """Estimate the cost of processing this request."""
        # Default small cost for budget checking
        default_cost = Decimal("0.01")

        try:
            # Try to extract model and token information from request
            if request.method == "POST":
                body = await request.body()
                if body:
                    try:
                        request_data = json.loads(body)
                        model_name = request_data.get("model", "default")

                        # Estimate token usage based on request content
                        estimated_input_tokens = self._estimate_tokens_from_content(request_data)
                        estimated_output_tokens = estimated_input_tokens // 2  # Conservative estimate

                        cost, _, _ = calculate_model_cost(model_name, estimated_input_tokens, estimated_output_tokens)
                        return cost

                    except (json.JSONDecodeError, KeyError):
                        pass

            return default_cost

        except Exception:
            return default_cost

    def _estimate_tokens_from_content(self, request_data: dict) -> int:
        """Estimate token count from request content."""
        # Simple estimation: ~4 characters per token
        content = ""

        # Extract text content from common request fields
        for field in ["prompt", "message", "messages", "input", "text"]:
            if field in request_data:
                value = request_data[field]
                if isinstance(value, str):
                    content += value
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and "content" in item:
                            content += str(item["content"])
                        elif isinstance(item, str):
                            content += item

        # Convert to estimated tokens (rough approximation)
        estimated_tokens = max(len(content) // 4, 10)  # Minimum 10 tokens
        return min(estimated_tokens, 8000)  # Cap at 8k tokens for estimation

    def _create_budget_exceeded_response(self, enforcement_result) -> Response:
        """Create response when budget is exceeded."""
        error_data = {
            "error": "budget_exceeded",
            "message": enforcement_result.blocked_reason or "Budget exceeded",
            "details": {
                "exceeded_entity_type": enforcement_result.exceeded_entity_type.value if enforcement_result.exceeded_entity_type else None,
                "exceeded_entity_id": enforcement_result.exceeded_entity_id,
                "budget_amount_usd": float(enforcement_result.budget_amount_usd) if enforcement_result.budget_amount_usd else None,
                "current_spend_usd": float(enforcement_result.current_spend_usd) if enforcement_result.current_spend_usd else None,
                "enforcement_mode": enforcement_result.enforcement_mode.value if enforcement_result.enforcement_mode else None,
            },
        }

        return Response(content=json.dumps(error_data), status_code=status.HTTP_402_PAYMENT_REQUIRED, headers={"Content-Type": "application/json"})

    async def _record_usage_after_request(self, request: Request, response: Response, request_id: int):
        """Record actual usage after request completes."""
        request_data = self.request_costs.get(request_id)
        if not request_data:
            return

        try:
            context = request_data["context"]

            # Extract actual usage from response if available
            actual_tokens_in, actual_tokens_out, model_name = await self._extract_actual_usage(request, response)

            if actual_tokens_in is not None and actual_tokens_out is not None:
                # Record actual usage
                await self.budget_service.record_usage(context, actual_tokens_in, actual_tokens_out, model_name or "default")
            else:
                # Fall back to estimated usage
                estimated_cost = request_data["estimated_cost"]
                # Convert estimated cost back to approximate tokens for recording
                estimated_tokens = int(estimated_cost * 1000 / 3)  # Rough approximation

                await self.budget_service.record_usage(context, estimated_tokens // 2, estimated_tokens // 2, "default")

        except Exception as e:
            # Log error but don't fail the request
            logger.warning(
                "Failed to record budget usage",
                extra={"error": str(e), "error_type": type(e).__name__},
            )

    async def _extract_actual_usage(self, request: Request, response: Response) -> tuple:
        """Extract actual token usage from request/response."""
        try:
            # Try to extract usage from response headers or body
            if hasattr(response, "headers"):
                tokens_in = response.headers.get("X-Tokens-Input")
                tokens_out = response.headers.get("X-Tokens-Output")
                model = response.headers.get("X-Model-Name")

                if tokens_in and tokens_out:
                    return int(tokens_in), int(tokens_out), model

            # If not in headers, try to extract from response body
            # This would depend on the specific format of your AI service responses

            return None, None, None

        except Exception:
            return None, None, None
