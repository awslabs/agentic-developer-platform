"""Tenant-scoped human controls preserve graph progress and retry allowances."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from src.admin.config import AdminRole, Permission
from src.agentauth.bootstrap import BootstrapRefusedError
from src.auth.dependencies import get_current_user
from src.orchestration.flow_controls import router
from src.orchestration.models import OrchestrationDecision, OrchestrationNode
from src.shared.database import get_db
from tests.orchestration import test_controls as fixtures
from tests.orchestration.test_controls import (
    ORG_A,
    _token_context,
    seed_flow,
)

session = fixtures.session


@pytest.fixture
async def controls(session, monkeypatch):
    app = FastAPI()
    app.include_router(router, prefix="/orchestration")
    context = _token_context(ORG_A).model_copy(update={"auth_source": "jwt"})
    access = MagicMock()
    access.check_permission = AsyncMock()
    access.get_user_role = AsyncMock(return_value=(AdminRole.ORG_ADMIN, ORG_A, None))
    monkeypatch.setattr("src.orchestration.flow_controls.AccessControl", lambda db: access)
    # Exercise real session-type/expiry validation; isolate live identity lookup.
    monkeypatch.setattr("src.agentauth.human_control.resolve_canonical_user_id", AsyncMock(return_value="human-1"))
    membership = AsyncMock()
    monkeypatch.setattr("src.agentauth.human_control.require_live_human_membership", membership)
    app.dependency_overrides[get_current_user] = lambda: context
    app.dependency_overrides[get_db] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        yield client, context, access, membership


async def test_default_paused_idempotent_toggle_and_preserved_progress(session, controls):
    client, _, access, _ = controls
    flow = await seed_flow(session)
    node = OrchestrationNode(
        org_id=ORG_A, flow_id=flow.id, epic_ref="E", wave_ref="W", node_ref="S", kind="story", title="Done", state="passed", attempts=3
    )
    session.add(node)
    await session.commit()
    assert flow.execution_paused is True
    route = f"/orchestration/flows/{flow.id}/execution"
    for paused in [False, False, True, True]:
        response = await client.post(route, json={"paused": paused})
        assert response.status_code == 200, response.text
        assert response.json() == {"flow_id": flow.id, "execution_paused": paused}
    await session.refresh(flow)
    await session.refresh(node)
    assert flow.execution_paused is True
    assert (node.state, node.attempts) == ("passed", 3)
    decisions = list(
        await session.scalars(
            select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id).order_by(OrchestrationDecision.created_at)
        )
    )
    assert [d.kind for d in decisions] == ["flow_resumed", "flow_paused"]
    assert all(d.actor_id == "human-1" and d.actor_kind == "human" and d.actor_role == "org_admin" for d in decisions)
    assert access.check_permission.call_args.args[1] == Permission.PLAN_APPROVE
    assert access.check_permission.call_args.kwargs["target_org_id"] == ORG_A


@pytest.mark.parametrize(
    "body", [{"paused": "false"}, {"paused": 0}, {"paused": False, "actor_kind": "human"}, {"paused": False, "reset_attempts": True}]
)
async def test_control_accepts_only_a_boolean(session, controls, body):
    flow = await seed_flow(session)
    response = await controls[0].post(f"/orchestration/flows/{flow.id}/execution", json=body)
    assert response.status_code == 422
    assert flow.execution_paused is True


@pytest.mark.parametrize("case", ["permission", "service", "iam", "expired", "membership"])
async def test_unauthorized_control_is_refused(session, controls, case):
    from datetime import UTC, datetime, timedelta

    client, context, access, membership = controls
    flow = await seed_flow(session)
    if case == "permission":
        access.check_permission.side_effect = HTTPException(403, "Permission denied")
    elif case == "service":
        context.account_type = "service"
    elif case == "iam":
        context.auth_source = "iam"
    elif case == "expired":
        context.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        membership.side_effect = BootstrapRefusedError("membership removed")
    response = await client.post(f"/orchestration/flows/{flow.id}/execution", json={"paused": False})
    assert response.status_code == 403, response.text
    assert flow.execution_paused is True
    assert list(await session.scalars(select(OrchestrationDecision))) == []


async def test_another_tenants_flow_is_not_disclosed_or_changed(session, controls):
    flow = await seed_flow(session, org_id="other-tenant")
    for flow_id in [flow.id, "unknown-flow"]:
        response = await controls[0].post(f"/orchestration/flows/{flow_id}/execution", json={"paused": False})
        assert response.status_code == 404
    await session.refresh(flow)
    assert flow.execution_paused is True
