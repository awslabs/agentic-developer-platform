"""Offline Product launch lifecycle through the mounted gateway entry point."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi import HTTPException
from l5_services.adapters.devops import federation
from l5_services.models.base import Base as L5Base

from src import l5_integration
from src.admin.membership_revocation import revoke_membership
from src.auth import native_launch_revocation as revocation
from src.auth.dependencies import get_current_user
from src.auth.middleware import get_current_user_context
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member
from tests.products.test_i05 import REQUEST


@pytest.fixture
async def lifecycle_fixture(app, test_client, test_engine, db_session, valid_token_context, monkeypatch):
    instant = datetime.now(UTC).replace(microsecond=0)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant

    monkeypatch.setattr(l5_integration, "datetime", FixedDateTime)
    monkeypatch.setattr(revocation, "datetime", FixedDateTime)
    revoked = {}

    async def get_revocation(key):
        return revoked.get(key)

    async def set_revocation(key, value, ex):
        revoked[key] = value
        return True

    store = SimpleNamespace(get=AsyncMock(side_effect=get_revocation), set=AsyncMock(side_effect=set_revocation))
    monkeypatch.setattr(
        revocation,
        "get_ratelimit_config",
        lambda: SimpleNamespace(
            backend_type="redis",
            redis_url="redis://offline.invalid",
        ),
    )
    monkeypatch.setattr(revocation, "_redis", lambda url: store)
    context = {
        "actor": valid_token_context.model_copy(
            update={
                "token_use": "access",
                "expires_at": instant + timedelta(hours=1),
            }
        )
    }
    app.dependency_overrides[get_current_user] = lambda: context["actor"]
    app.dependency_overrides[get_current_user_context] = lambda: context["actor"]
    db_session.add(Organization(id=valid_token_context.org_id, name="Offline workspace"))
    db_session.add(
        User(
            id=valid_token_context.user_id,
            org_id=valid_token_context.org_id,
            team_id=valid_token_context.team_id,
            email="member@example.test",
            user_kind="human",
        )
    )
    await add_team_member(db_session, valid_token_context)
    await db_session.commit()
    async with test_engine.begin() as connection:
        await connection.run_sync(L5Base.metadata.create_all)
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_lifecycle_fixture_0001"})
    assert created.status_code == 201, created.text
    limiter = AsyncMock(return_value=SimpleNamespace(allowed=True))
    monkeypatch.setattr(app.state.ratelimit_service, "consume_tool_rate_limit", limiter)
    binding = AsyncMock(side_effect=HTTPException(404, detail={"code": "not_found"}))
    provider = AsyncMock()
    monkeypatch.setattr(l5_integration.ConfigurationStore, "current", binding)
    monkeypatch.setattr(federation, "mint_browser_launch", provider)
    return SimpleNamespace(
        instant=instant,
        context=context,
        binding=binding,
        provider=provider,
        store=store,
        product_id=created.json()["product_id"],
        project_id=created.json()["project_id"],
    )


def access_token(expires_at):
    return jwt.encode({"sub": "offline-person", "exp": expires_at}, "offline-scenario-key-not-production-0123", algorithm="HS256")


@pytest.mark.asyncio
async def test_session_lifecycle(test_client, db_session, lifecycle_fixture):
    fixture = lifecycle_fixture
    path = f"/v1/products/{fixture.product_id}/projects/{fixture.project_id}/services/devops/bindings/binding/browser-launch"

    async def launch(expires_at):
        return await test_client.post(path, json={"configuration_revision": 1}, headers={"Authorization": "Bearer " + access_token(expires_at)})

    fixture.context["actor"] = fixture.context["actor"].model_copy(
        update={
            "expires_at": fixture.instant - timedelta(seconds=1),
        }
    )
    assert (await launch(fixture.instant - timedelta(seconds=1))).status_code == 401
    fixture.binding.assert_not_awaited()
    fixture.store.get.assert_not_awaited()

    fixture.context["actor"] = fixture.context["actor"].model_copy(
        update={
            "expires_at": fixture.instant + timedelta(hours=1),
        }
    )
    refreshed_token = access_token(fixture.instant + timedelta(hours=1))
    available = await test_client.post(path, json={"configuration_revision": 1}, headers={"Authorization": "Bearer " + refreshed_token})
    assert available.status_code == 404
    fixture.binding.assert_awaited_once()

    logout = await test_client.post("/auth/logout", headers={"Authorization": "Bearer " + refreshed_token})
    assert logout.status_code == 200
    assert fixture.store.set.await_count == 1
    assert (
        await test_client.post(path, json={"configuration_revision": 1}, headers={"Authorization": "Bearer " + refreshed_token})
    ).status_code == 401
    assert fixture.binding.await_count == 1

    await revoke_membership(db_session, org_id=fixture.context["actor"].org_id, user_id=fixture.context["actor"].user_id)
    assert (await launch(fixture.instant + timedelta(hours=2))).status_code == 404
    assert fixture.binding.await_count == 1
    fixture.provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_project_bindings_and_action_overrides_stop_before_provider(test_client, lifecycle_fixture):
    fixture = lifecycle_fixture
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "create_second_project_0001"})
    assert created.status_code == 201, created.text
    other_product = created.json()["product_id"]
    other_project = created.json()["project_id"]
    authorization = {"Authorization": "Bearer " + access_token(fixture.instant + timedelta(hours=1))}

    def path(product_id, project_id):
        return f"/v1/products/{product_id}/projects/{project_id}/services/devops/bindings/binding/browser-launch"

    for product_id, project_id in ((fixture.product_id, fixture.project_id), (other_product, other_project)):
        own = await test_client.post(path(product_id, project_id), headers=authorization, json={"configuration_revision": 1})
        assert own.status_code == 404
    assert fixture.binding.await_count == 2
    assert [awaited.args[:2] for awaited in fixture.binding.await_args_list] == [
        (fixture.product_id, fixture.project_id),
        (other_product, other_project),
    ]

    for product_id, project_id in ((fixture.product_id, other_project), (other_product, fixture.project_id)):
        foreign = await test_client.post(path(product_id, project_id), headers=authorization, json={"configuration_revision": 1})
        assert foreign.status_code == 404
        assert foreign.json() == {"detail": {"code": "not_found"}}

    for forged in (
        {"action": "launchNativeBrowser"},
        {"permitted_native_actions": ["launchNativeBrowser"]},
        {"role_arn": "arn:aws:iam::123456789012:role/foreign"},
    ):
        for project_id in (fixture.project_id, other_project):
            denied = await test_client.post(path(fixture.product_id, project_id), headers=authorization, json={"configuration_revision": 1, **forged})
            assert denied.status_code == 422

    assert fixture.binding.await_count == 2
    fixture.provider.assert_not_awaited()
