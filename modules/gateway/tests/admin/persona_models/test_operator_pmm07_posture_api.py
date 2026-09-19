"""Operator review of the actual HTTP mutation boundary; external auth is injected."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from src.admin.persona_models import posture_routes
from src.agentauth.runtime_posture import reset_posture_cache
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.organization import User
from src.shared.models.persona_models import PersonaModelPolicySetting
from src.shared.schemas.auth import TokenContext


async def call_mutation(db, *, registered: bool, revision):
    reset_posture_cache()
    db.add(PersonaModelPolicySetting(compatibility_class="claude-agent-sdk", enforcement_posture="report_only", posture_revision=1, revision=1))
    if registered:
        db.add(User(id="canonical-admin", org_id="tenant-a", team_id="team-a", email="admin@example.test", cognito_sub="signed-admin-sub"))
    await db.commit()
    context = TokenContext(
        user_id="signed-admin-sub",
        org_id="tenant-a",
        team_id="team-a",
        department_id="",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="jwt",
    )
    app = FastAPI()
    app.include_router(posture_routes.router)

    async def database():
        yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: context
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put("/admin/persona-models/posture/claude-agent-sdk", json={"posture": "enforcing", "expected_revision": revision})
    return response


async def test_unresolvable_actor_cannot_mutate_or_be_written_as_raw_subject(db_session):
    response = await call_mutation(db_session, registered=False, revision=1)
    assert response.status_code in (403, 422), response.json()
    setting = await db_session.get(PersonaModelPolicySetting, "claude-agent-sdk")
    assert (setting.enforcement_posture, setting.posture_revision) == ("report_only", 1)
    audits = (await db_session.scalars(select(AuditLog))).all()
    assert all(a.actor_id != "signed-admin-sub" for a in audits)


@pytest.mark.parametrize("revision", [True, 1.0, "1", None, 0, -1])
async def test_http_revision_is_a_strict_positive_integer(db_session, revision):
    response = await call_mutation(db_session, registered=True, revision=revision)
    assert response.status_code == 422, response.json()
    setting = await db_session.get(PersonaModelPolicySetting, "claude-agent-sdk")
    assert (setting.enforcement_posture, setting.posture_revision) == ("report_only", 1)


async def test_registered_admin_integer_revision_still_commits_with_canonical_audit(db_session):
    response = await call_mutation(db_session, registered=True, revision=1)
    assert response.status_code == 200, response.json()
    assert response.json()["updated_by"] == "canonical-admin"
    audits = (await db_session.scalars(select(AuditLog))).all()
    assert [a.actor_id for a in audits] == ["canonical-admin"]
