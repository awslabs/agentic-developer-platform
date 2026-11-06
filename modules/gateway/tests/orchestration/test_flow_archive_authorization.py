"""Archive checks real tenant-scoped permissions without weakening list reads."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole
from src.auth.dependencies import get_current_user
from src.orchestration.models import OrchestrationDecision
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from tests.orchestration import test_flow_controls as control_fixtures
from tests.orchestration import test_list_flows as list_fixtures
from tests.orchestration.test_controls import ORG_A, _token_context, seed_flow

session = control_fixtures.session
app_with_router = list_fixtures.app_with_router


@pytest.fixture
async def authorized_app(app_with_router, session, monkeypatch):
    context = _token_context(ORG_A)
    session.add(Organization(id=ORG_A, name="Example workspace"))
    await session.flush()
    session.add(User(id="human-1", cognito_sub=context.user_id, org_id=ORG_A, team_id="", email="member@example.test"))
    await session.flush()
    session.add(TenantMembership(user_id="human-1", tenant_id=ORG_A, role="member"))
    await session.flush()
    role = AsyncMock(return_value=(AdminRole.MEMBER, ORG_A, None))
    monkeypatch.setattr(AccessControl, "get_user_role", role)
    resolve = AsyncMock(return_value="human-1")
    membership = AsyncMock()
    monkeypatch.setattr("src.agentauth.human_control.resolve_canonical_user_id", resolve)
    monkeypatch.setattr("src.agentauth.human_control.require_live_human_membership", membership)
    app_with_router.dependency_overrides[get_current_user] = lambda: _token_context(ORG_A).model_copy(update={"auth_source": "jwt"})
    return app_with_router, role, membership


@pytest.mark.parametrize("action", ["archive", "unarchive"])
async def test_workspace_member_can_list_and_archive(session, authorized_app, action):
    app, role, membership = authorized_app
    flow = await seed_flow(session)
    await session.commit()

    with TestClient(app) as client:
        listed = client.get("/orchestration/flows")
        result = client.post(f"/orchestration/flows/{flow.id}/{action}", json={"reason": "No active work remains"})

    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["flows"]] == [flow.id]
    assert result.status_code == 200, result.text
    membership.assert_awaited_once()
    await session.refresh(flow)
    assert (flow.archived_at is not None) == (action == "archive")
    decisions = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id)))
    assert [row.kind for row in decisions] == (["flow_archived"] if action == "archive" else [])


@pytest.mark.parametrize("action", ["archive", "unarchive"])
async def test_revoked_workspace_member_cannot_archive(session, authorized_app, action):
    from datetime import UTC, datetime

    app, _, membership_check = authorized_app
    flow = await seed_flow(session)
    membership = await session.scalar(select(TenantMembership))
    membership.revoked_at = datetime.now(UTC)
    await session.commit()
    with TestClient(app) as client:
        result = client.post(f"/orchestration/flows/{flow.id}/{action}", json={"reason": "No active work remains"})
    assert result.status_code == 403
    membership_check.assert_not_awaited()
    assert list(await session.scalars(select(OrchestrationDecision))) == []


@pytest.mark.parametrize("action", ["archive", "unarchive"])
async def test_foreign_and_missing_flow_ids_both_return_404_without_writing(session, authorized_app, action):
    app, role, membership = authorized_app
    role.return_value = (AdminRole.ORG_ADMIN, ORG_A, None)
    foreign = await seed_flow(session, org_id="another-tenant", slug="foreign-flow")
    own = await seed_flow(session, slug="own-flow")
    await session.commit()

    with TestClient(app) as client:
        listing = client.get("/orchestration/flows")
        responses = [
            client.post(
                f"/orchestration/flows/{flow_id}/{action}",
                headers={"X-Org-ID": "another-tenant"},
                json={"reason": "No active work remains"},
            )
            for flow_id in (foreign.id, "missing-flow")
        ]

    assert listing.status_code == 200, listing.text
    assert [row["id"] for row in listing.json()["flows"]] == [own.id]
    assert [response.status_code for response in responses] == [404, 404]
    assert membership.await_count == 2
    await session.refresh(foreign)
    assert foreign.archived_at is None
    assert list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == foreign.id))) == []
