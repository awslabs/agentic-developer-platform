"""
End-to-end denial behaviour when the budget check itself fails (Issue #4075).

Two distinct outcomes must be distinguishable to the caller and to whoever is
on-call at 3am:

  * a real cap  → ``402 budget_exceeded`` (deliberately non-retryable; the AWS
    SDK does not retry 402, which is correct — more money is not coming)
  * a check failure → ``503 budget_check_unavailable`` + ``Retry-After``
    (retryable; the ledger is temporarily unreadable, nothing is over budget)

Pre-fix the middleware hardcoded ``402 budget_exceeded`` for every denial, so
under fail-closed a DB outage would present platform-wide as "budget exceeded"
with a null budget and null spend — operators misdiagnose it as billing, clients
cannot self-recover, and every dashboard built on 402 rates is corrupted.

T2 is the #4068 gate test: it asserts the downstream app was NEVER invoked,
which is what makes it a real gate (it cannot pass with the enforcement branch
deleted).
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from src.budget.config import BudgetConfig
from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.budget.enforcement_service import BudgetEnforcementService
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EnforcementMode, EnforcementResult, EntityType


@pytest.fixture
def token_context():
    return TokenContext(
        user_id="user-123",
        org_id="org-456",
        team_id="team-789",
        department_id="dept-012",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


class _Harness:
    """Drives the pure-ASGI middleware and records what happened."""

    def __init__(self, result: EnforcementResult | None = None, *, service: BudgetEnforcementService | None = None):
        self.app_invoked = False
        self.messages: list[dict] = []
        if service is None:
            service = BudgetEnforcementService()
            service.check_budget_hierarchy = AsyncMock(return_value=result)  # type: ignore[method-assign]
        self.middleware = BudgetEnforcementMiddleware(self._inner_app, enforcement_service=service)

    async def _inner_app(self, scope, receive, send):
        self.app_invoked = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"message":"success"}'})

    async def post(self, path: str = "/v1/chat/completions", *, token_context: TokenContext) -> None:
        scope = {
            "type": "http",
            "path": path,
            "method": "POST",
            "headers": [],
            "state": {"token_context": token_context},
        }

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message):
            self.messages.append(message)

        await self.middleware(scope, receive, send)

    @property
    def status(self) -> int:
        return next(m["status"] for m in self.messages if m["type"] == "http.response.start")

    @property
    def headers(self) -> dict[str, str]:
        start = next(m for m in self.messages if m["type"] == "http.response.start")
        return {k.decode().lower(): v.decode() for k, v in start["headers"]}

    @property
    def body(self) -> dict:
        raw = b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.response.body")
        return json.loads(raw)


class TestCheckUnavailableDenial:
    """T2 (GATE) — a failed check denies, and says why honestly."""

    @pytest.mark.asyncio
    async def test_check_failure_denies_without_reaching_the_app(self, token_context):
        """T2 (GATE): a real DB fault blocks the request end-to-end.

        This drives the *actual* service (not a stubbed result) through the
        real middleware, so it exercises the whole path the shipped default
        governs. Pre-fix the check returns ``allowed=True`` on error, so the
        app IS invoked and the client gets 200 — both assertions fail.
        """
        from sqlalchemy.exc import OperationalError

        service = BudgetEnforcementService()
        config = BudgetConfig()
        object.__setattr__(config, "budget_fail_open_grace_seconds", 0)

        harness = _Harness(service=service)
        db_error = OperationalError("SELECT 1", {}, Exception("connection refused"))

        with patch.object(service, "_get_session", side_effect=db_error):
            with patch("src.budget.enforcement_service.budget_config", config):
                await harness.post(token_context=token_context)

        assert harness.app_invoked is False, "a request that failed the budget check must not reach the model"
        assert harness.status == 503

    @pytest.mark.asyncio
    async def test_check_failure_is_retryable_and_not_labelled_as_billing(self, token_context):
        """A DB outage must not be reported to the caller as 'budget exceeded'."""
        harness = _Harness(
            EnforcementResult(
                allowed=False,
                deny_reason="check_unavailable",
                blocked_reason="Budget check failed: connection refused",
            )
        )

        await harness.post(token_context=token_context)

        assert harness.status == 503
        assert harness.body["error"] == "budget_check_unavailable"
        assert harness.headers["retry-after"] == "5", "the client must be told to retry soon, not in an hour"
        # A null budget/spend reported under a 402 is what makes operators
        # chase a billing problem during a database incident.
        assert "x-budget-remaining" not in harness.headers


class TestRealCapDenialUnchanged:
    """Regression — a genuine cap must keep its existing contract."""

    @pytest.mark.asyncio
    async def test_real_cap_still_returns_402_with_budget_details(self, token_context):
        """A real cap keeps 402 + the details clients already parse."""
        harness = _Harness(
            EnforcementResult(
                allowed=False,
                deny_reason="budget_exceeded",
                blocked_reason="Budget exceeded for org org-456",
                exceeded_entity_type=EntityType.ORGANIZATION,
                exceeded_entity_id="org-456",
                budget_amount_usd=Decimal("10.00"),
                current_spend_usd=Decimal("10.50"),
                enforcement_mode=EnforcementMode.HARD,
            )
        )

        await harness.post(token_context=token_context)

        assert harness.app_invoked is False
        assert harness.status == 402
        assert harness.body["error"] == "budget_exceeded"
        assert harness.body["details"]["budget_usd"] == 10.0
        assert harness.body["details"]["spent_usd"] == 10.5
        assert harness.headers["x-budget-remaining"] == "0"
        assert harness.headers["x-budget-limit"] == "10.00"

    @pytest.mark.asyncio
    async def test_denial_without_an_explicit_reason_defaults_to_402(self, token_context):
        """Back-compat: older denial results carry no deny_reason.

        Any construction site that predates the discriminator must keep
        producing the cap response rather than silently becoming a 503.
        """
        harness = _Harness(EnforcementResult(allowed=False, blocked_reason="Budget exceeded for user user-123"))

        await harness.post(token_context=token_context)

        assert harness.status == 402

    @pytest.mark.asyncio
    async def test_in_budget_request_reaches_the_app(self, token_context):
        """Healthy traffic is untouched."""
        harness = _Harness(EnforcementResult(allowed=True))

        await harness.post(token_context=token_context)

        assert harness.app_invoked is True
        assert harness.status == 200
