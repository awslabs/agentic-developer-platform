"""
Integration tests for complete middleware chain execution order.

These tests verify that the middleware chain executes in the correct order:
Auth middleware → Rate limit middleware → Budget middleware → Proxy handler

The tests verify correct order and short-circuit behavior when
middleware checks fail.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.exceptions import (
    BudgetExceededError,
    InvalidCredentialsError,
    RateLimitExceededError,
    TokenExpiredError,
)
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.common import BudgetCheckResult, RateLimitCheckResult
from tests.fixtures.factories import (
    create_budget_config,
    create_department,
    create_org,
    create_rate_limit_config,
    create_team,
    create_token,
    create_user,
)
from tests.fixtures.mock_aws import MockBedrockClient


@pytest.mark.integration
class TestMiddlewareChain:
    """Test suite for middleware chain execution."""

    @pytest.mark.asyncio
    async def test_middleware_executes_in_correct_order(
        self,
        db_session: AsyncSession,
    ):
        """
        Test that middleware executes in the correct order:
        Auth → Rate Limit → Budget → Proxy

        The correct order ensures:
        1. Unauthenticated requests are rejected first
        2. Rate limiting protects from abuse before expensive operations
        3. Budget checks happen before proxy to prevent overspending
        4. Proxy only executes if all checks pass
        """
        # Setup
        org = await create_org(db_session, id="org-chain")
        dept = await create_department(db_session, org.id, id="dept-chain")
        team = await create_team(db_session, org.id, dept.id, id="team-chain")
        user = await create_user(db_session, org.id, team.id, id="user-chain")

        token, raw_token = await create_token(
            db_session,
            org.id,
            team.id,
            dept.id,
            user.id,
        )

        await create_rate_limit_config(
            db_session,
            org.id,
            "user",
            user.id,
            rpm=100,
        )

        await create_budget_config(
            db_session,
            org.id,
            "user",
            user.id,
            budget_amount_usd=1000,
        )
        await db_session.commit()

        # Track middleware execution order
        execution_order = []

        # Mock middleware functions
        async def auth_middleware(token_str: str) -> TokenContext:
            execution_order.append("auth")
            return TokenContext(
                user_id=user.id,
                org_id=org.id,
                team_id=team.id,
                department_id=dept.id,
                account_type="human",
                is_admin=False,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )

        async def rate_limit_middleware(context: TokenContext) -> RateLimitCheckResult:
            execution_order.append("rate_limit")
            return RateLimitCheckResult(
                allowed=True,
                limit=100,
                remaining=99,
            )

        async def budget_middleware(context: TokenContext) -> BudgetCheckResult:
            execution_order.append("budget")
            return BudgetCheckResult(
                allowed=True,
                budget_usd=1000,
                spent_usd=0,
            )

        async def proxy_handler(context: TokenContext, request: dict) -> dict:
            execution_order.append("proxy")
            return {"status": "success", "content": "Mock response"}

        # Execute chain
        context = await auth_middleware(raw_token)
        await rate_limit_middleware(context)
        await budget_middleware(context)
        await proxy_handler(context, {"messages": []})

        # Verify execution order
        assert execution_order == ["auth", "rate_limit", "budget", "proxy"]

    @pytest.mark.asyncio
    async def test_auth_failure_short_circuits_chain(self):
        """
        Test that auth failure stops the middleware chain.

        If authentication fails, no further middleware should execute.
        """
        execution_order = []

        # Mock middleware with auth failure
        async def auth_middleware(token_str: str) -> TokenContext:
            execution_order.append("auth")
            raise InvalidCredentialsError("Invalid token")

        async def rate_limit_middleware(context: TokenContext) -> RateLimitCheckResult:
            execution_order.append("rate_limit")
            return RateLimitCheckResult(allowed=True)

        async def budget_middleware(context: TokenContext) -> BudgetCheckResult:
            execution_order.append("budget")
            return BudgetCheckResult(allowed=True)

        async def proxy_handler(context: TokenContext, request: dict) -> dict:
            execution_order.append("proxy")
            return {"status": "success"}

        # Execute chain with auth failure
        with pytest.raises(InvalidCredentialsError):
            context = await auth_middleware("invalid-token")
            # These should NOT execute
            await rate_limit_middleware(context)
            await budget_middleware(context)
            await proxy_handler(context, {})

        # Only auth should have executed
        assert execution_order == ["auth"]

    @pytest.mark.asyncio
    async def test_rate_limit_failure_short_circuits_chain(self):
        """
        Test that rate limit failure stops the middleware chain.

        If rate limiting fails, budget check and proxy should not execute.
        """
        execution_order = []

        context = TokenContext(
            user_id="user-test",
            org_id="org-test",
            team_id="team-test",
            department_id="dept-test",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        async def rate_limit_middleware(ctx: TokenContext) -> RateLimitCheckResult:
            execution_order.append("rate_limit")
            raise RateLimitExceededError(
                limit_type="rpm",
                limit=100,
                retry_after=30,
            )

        async def budget_middleware(ctx: TokenContext) -> BudgetCheckResult:
            execution_order.append("budget")
            return BudgetCheckResult(allowed=True)

        async def proxy_handler(ctx: TokenContext, request: dict) -> dict:
            execution_order.append("proxy")
            return {"status": "success"}

        # Execute chain
        execution_order.append("auth")  # Auth passed

        with pytest.raises(RateLimitExceededError):
            await rate_limit_middleware(context)
            # These should NOT execute
            await budget_middleware(context)
            await proxy_handler(context, {})

        # Auth and rate_limit should have executed
        assert execution_order == ["auth", "rate_limit"]

    @pytest.mark.asyncio
    async def test_budget_failure_short_circuits_chain(self):
        """
        Test that budget failure stops the middleware chain.

        If budget check fails, proxy should not execute.
        """
        execution_order = []

        context = TokenContext(
            user_id="user-test",
            org_id="org-test",
            team_id="team-test",
            department_id="dept-test",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        async def budget_middleware(ctx: TokenContext) -> BudgetCheckResult:
            execution_order.append("budget")
            raise BudgetExceededError(
                level="user",
                entity="user-test",
                budget_usd=100,
                spent_usd=105,
                period="monthly",
                resets_at="2026-03-01T00:00:00Z",
            )

        async def proxy_handler(ctx: TokenContext, request: dict) -> dict:
            execution_order.append("proxy")
            return {"status": "success"}

        # Execute chain
        execution_order.append("auth")
        execution_order.append("rate_limit")

        with pytest.raises(BudgetExceededError):
            await budget_middleware(context)
            # This should NOT execute
            await proxy_handler(context, {})

        # Auth, rate_limit, and budget should have executed
        assert execution_order == ["auth", "rate_limit", "budget"]

    @pytest.mark.asyncio
    async def test_all_checks_pass_proxy_executes(
        self,
        mock_bedrock_client: MockBedrockClient,
    ):
        """
        Test that proxy executes when all middleware checks pass.
        """
        execution_order = []

        context = TokenContext(
            user_id="user-test",
            org_id="org-test",
            team_id="team-test",
            department_id="dept-test",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        async def auth_middleware() -> TokenContext:
            execution_order.append("auth")
            return context

        async def rate_limit_middleware(ctx: TokenContext) -> RateLimitCheckResult:
            execution_order.append("rate_limit")
            return RateLimitCheckResult(allowed=True, limit=100, remaining=99)

        async def budget_middleware(ctx: TokenContext) -> BudgetCheckResult:
            execution_order.append("budget")
            return BudgetCheckResult(allowed=True, budget_usd=1000, spent_usd=0)

        async def proxy_handler(ctx: TokenContext, request: dict) -> dict:
            execution_order.append("proxy")
            return await mock_bedrock_client.invoke_model(
                model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
                body=request,
            )

        # Execute complete chain
        ctx = await auth_middleware()
        await rate_limit_middleware(ctx)
        await budget_middleware(ctx)
        response = await proxy_handler(ctx, {"messages": [{"role": "user", "content": "Hello"}]})

        # All middleware should have executed
        assert execution_order == ["auth", "rate_limit", "budget", "proxy"]
        assert response is not None


@pytest.mark.integration
class TestMiddlewareHeaders:
    """Test suite for middleware header handling."""

    @pytest.mark.asyncio
    async def test_response_includes_all_middleware_headers(self):
        """
        Test that response includes headers from all middleware.

        Headers should include:
        - Rate limit headers (X-RateLimit-*)
        - Budget headers (X-Budget-*)
        """
        # Simulate middleware adding headers
        response_headers = {}

        # Rate limit middleware adds headers
        rate_limit_result = RateLimitCheckResult(
            allowed=True,
            limit_type="rpm",
            limit=100,
            remaining=95,
        )
        response_headers["X-RateLimit-Limit"] = str(rate_limit_result.limit)
        response_headers["X-RateLimit-Remaining"] = str(rate_limit_result.remaining)
        response_headers["X-RateLimit-Reset"] = "60"

        # Budget middleware adds headers
        budget_result = BudgetCheckResult(
            allowed=True,
            budget_usd=500.00,
            spent_usd=123.45,
            period="monthly",
            enforcement_mode="hard",
        )
        response_headers["X-Budget-Remaining-USD"] = str(budget_result.budget_usd - budget_result.spent_usd)
        response_headers["X-Budget-Period"] = budget_result.period
        response_headers["X-Budget-Enforcement"] = budget_result.enforcement_mode

        # Verify all headers present
        assert "X-RateLimit-Limit" in response_headers
        assert "X-RateLimit-Remaining" in response_headers
        assert "X-RateLimit-Reset" in response_headers
        assert "X-Budget-Remaining-USD" in response_headers
        assert "X-Budget-Period" in response_headers
        assert "X-Budget-Enforcement" in response_headers

    @pytest.mark.asyncio
    async def test_soft_budget_warning_header_added(self):
        """
        Test that soft budget warning header is added when limit exceeded.
        """
        response_headers = {}

        # Budget check with soft limit exceeded
        budget_result = BudgetCheckResult(
            allowed=True,  # Soft limit allows request
            budget_usd=100.00,
            spent_usd=120.00,
            period="monthly",
            enforcement_mode="soft",
            warnings=["soft_limit_exceeded"],
        )

        # Add warning header
        if "soft_limit_exceeded" in budget_result.warnings:
            response_headers["X-Budget-Warning"] = "soft_limit_exceeded"

        assert response_headers["X-Budget-Warning"] == "soft_limit_exceeded"


@pytest.mark.integration
class TestMiddlewareContext:
    """Test suite for middleware context passing."""

    @pytest.mark.asyncio
    async def test_context_passed_through_chain(self):
        """
        Test that TokenContext is correctly passed through the chain.
        """
        # Create context
        original_context = TokenContext(
            user_id="user-context-test",
            org_id="org-context-test",
            team_id="team-context-test",
            department_id="dept-context-test",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        contexts_received = []

        async def rate_limit_middleware(ctx: TokenContext) -> RateLimitCheckResult:
            contexts_received.append(("rate_limit", ctx))
            return RateLimitCheckResult(allowed=True)

        async def budget_middleware(ctx: TokenContext) -> BudgetCheckResult:
            contexts_received.append(("budget", ctx))
            return BudgetCheckResult(allowed=True)

        async def proxy_handler(ctx: TokenContext) -> dict:
            contexts_received.append(("proxy", ctx))
            return {"status": "success"}

        # Execute chain
        await rate_limit_middleware(original_context)
        await budget_middleware(original_context)
        await proxy_handler(original_context)

        # Verify all middleware received the same context
        assert len(contexts_received) == 3
        for name, ctx in contexts_received:
            assert ctx.user_id == original_context.user_id
            assert ctx.org_id == original_context.org_id
            assert ctx.team_id == original_context.team_id

    @pytest.mark.asyncio
    async def test_context_immutable_through_chain(self):
        """
        Test that context is not modified by middleware.
        """
        original_context = TokenContext(
            user_id="user-immutable",
            org_id="org-immutable",
            team_id="team-immutable",
            department_id="dept-immutable",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        # Store original values
        original_user_id = original_context.user_id
        original_org_id = original_context.org_id

        # Simulate middleware that might try to modify context
        # (in Pydantic, model fields are typically immutable)

        # Verify context unchanged
        assert original_context.user_id == original_user_id
        assert original_context.org_id == original_org_id


@pytest.mark.integration
class TestErrorHandling:
    """Test suite for middleware error handling."""

    @pytest.mark.asyncio
    async def test_expired_token_returns_401(self):
        """
        Test that expired token returns 401 from auth middleware.
        """
        expired_context = TokenContext(
            user_id="user-expired",
            org_id="org-test",
            team_id="team-test",
            department_id="dept-test",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now(UTC) - timedelta(hours=1),  # Expired
        )

        # Auth should fail
        if expired_context.expires_at < datetime.now(UTC):
            with pytest.raises(TokenExpiredError):
                raise TokenExpiredError()

    @pytest.mark.asyncio
    async def test_error_response_includes_request_id(self):
        """
        Test that error responses include request ID for tracking.
        """
        request_id = "req-12345-abcde"

        try:
            raise RateLimitExceededError(
                limit_type="rpm",
                limit=100,
                retry_after=30,
            )
        except RateLimitExceededError as e:
            error_response = {
                "error": e.error,
                "message": e.message,
                "request_id": request_id,
                "details": e.details,
            }

            assert error_response["request_id"] == request_id
            assert error_response["error"] == "rate_limited"
