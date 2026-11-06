"""Real ORM, HTTP schemas and limiter keys for scoped rate-limit edits."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

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


async def test_campaign_receipt_survives_lost_reply_and_refuses_unrelated_edit(db_session, sample_organizations, caller):
    from src.admin.campaign_quota import CampaignQuotaEdit, edit_campaign_quota, get_campaign_receipt

    org = caller.org_id
    foreign_org = sample_organizations[1].id
    db_session.add(Department(id="fixture-dept", org_id=org, name="fixture-dept"))
    db_session.add(Team(id="fixture-team", org_id=org, department_id="fixture-dept", name="fixture-team"))
    db_session.add(User(id="fixture-user", org_id=org, team_id="fixture-team", email="fixture@example.invalid", cognito_sub="fixture-sub"))
    await db_session.commit()
    operation_id = uuid4()
    body = CampaignQuotaEdit(expected_revision=None, desired={"rpm": 20, "tpm": 200, "concurrent_requests": 2})
    committed = await edit_campaign_quota(org, "fixture-sub", operation_id, body, db_session, caller)
    db_session.expire_all()
    recovered = await get_campaign_receipt(org, "fixture-sub", operation_id, db_session, caller)
    assert recovered == committed and recovered["before"] is None
    assert await edit_campaign_quota(org, "fixture-sub", operation_id, body, db_session, caller) == committed
    revision = datetime.fromisoformat(recovered["after"]["updated_at"])
    changed = await routes.set_config(org, "user", "fixture-sub", RateLimitPatch(rpm=99, expected_revision=revision), db_session, caller)
    restore = CampaignQuotaEdit(expected_revision=revision, desired=None)
    with pytest.raises(HTTPException) as stale:
        await edit_campaign_quota(org, "fixture-sub", uuid4(), restore, db_session, caller)
    assert stale.value.status_code == 409
    assert routes.serialize(await routes.saved(db_session, org, "user", "fixture-sub")) == changed
    with pytest.raises(HTTPException) as reused:
        await edit_campaign_quota(org, "fixture-sub", operation_id, restore, db_session, caller)
    assert reused.value.status_code == 409
    with pytest.raises(HTTPException) as foreign:
        await get_campaign_receipt(foreign_org, "fixture-sub", operation_id, db_session, caller)
    assert foreign.value.status_code == 404


async def test_campaign_http_restore_receipt_and_strict_versions(db_session, sample_organizations, caller):
    from src.admin import campaign_quota

    org = caller.org_id
    db_session.add(Department(id="receipt-dept", org_id=org, name="receipt-dept"))
    db_session.add(Team(id="receipt-team", org_id=org, department_id="receipt-dept", name="receipt-team"))
    db_session.add(User(id="receipt-user", org_id=org, team_id="receipt-team", email="receipt@example.invalid", cognito_sub="receipt-sub"))
    await db_session.commit()
    app = FastAPI()
    app.include_router(campaign_quota.router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: caller
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/organizations/{org}/ratelimit-cli/user/receipt-sub/campaign/"
        operation_id = str(uuid4())
        body = {"expected_revision": None, "desired": {"rpm": 5, "tpm": 50, "concurrent_requests": 1}}
        response = await client.put(path + operation_id, json=body)
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert (await client.get(path + operation_id)).json() == receipt
        stale = {**body, "expected_revision": None}
        assert (await client.put(path + str(uuid4()), json=stale)).status_code == 409
        restore_id = str(uuid4())
        restore = {"expected_revision": receipt["after"]["updated_at"], "desired": None}
        response = await client.put(path + restore_id, json=restore)
        assert response.status_code == 200, response.text
        assert response.json()["after"] is None
        assert (await client.get(path + restore_id)).json() == response.json()
        assert (await client.put(path + restore_id, json=restore)).json() == response.json()


async def test_campaign_adapter_recovers_lost_response_through_real_gateway(db_session, sample_organizations, caller, tmp_path, monkeypatch):
    import asyncio
    import json
    from pathlib import Path
    from unittest.mock import Mock

    from src.admin import campaign_quota

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "tests/performance/gateway"))
    from campaign_adp import ADPFixtureBackend
    from campaign_recovery import RecoveryJournal

    org = caller.org_id
    db_session.add(Department(id="adapter-dept", org_id=org, name="adapter-dept"))
    db_session.add(Team(id="adapter-team", org_id=org, department_id="adapter-dept", name="adapter-team"))
    db_session.add(User(id="adapter-user", org_id=org, team_id="adapter-team", email="adapter@example.invalid", cognito_sub="adapter-sub"))
    await db_session.commit()
    tmp_path.chmod(0o700)
    private = tmp_path / "private-input"
    private.write_text("{}")
    private.chmod(0o600)
    config = {
        "campaign": "adapter-campaign",
        "owner": "adapter-owner",
        "journal_dir": str(tmp_path),
        "target": "https://gateway.example.invalid",
        "gateway_admin_token_file": str(private),
        "aws_connection": "fixture-only",
        "aws_region": "us-east-1",
        "user_pool_id": "fixture-pool",
        "health_file": str(private),
        "health_owner": "fixture-health",
        "fixtures": [
            {
                "kind": "identity",
                "binding": "adapter-sub",
                "username": "adapter-user",
                "tenant": org,
                "root_human": "adapter-sub",
                "ownership_receipt": "owned",
                "desired": {"enabled": True},
            },
            {"kind": "quota", "binding": "adapter-sub", "ownership_receipt": "owned", "desired": {"rpm": 30, "tpm": 300, "concurrent_requests": 3}},
        ],
    }
    app = FastAPI()
    app.include_router(campaign_quota.router, prefix="/admin")
    app.include_router(routes.router, prefix="/admin")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: caller
    loop = asyncio.get_running_loop()
    lost = [True]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=config["target"]) as client:

        def transport(method, route, payload=None):
            response = asyncio.run_coroutine_threadsafe(client.request(method, route, json=payload), loop).result(5)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            if method == "PUT" and lost[0]:
                lost[0] = False
                raise TimeoutError("committed gateway response lost")
            return response.json()

        monkeypatch.setattr(ADPFixtureBackend, "_http", staticmethod(transport))
        monkeypatch.setattr(ADPFixtureBackend, "verify_baseline", Mock(return_value=True))
        backend = ADPFixtureBackend(config)
        journal = RecoveryJournal(tmp_path, config["campaign"], config["owner"], clock=lambda: 100)
        with pytest.raises(TimeoutError):
            await asyncio.to_thread(journal.mutate, backend, "quota", "adapter-sub", config["fixtures"][1]["desired"], "owned")
        assert json.loads(journal.path.read_text())["intents"][0]["state"] == "intent"
        restarted = RecoveryJournal(tmp_path, config["campaign"], config["owner"], clock=lambda: 131, create=False)
        receipt = await asyncio.to_thread(restarted.recover, ADPFixtureBackend(config), 20)
        assert receipt["status"] == "clean" and receipt["restored"] == 1
        assert await routes.saved(db_session, org, "user", "adapter-sub") is None


async def test_target_maps_to_actual_login_key_and_refuses_foreign(db_session, sample_organizations, caller):
    org = sample_organizations[0].id
    db_session.add(Department(id="dept", org_id=org, name="dept"))
    db_session.add(Team(id="team", org_id=org, department_id="dept", name="team"))
    db_session.add(User(id="canonical", org_id=org, team_id="team", email="x@example.invalid", cognito_sub="login-sub"))
    await db_session.commit()
    assert await routes.target(db_session, caller, org, "user", "canonical", Permission.RATELIMIT_READ) == ("login-sub", "canonical")
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


async def test_secondary_membership_primary_team_and_revocation(db_session, sample_organizations, caller, monkeypatch):
    from src.shared.models.onboarding import TenantMembership
    from src.shared.models.organization import TeamMembership

    home, work = [org.id for org in sample_organizations[:2]]
    db_session.add(Department(id="work-dept", org_id=work, name="work"))
    db_session.add(Team(id="work-team", org_id=work, department_id="work-dept", name="work"))
    db_session.add(User(id="multi", org_id=home, team_id="home-team", email="multi@example.invalid", cognito_sub="multi-sub"))
    member = TenantMembership(user_id="multi", tenant_id=work, role="member", is_active=False)
    db_session.add(member)
    db_session.add(TeamMembership(user_id="multi", org_id=work, team_id="work-team", is_primary=True))
    await db_session.commit()
    monkeypatch.setattr(routes.AccessControl, "get_user_role", AsyncMock(return_value=(AdminRole.DEPT_ADMIN, work, "work-dept")))
    monkeypatch.setattr(routes.AccessControl, "check_permission", AsyncMock())
    assert await routes.target(db_session, caller, work, "user", "multi", Permission.RATELIMIT_READ) == ("multi-sub", "multi")
    member.revoked_at = datetime.now(UTC)
    await db_session.commit()
    with pytest.raises(HTTPException) as refused:
        await routes.target(db_session, caller, work, "user", "multi", Permission.RATELIMIT_READ)
    assert refused.value.status_code == 404


async def test_ambiguous_id_subject_refuses_without_500(db_session, sample_organizations, caller):
    org = sample_organizations[0].id
    db_session.add(User(id="shared-id", org_id=org, team_id="", email="one@example.invalid", cognito_sub="one-sub"))
    db_session.add(User(id="second", org_id=org, team_id="", email="two@example.invalid", cognito_sub="shared-id"))
    await db_session.commit()
    with pytest.raises(HTTPException) as refused:
        await routes.target(db_session, caller, org, "user", "shared-id", Permission.RATELIMIT_READ)
    assert refused.value.status_code == 409
