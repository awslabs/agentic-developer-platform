"""Real policy SQL, Redis Lua and both production ASGI middleware boundaries."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from src.agentauth.model_identity import AgentModelIdentityMiddleware
from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.budget.enforcement_service import BudgetEnforcementService
from src.orchestration.flow_budget import get_flow_reservations
from src.orchestration.flow_meter import meter_target, read_flow_meter
from src.orchestration.policy_admission import load_in_force_policy
from src.shared.schemas.auth import TokenContext
from tests.orchestration.test_runtime_policy import (
    assignment as assignment_fixture,
)
from tests.orchestration.test_runtime_policy import (
    engine as engine_fixture,
)
from tests.orchestration.test_runtime_policy import (
    healthy_policy_reservations as reservations_fixture,
)
from tests.orchestration.test_runtime_policy import (
    policy_budget_initializers as initializers_fixture,
)
from tests.orchestration.test_runtime_policy import (
    session as session_fixture,
)

engine, session, assignment = engine_fixture, session_fixture, assignment_fixture
healthy_policy_reservations, policy_budget_initializers = reservations_fixture, initializers_fixture


@pytest.fixture
async def model_path(session, assignment, monkeypatch):
    @asynccontextmanager
    async def sessions():
        yield session

    caller = SimpleNamespace(tenant_id=assignment.grant.tenant_id, invocation_id="worker")
    runtime = SimpleNamespace(
        authenticate=lambda *_: (None, caller, None, assignment.grant),
        validate_flow=AsyncMock(),
        store=SimpleNamespace(_read=lambda *_: assignment.execution),
    )
    monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: sessions)
    service = BudgetEnforcementService()
    service._reservations = get_flow_reservations()
    monkeypatch.setattr(service, "_get_session", sessions)
    monkeypatch.setattr(service, "_note_check_succeeded", AsyncMock())
    inputs = await load_in_force_policy(session, org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id)
    return SimpleNamespace(service=service, policy=inputs.policy, calls=0, bodies=[], runtime=runtime)


async def invoke(model_path, assignment, *, request_id="call", usage_known=True, during_upload=None):
    body = b'{ "model": "anthropic.claude-sonnet-4-6", "max_tokens": 16, "messages": [{"role":"user","content":"hello"}] }'
    context = TokenContext(
        user_id="authority-worker",
        org_id="__platform__",
        team_id="",
        department_id="",
        account_type="service",
        auth_source="iam",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/messages",
        "state": {"token_context": context, "request_id": request_id},
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
    }
    frames = [{"type": "http.request", "body": body[:13], "more_body": True}, {"type": "http.request", "body": body[13:], "more_body": False}]
    sent = []

    async def receive():
        if during_upload is not None:
            await during_upload()
        return frames.pop(0)

    async def send(frame):
        sent.append(frame)

    async def provider(scope, receive, send):
        model_path.calls += 1
        model_path.bodies.append(await Request(scope, receive).body())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await model_path.service.reconcile_reservation(
            context, request_id, "anthropic.claude-sonnet-4-6", 5, 1, actual_cost_usd=Decimal("0.01"), usage_known=usage_known
        )
        await send({"type": "http.response.body", "body": b"second", "more_body": False})

    await AgentModelIdentityMiddleware(BudgetEnforcementMiddleware(provider, model_path.service))(scope, receive, send)
    return sent, body


async def test_first_model_call_before_any_usage_row_succeeds_and_preserves_stream(model_path, assignment):
    sent, body = await invoke(model_path, assignment)
    assert sent[0]["status"] == 200
    assert [frame.get("body") for frame in sent[1:]] == [b"first", b"second"]
    assert model_path.bodies == [body]
    meter = await read_flow_meter(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
    assert meter.total_usd == Decimal("0.01") and not meter.has_pending


async def test_exhausted_shared_budget_never_reaches_provider(model_path, assignment):
    target = meter_target(org_id=assignment.grant.tenant_id, flow_id=assignment.flow.id, policy=model_path.policy)
    await get_flow_reservations().reserve("earlier", model_path.policy.limits.max_spend_usd - Decimal("0.001"), [target])
    sent, _ = await invoke(model_path, assignment)
    assert sent[0]["status"] == 402
    assert model_path.calls == 0


async def test_missing_usage_blocks_next_model_call_until_receipt(model_path, assignment):
    assert (await invoke(model_path, assignment, usage_known=False))[0][0]["status"] == 200
    assert (await invoke(model_path, assignment, request_id="next"))[0][0]["status"] == 503
    assert model_path.calls == 1


async def test_policy_expiry_during_upload_prevents_spending(model_path, assignment, monkeypatch):
    async def expire():
        monkeypatch.setattr("src.orchestration.runtime_policy.utcnow", lambda: datetime.now(UTC) + timedelta(days=100))

    sent, _ = await invoke(model_path, assignment, during_upload=expire)
    assert sent[0]["status"] == 403
    assert model_path.calls == 0
