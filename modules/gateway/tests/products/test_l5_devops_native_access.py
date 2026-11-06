"""Human UI consent and launch checks use real Product scope and no browser automation."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from l5_services.api.bindings import BindingStore
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.devops_space import DevopsSpaceIntent
from sqlalchemy import delete, select

from src import l5_integration
from src.auth.dependencies import get_current_user
from src.auth.l5_execution_scope import current_execution_scope
from src.products.models import Product
from src.shared.models.organization import TeamMembership, User
from src.shared.models.vault import CredentialServiceDelegation
from tests.products import test_l5_configuration_host as fixtures
from tests.products.membership import add_team_member, grant_platform_admin

durable_devops_scope = fixtures.durable_devops_scope
l5_owner = fixtures.l5_owner


@pytest.fixture
async def native_scope(durable_devops_scope, db_session, l5_owner, app, monkeypatch):
    scope = durable_devops_scope
    binding = await db_session.get(Binding, (l5_owner.org_id, scope.selection.binding_id))
    old_receipt = await db_session.scalar(select(DevopsSpaceIntent))
    await db_session.delete(old_receipt)
    await db_session.delete(binding)
    await db_session.flush()
    reference = {**scope.reference, "record_type": "agent_space", "record_id": "native-space", "record_revision": "native-space"}
    identity = {"account_id": scope.selection.account_id, "role_arn": scope.role, "region": scope.selection.region}
    ordinary = await db_session.scalar(select(CredentialServiceDelegation))
    verified = {
        "configuration_id": scope.config_id,
        "configuration_revision": 1,
        "binding_id": scope.selection.binding_id,
        "scope": scope.scope,
        "connection_ref": scope.reference,
        **identity,
        "authorization": {"actor_id": l5_owner.user_id, "delegation_id": ordinary.id, "delegation_revision": ordinary.revision},
    }
    provider = AsyncMock(
        read=AsyncMock(
            return_value={
                "native_resource_ref": reference,
                "provider_identity": identity,
                "state": "ready",
                "readiness": [],
                "gaps": [],
                "prevention_observation": {
                    "scope_fingerprint": scope.selection.fingerprint(),
                    "configuration_revision": 1,
                    "native_space_id": "native-space",
                },
            }
        )
    )
    store = BindingStore(db_session, l5_owner.org_id, scope.product_id, scope.project_id, "devops")
    operation = await store.start(
        {"action": "link", "configuration_revision": 1, "native_resource_ref": reference}, "native_ui_original_link", verified
    )
    await store.finish_link(operation.operation_id, verified, provider)
    # Neither Investigation consent nor a synthesized readiness item grants native UI access.
    ordinary = await db_session.scalar(select(CredentialServiceDelegation))
    ordinary.capabilities = ["investigation"]
    version = await db_session.get(ConfigurationVersion, (l5_owner.org_id, scope.config_id, 1))
    version.settings = {
        **version.settings,
        "enabled": False,
        "capabilities": ["investigation"],
        "devops": {
            **version.settings["devops"],
            "browser_launch_policy": "disabled",
            "permitted_native_actions": [],
        },
    }
    version.state = "disabled"
    await db_session.commit()
    monkeypatch.setattr(app.state.ratelimit_service, "consume_tool_rate_limit", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    monkeypatch.setattr(l5_integration, "get_settings", lambda: SimpleNamespace(gateway_base_url="https://gateway.example.test"))
    reader = Mock()
    reader.get_agent_space.return_value = {"agentSpace": {"agentSpaceId": "native-space"}}
    reader.get_operator_app.return_value = {
        "iam": {"operatorAppRoleArn": scope.operator},
        "operatorAppUrl": "https://native-space.aidevops.global.app.aws/",
    }
    sessions = AsyncMock(return_value=Mock(client=Mock(return_value=reader)))
    launched = AsyncMock(
        return_value={
            "binding_id": scope.selection.binding_id,
            "launch_url": "https://signin.aws.amazon.com/federation?ticket=test",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        }
    )
    monkeypatch.setattr(l5_integration.GatewayConfigurationBackend, "_devops_connection_session", sessions)
    monkeypatch.setattr(l5_integration.federation, "mint_browser_launch", launched)
    path = f"/v1/products/{scope.product_id}/projects/{scope.project_id}/services/devops"
    return SimpleNamespace(
        **vars(scope),
        path=path,
        sessions=sessions,
        reader=reader,
        launched=launched,
        ordinary=ordinary,
        policy={"launcher_role_arn": scope.launcher, "operator_role_arn": scope.operator},
    )


async def grant_native(client, fixture, *, expiry=None):
    body = {"binding_id": fixture.selection.binding_id, "for_control": True, "control_action": "devops_native_ui", "native_ui_policy": fixture.policy}
    preview = await client.post(fixture.path + "/delegation-scope", json=body)
    assert preview.status_code == 200, preview.text
    granted = await client.post(
        fixture.path + "/delegations",
        json={**body, "scope_digest": preview.json()["scope_digest"], "expires_at": (expiry or datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        headers={"If-Match": preview.headers["ETag"], "Idempotency-Key": "native_ui_owner_grant_0001"},
    )
    return preview, granted


@pytest.mark.asyncio
async def test_owner_grant_then_human_check_launch_and_revoke(test_client, db_session, native_scope, l5_owner):
    fixture = native_scope
    preview, granted = await grant_native(test_client, fixture)
    assert granted.status_code == 200, granted.text
    scope = preview.json()["scope"]
    assert scope["capabilities"] == ["native_ui"] and scope["operations"] == ["credential:session"]
    assert scope["binding_generation"] == 1 and scope["native_ui_policy"]["native_space_ref"]["record_id"] == "native-space"
    grant = await db_session.get(CredentialServiceDelegation, granted.json()["delegation_id"])
    assert not await current_execution_scope(db_session, grant, check_binding=True, binding_id=fixture.selection.binding_id, binding_generation=1)
    assert fixture.ordinary.revoked_at is None and fixture.ordinary.capabilities == ["investigation"]
    path = fixture.path + f"/bindings/{fixture.selection.binding_id}/browser-launch"
    headers = {"Authorization": "Bearer offline-placeholder"}
    checked = await test_client.post(path + "/check", json={"configuration_revision": 1}, headers=headers)
    assert checked.status_code == 200, checked.text
    assert checked.json()["ready"] and checked.json()["binding_revision"] == 1
    assert checked.headers["Cache-Control"] == "no-store" and "launch_url" not in checked.json()
    assert fixture.sessions.await_count == 1
    fixture.launched.assert_not_awaited()
    launched = await test_client.post(path, json={"configuration_revision": 1}, headers=headers)
    assert launched.status_code == 200, launched.text
    assert fixture.launched.await_count == 1 and fixture.launched.call_args.kwargs["operator_role_arn"] == fixture.operator
    revoked = await test_client.delete(fixture.path + f"/delegations/{grant.id}", headers={"If-Match": '"1"'})
    assert revoked.status_code == 200
    denied = await test_client.post(path, json={"configuration_revision": 1}, headers=headers)
    assert denied.status_code in {403, 404} and fixture.launched.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["config", "binding", "expiry", "credential", "policy", "scope"])
async def test_launch_rechecks_changes_after_eligibility(test_client, db_session, native_scope, l5_owner, changed):
    fixture = native_scope
    _, granted = await grant_native(test_client, fixture)
    assert granted.status_code == 200, granted.text
    path = fixture.path + f"/bindings/{fixture.selection.binding_id}/browser-launch"
    args = {"json": {"configuration_revision": 1}, "headers": {"Authorization": "Bearer offline-placeholder"}}
    assert (await test_client.post(path + "/check", **args)).status_code == 200
    grant = await db_session.get(CredentialServiceDelegation, granted.json()["delegation_id"])
    if changed == "config":
        config = await db_session.get(Configuration, (l5_owner.org_id, fixture.config_id))
        config.current_revision = 2
    elif changed == "binding":
        binding = await db_session.get(Binding, (l5_owner.org_id, fixture.selection.binding_id))
        binding.revision += 1
    elif changed == "expiry":
        grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif changed == "credential":
        fixture.credential.aws_verified_version_id = "changed-version"
    elif changed == "policy":
        grant.execution_scope = {
            **grant.execution_scope,
            "native_ui_policy": {**grant.execution_scope["native_ui_policy"], "operator_role_arn": fixture.role},
        }
    else:
        grant.capabilities = ["native_ui", "investigation"]
    await db_session.commit()
    denied = await test_client.post(path, **args)
    assert denied.status_code in {403, 404, 409, 503}, denied.text
    assert fixture.sessions.await_count == 1
    fixture.launched.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("launcher_role_arn", "arn:aws:iam::999999999999:role/foreign"),
        ("operator_role_arn", "arn:aws:iam::123456789012:role/hosting"),
        ("operator_role_arn", "arn:aws:iam::123456789012:role/launch"),
        ("policy", "forged"),
    ],
)
async def test_native_policy_cannot_widen_selected_roles(test_client, native_scope, field, value):
    fixture = native_scope
    fixture.policy[field] = value
    response = await test_client.post(
        fixture.path + "/delegation-scope",
        json={
            "binding_id": fixture.selection.binding_id,
            "for_control": True,
            "control_action": "devops_native_ui",
            "native_ui_policy": fixture.policy,
        },
    )
    assert response.status_code == 409, response.text
    fixture.sessions.assert_not_awaited()
    fixture.launched.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_grant_requires_explicit_live_expiry(test_client, native_scope):
    _, response = await grant_native(test_client, native_scope, expiry=datetime.now(UTC) + timedelta(seconds=30))
    assert response.status_code == 422 and response.json()["detail"]["code"] == "delegation_expiry_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_minutes,grant_minutes", [(5, 2), (2, 60)])
async def test_short_adp_session_and_consent_cap_handoff(test_client, db_session, native_scope, l5_owner, app, actor_minutes, grant_minutes):
    fixture = native_scope
    actor_expiry = datetime.now(UTC) + timedelta(minutes=actor_minutes)
    app.dependency_overrides[get_current_user] = lambda: l5_owner.model_copy(update={"expires_at": actor_expiry})
    _, granted = await grant_native(test_client, fixture, expiry=datetime.now(UTC) + timedelta(minutes=grant_minutes))
    assert granted.status_code == 200, granted.text
    grant = await db_session.get(CredentialServiceDelegation, granted.json()["delegation_id"])
    path = fixture.path + f"/bindings/{fixture.selection.binding_id}/browser-launch"
    args = {"json": {"configuration_revision": 1}, "headers": {"Authorization": "Bearer offline-placeholder"}}
    assert (await test_client.post(path + "/check", **args)).status_code == 200
    launched = await test_client.post(path, **args)
    assert launched.status_code == 200, launched.text
    assert fixture.launched.call_args.kwargs["expires_at"] == min(actor_expiry, grant.expires_at.replace(tzinfo=UTC))


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["owner_transfer", "owner_team_removed", "actor_admin_outside_team"])
async def test_live_owner_and_actor_membership_are_required(test_client, db_session, native_scope, l5_owner, app, monkeypatch, changed):
    fixture = native_scope
    _, granted = await grant_native(test_client, fixture)
    assert granted.status_code == 200
    if changed == "owner_transfer":
        product = await db_session.get(Product, (l5_owner.org_id, fixture.product_id))
        product.owner_user_id = "new-product-owner"
    elif changed == "owner_team_removed":
        teammate = l5_owner.model_copy(update={"user_id": "remaining-teammate"})
        db_session.add(User(id=teammate.user_id, org_id=teammate.org_id, team_id=teammate.team_id, email="teammate@example.test", user_kind="human"))
        await add_team_member(db_session, teammate)
        await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == l5_owner.user_id))
        app.dependency_overrides[get_current_user] = lambda: teammate
    else:
        admin = l5_owner.model_copy(update={"user_id": "outside-admin", "team_id": "other-team"})
        db_session.add(User(id=admin.user_id, org_id=admin.org_id, team_id=admin.team_id, email="outside@example.test", user_kind="human"))
        await add_team_member(db_session, admin)
        await grant_platform_admin(db_session, admin, monkeypatch)
        app.dependency_overrides[get_current_user] = lambda: admin
    await db_session.commit()
    path = fixture.path + f"/bindings/{fixture.selection.binding_id}/browser-launch"
    response = await test_client.post(path, json={"configuration_revision": 1}, headers={"Authorization": "Bearer offline-placeholder"})
    assert response.status_code == 404, response.text
    fixture.sessions.assert_not_awaited()
    fixture.launched.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["revoked", "binding", "membership", "owner_transfer", "grant_expiry", "actor_expiry"])
async def test_provider_result_never_releases_url_after_authority_changes(test_client, db_session, native_scope, l5_owner, changed, monkeypatch):
    fixture = native_scope
    _, granted = await grant_native(test_client, fixture)
    assert granted.status_code == 200
    original_result = fixture.launched.return_value

    async def mutate_during_provider(*args, **kwargs):
        if changed in {"revoked", "grant_expiry"}:
            grant = await db_session.get(CredentialServiceDelegation, granted.json()["delegation_id"])
            if changed == "revoked":
                grant.revoked_at = datetime.now(UTC)
            else:
                grant.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif changed == "actor_expiry":

            class Later(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.now(tz) + timedelta(days=1)

            monkeypatch.setattr(l5_integration, "datetime", Later)
        elif changed == "binding":
            binding = await db_session.get(Binding, (l5_owner.org_id, fixture.selection.binding_id))
            binding.revision += 1
        elif changed == "membership":
            await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == l5_owner.user_id))
        else:
            product = await db_session.get(Product, (l5_owner.org_id, fixture.product_id))
            product.owner_user_id = "new-product-owner"
        await db_session.commit()
        return original_result

    fixture.launched.side_effect = mutate_during_provider
    response = await test_client.post(
        fixture.path + f"/bindings/{fixture.selection.binding_id}/browser-launch",
        json={"configuration_revision": 1},
        headers={"Authorization": "Bearer offline-placeholder"},
    )
    assert response.status_code in {401, 403, 404}, response.text
    assert "launch_url" not in response.text and "ticket=test" not in response.text
    fixture.launched.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_app_mismatch_keeps_access_unready(test_client, native_scope):
    fixture = native_scope
    _, granted = await grant_native(test_client, fixture)
    assert granted.status_code == 200
    fixture.reader.get_operator_app.return_value["iam"]["operatorAppRoleArn"] = fixture.launcher
    response = await test_client.post(
        fixture.path + f"/bindings/{fixture.selection.binding_id}/browser-launch/check",
        json={"configuration_revision": 1},
        headers={"Authorization": "Bearer offline-placeholder"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "native_operator_app_unavailable"
    fixture.launched.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_selects_execution_consent_with_native_consent_present(test_client, db_session, native_scope, l5_owner, monkeypatch):
    from pathlib import Path

    from starlette.requests import Request

    from src.internal import tools_l5

    monkeypatch.setattr(tools_l5, "files", lambda package: Path(__file__).resolve().parents[4] / "docs/l5-services")
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    fixture = native_scope
    _, granted = await grant_native(test_client, fixture)
    assert granted.status_code == 200
    actor = l5_owner.model_copy(update={"account_type": "service", "auth_source": "iam", "agent_registry_id": "l5-devops-worker"})
    dispatch = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(l5_integration, "dispatch_l5", dispatch)
    selected = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(l5_integration, "backend", selected)
    result = await l5_integration.call_l5_mcp_tool(
        l5_integration.L5ToolCall(
            tool="l5___get_configuration",
            arguments={
                "product_id": fixture.product_id,
                "project_id": fixture.project_id,
                "service_id": "devops",
            },
        ),
        Request({"type": "http", "headers": []}),
        actor=actor,
        db=db_session,
    )
    assert result == {"ok": True}
    assert selected.call_args.kwargs["x_l5_delegation_id"] == fixture.ordinary.id
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_original_setup_remains_exact_when_later_receipts_exist(test_client, db_session, native_scope, l5_owner):
    from uuid import uuid4

    fixture = native_scope
    _, granted = await grant_native(test_client, fixture)
    assert granted.status_code == 200
    grant = await db_session.get(CredentialServiceDelegation, granted.json()["delegation_id"])
    original = await db_session.get(DevopsSpaceIntent, (l5_owner.org_id, grant.execution_scope["native_ui_policy"]["setup_operation_id"]))
    db_session.add(
        DevopsSpaceIntent(
            tenant_id=l5_owner.org_id,
            operation_id=str(uuid4()),
            selection=original.selection,
            configuration_revision=1,
            intent_hash=original.intent_hash,
            state="ready",
            native_space_id="native-space",
            ownership=original.ownership,
            scope_fingerprint=original.scope_fingerprint,
        )
    )
    await db_session.commit()
    response = await test_client.post(
        fixture.path + f"/bindings/{fixture.selection.binding_id}/browser-launch/check",
        json={"configuration_revision": 1},
        headers={"Authorization": "Bearer offline-placeholder"},
    )
    assert response.status_code == 200, response.text
    assert fixture.sessions.call_args.args[2] == original.operation_id
    fixture.launched.assert_not_awaited()
