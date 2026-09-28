"""Exercise admission with fresh ASGI scopes and repeated caller correlation IDs."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.shared.middleware.request_identity import RequestIdentityMiddleware
from src.shared.schemas.auth import TokenContext


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,started,release", [(429, False, True), (422, False, True), (200, False, True), (502, True, False), (200, True, False)]
)
async def test_admission_identity_and_preprovider_release(status, started, release):
    context = TokenContext(
        org_id="tenant", user_id="owner", account_type="human", role="member", team_id="team", department_id="dept", expires_at=datetime.now(UTC)
    )
    service = SimpleNamespace(
        prepare_enforcement_context=AsyncMock(return_value=None),
        check_budget_hierarchy=AsyncMock(return_value=SimpleNamespace(allowed=True)),
        reconcile_reservation=AsyncMock(),
    )
    received_ids = []

    async def endpoint(scope, receive, send):
        received_ids.append(scope["state"]["request_id"])
        context._budget_provider_started = started
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    budget = BudgetEnforcementMiddleware(endpoint, enforcement_service=service)

    async def authenticated(scope, receive, send):
        scope["state"]["token_context"] = context
        await budget(scope, receive, send)

    app = RequestIdentityMiddleware(authenticated)
    for _ in range(2):
        context._budget_provider_started = False
        scope = {"type": "http", "method": "POST", "path": "/v1/messages", "headers": [(b"x-request-id", b"reused-client-id")]}
        messages = []

        async def send(message):
            messages.append(message)

        await app(scope, AsyncMock(return_value={"type": "http.request", "body": b""}), send)
        request_id = scope["state"]["request_id"]
        assert UUID(request_id)
        assert scope["state"]["client_request_id"] == "reused-client-id"
        assert dict(messages[0]["headers"])[b"x-request-id"].decode() == request_id
        assert service.check_budget_hierarchy.await_args.kwargs["request_id"] == request_id
    assert len(set(received_ids)) == 2
    assert service.reconcile_reservation.await_count == (2 if release else 0)


def test_production_identity_wraps_auth_and_admission():
    from src.app import create_app

    app = create_app()
    names = [middleware.cls.__name__ for middleware in app.user_middleware]
    assert names[0] == "RequestIdentityMiddleware"
    assert names.index("RequestIdentityMiddleware") < names.index("TokenContextMiddleware") < names.index("BudgetEnforcementMiddleware")
    assert not BudgetEnforcementMiddleware(None)._should_enforce("/v1/messages/count_tokens")
