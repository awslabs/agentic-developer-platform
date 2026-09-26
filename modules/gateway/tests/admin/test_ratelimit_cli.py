"""Real ORM, HTTP schemas and limiter keys for scoped rate-limit edits."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from src.admin import ratelimit_cli as routes
from src.admin.config import AdminRole, Permission
from src.admin.ratelimit_cli import RateLimitPatch
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.organization import Department, Team, User
from src.shared.schemas.auth import TokenContext


@pytest.fixture
def caller(sample_organizations):
    return TokenContext(
        user_id="caller",
        org_id=sample_organizations[0].id,
        team_id="",
        department_id="",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.mark.parametrize("value", [0, -1, 2147483648, True, 1.5, "2"])
def test_invalid_dimensions(value):
    with pytest.raises(ValidationError):
        RateLimitPatch(rpm=value, expect_absent=True)


def test_schema_preserves_omitted_vs_unset():
    patch = RateLimitPatch(rpm=None, expect_absent=True)
    assert patch.model_fields_set.intersection(routes.DIMENSIONS) == {"rpm"}
    for body in [{"expect_absent": True}, {"rpm": 1}, {"rpm": 1, "expected_revision": "2026-09-25T12:00:00"}]:
        with pytest.raises(ValidationError):
            RateLimitPatch.model_validate(body)


async def test_real_rows_patch_clear_stale_revision_and_delete(db_session, sample_organizations, caller):
    org = sample_organizations[0].id
    row = await routes.set_config(org, "org", org, RateLimitPatch(rpm=20, tpm=200, concurrent_requests=2, expect_absent=True), db_session, caller)
    revision = datetime.fromisoformat(row["updated_at"])
    changed = await routes.set_config(org, "org", org, RateLimitPatch(rpm=None, expected_revision=revision), db_session, caller)
    assert changed["rpm"] is None and changed["tpm"] == 200 and changed["concurrent_requests"] == 2
    with pytest.raises(HTTPException) as stale:
        await routes.set_config(org, "org", org, RateLimitPatch(tpm=999, expected_revision=revision), db_session, caller)
    assert stale.value.status_code == 409
    replay = await routes.set_config(org, "org", org, RateLimitPatch(rpm=None, expected_revision=revision), db_session, caller)
    assert replay == changed
    await routes.delete_config(org, "org", org, datetime.fromisoformat(changed["updated_at"]), db_session, caller)
    assert await routes.saved(db_session, org, "org", org) is None


async def test_target_maps_to_actual_login_key_and_refuses_foreign(db_session, sample_organizations, caller):
    org = sample_organizations[0].id
    db_session.add(Department(id="dept", org_id=org, name="dept"))
    db_session.add(Team(id="team", org_id=org, department_id="dept", name="team"))
    db_session.add(User(id="canonical", org_id=org, team_id="team", email="x@example.invalid", cognito_sub="login-sub"))
    await db_session.commit()
    assert await routes.target(db_session, caller, org, "user", "canonical", Permission.RATELIMIT_READ) == "login-sub"
    with pytest.raises(HTTPException) as foreign:
        await routes.target(db_session, caller, sample_organizations[1].id, "user", "canonical", Permission.RATELIMIT_READ)
    assert foreign.value.status_code == 404


async def test_department_admin_cannot_read_org(db_session, sample_organizations, caller, monkeypatch):
    monkeypatch.setattr(routes.AccessControl, "get_user_role", AsyncMock(return_value=(AdminRole.DEPT_ADMIN, caller.org_id, "dept")))
    monkeypatch.setattr(routes.AccessControl, "check_permission", AsyncMock())
    with pytest.raises(HTTPException) as refused:
        await routes.target(db_session, caller, caller.org_id, "org", caller.org_id, Permission.RATELIMIT_READ)
    assert refused.value.status_code == 403


async def test_real_http_schema_and_own_status_no_counter_mutation(db_session, sample_organizations, caller):
    from src.ratelimit.backends.in_memory import InMemoryBackend
    from src.ratelimit.config import RateLimitConfig
    from src.ratelimit.routes import router as own_router
    from src.ratelimit.service import RateLimitService

    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(own_router)
    app.state.ratelimit_service = RateLimitService(backend=InMemoryBackend(), config=RateLimitConfig())
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: caller
    app.state.ratelimit_service._backend.get_bucket_tokens = AsyncMock(side_effect=AssertionError("No bucket probing"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ratelimits/me")
        assert response.status_code == 200
        data = response.json()
        assert data["lines"][0]["entity_id"] == caller.user_id
        assert data["runtime"]["quota_storage"] == "process_local"
        assert data["runtime"]["tpm"] == "unavailable_actual_usage_not_reconciled"
        assert data["lines"][0]["sources"]["rpm"] == "account_type_default"
        response = await client.get(f"/organizations/{caller.org_id}/ratelimit-cli/org/{caller.org_id}")
        assert response.status_code == 200 and response.json()["saved"] is None
        path = f"/organizations/{caller.org_id}/ratelimit-cli/org/{caller.org_id}"
        response = await client.put(path, json={"rpm": 7, "concurrent_requests": 2, "expect_absent": True})
        assert response.status_code == 200, response.text
        revision = response.json()["updated_at"]
        response = await client.put(path, json={"rpm": None, "expected_revision": revision})
        assert response.status_code == 200, response.text
        assert response.json()["rpm"] is None and response.json()["concurrent_requests"] == 2
        response = await client.put(path, json={"rpm": 8, "expected_revision": revision})
        assert response.status_code == 409
        # The real OpenAPI request schema preserves strict bounds and closed fields.
        schema = app.openapi()["components"]["schemas"]["RateLimitPatch"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["rpm"]["anyOf"][0]["maximum"] == 2147483647


def test_unavailable_runtime_is_honest():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    assert routes.runtime_metadata(request)["state"] == "unavailable"


async def test_legacy_org_alias_and_zero_are_readable(db_session, sample_organizations):
    from src.shared.models.usage import RateLimitConfig

    org = sample_organizations[0].id
    db_session.add(RateLimitConfig(org_id=org, entity_type="organization", entity_id=org, rpm=0))
    await db_session.commit()
    row = routes.serialize(await routes.saved(db_session, org, "org", org))
    assert row["entity_type"] == "org" and row["rpm"] == 0
    db_session.add(RateLimitConfig(org_id=org, entity_type="org", entity_id=org, rpm=10))
    await db_session.commit()
    with pytest.raises(HTTPException) as duplicate:
        await routes.saved(db_session, org, "org", org)
    assert duplicate.value.status_code == 409
