"""A workspace engine run must be readable by the login that approved it."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from src.activity import routes
from src.activity.service import ActivityService
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.identity import resolve_canonical_user_id
from src.shared.identity.workspaces import link_login_to_workspace
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext


async def seed_workspace(db, *, linked=True):
    db.add_all([Organization(id="home", name="Home"), Organization(id="work", name="Work"), Organization(id="other", name="Other")])
    await db.flush()
    home = User(id="home-user", org_id="home", team_id="", email="same@example.test", cognito_sub="login-sub")
    work = User(id="work-user", org_id="work", team_id="", email="same@example.test", cognito_sub=None)
    db.add_all([home, work])
    await db.flush()
    db.add(TenantMembership(user_id=work.id, tenant_id="work", role="member"))
    if linked:
        await link_login_to_workspace(db, home, work)
    await db.commit()


@pytest.mark.parametrize("suffix", ["", "/transcript"])
@pytest.mark.parametrize("org,linked,expected", [("work", True, 200), ("home", True, 404), ("other", True, 404), ("work", False, 404)])
async def test_workspace_run_and_transcript_authorization(db_session, monkeypatch, suffix, org, linked, expected):
    await seed_workspace(db_session, linked=linked)
    table = MagicMock()
    table.query.return_value = {
        "Items": [
            {
                "event_id": "orch:workspace-run",
                "arrived_at": "2026-09-13T22:00:00Z",
                "tenant_id": "work",
                "user_id": "work-user",
                "root_human_id": "work-user",
                "channel": "orchestration",
                "status": "complete",
                "persona": "operations",
                "transcript_key": "operations/verified-report.md",
            }
        ]
    }
    resource = MagicMock()
    resource.Table.return_value = table
    service = ActivityService(dynamodb_resource=resource)
    transcript = AsyncMock(return_value="27 tests passed with recorded evidence")
    monkeypatch.setattr(routes, "_fetch_transcript", transcript)
    app = FastAPI()
    app.include_router(routes.router)
    context = TokenContext(
        user_id="login-sub", org_id=org, team_id="", department_id="", account_type="human", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    app.dependency_overrides[get_current_user] = lambda: context
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[routes.get_activity_service] = lambda: service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/me/agent-invocations/orch:workspace-run" + suffix, params={"user_id": "work-user", "org_id": "work"})
    assert response.status_code == expected, response.text
    if expected == 200:
        if suffix:
            assert response.text == "27 tests passed with recorded evidence"
            transcript.assert_awaited_once_with("operations/verified-report.md")
        else:
            assert response.json()["invocation_id"] == "orch:workspace-run"
    else:
        transcript.assert_not_awaited()


async def test_canonical_resolution_can_select_workspace_without_changing_legacy_callers(db_session):
    await seed_workspace(db_session)
    assert await resolve_canonical_user_id(db_session, "login-sub") == "home-user"
    assert await resolve_canonical_user_id(db_session, "login-sub", org_id="work") == "work-user"
    assert await resolve_canonical_user_id(db_session, "login-sub", org_id="other") == "login-sub"
