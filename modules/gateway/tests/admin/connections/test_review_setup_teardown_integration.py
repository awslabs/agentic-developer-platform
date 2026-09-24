"""Independent exact setup plus durable teardown route integration."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.admin.connections import service as connections
from src.shared.identity.workspaces import link_login_to_workspace
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import ChannelTenantMap, InstallationRevocation, MagicLinkNonce
from tests.admin.connections import test_installation_revocation_durable as teardown_fx
from tests.admin.connections import test_setup_identity_binding as fx

db = fx.db
db_engine = fx.db_engine
setup = fx.setup
projection = teardown_fx.projection


async def test_actual_durable_revocation_denies_live_setup_capability(db, projection, setup):
    await fx.prove(db, setup)
    first = (await fx.start(setup)).json()["state_token"]
    assert "success=1" in (await fx.callback(setup, first)).headers["location"]
    second = (await fx.start(setup)).json()["state_token"]
    result = await connections.delete_connection(
        installation_id=123456,
        caller_org_id=setup["claims"]["org"],
        caller_user_id=setup["user"].id,
        caller_is_admin=False,
        db=db,
        github_client=teardown_fx._provider(),
    )
    assert result.local_revoked
    assert (await db.get(InstallationRevocation, "123456")).restored_at is None
    setup["seed"].reset_mock()
    setup["index"].reset_mock()
    response = await fx.callback(setup, second)
    assert "tenant_conflict" in response.headers["location"]
    assert (await db.get(MagicLinkNonce, second)).consumed_at is None
    assert not list(await db.scalars(select(ChannelTenantMap)))
    setup["seed"].assert_not_awaited()
    setup["index"].assert_not_awaited()


async def test_tenant_local_installer_can_disconnect_through_real_authenticated_route(db, projection, setup, monkeypatch):
    db.add(Organization(id="selected-work", name="Selected workspace"))
    local = User(id="selected-local-user", org_id="selected-work", team_id="", email="local@example.test", role="member")
    db.add(local)
    await db.flush()
    await link_login_to_workspace(db, setup["user"], local)
    db.add(TenantMembership(user_id=local.id, tenant_id="selected-work", role="member"))
    await db.commit()
    setup["claims"].update(org="selected-work")
    await fx.prove(db, setup, user_id=local.id, org_id=local.org_id)
    state = (await fx.start(setup)).json()["state_token"]
    assert "success=1" in (await fx.callback(setup, state)).headers["location"]
    mapping = (await db.scalars(select(ChannelTenantMap))).one()
    assert mapping.installed_by_user_id == local.id
    provider_delete = AsyncMock(side_effect=RuntimeError("inert provider outage"))
    monkeypatch.setattr(setup["github"], "delete_installation", provider_delete)
    response = await setup["client"].delete("/admin/connections/github/123456", headers={"Authorization": "Bearer inert-valid-signed-token"})
    assert response.status_code == 200, response.text
    assert response.json()["local_revoked"]
    assert response.json()["residual"] == ["provider_uninstall"]
    provider_delete.side_effect = None
    retry = await setup["client"].delete("/admin/connections/github/123456", headers={"Authorization": "Bearer inert-valid-signed-token"})
    assert retry.status_code == 200, retry.text
    assert retry.json()["provider_revoked"] and not retry.json()["residual"]
    assert provider_delete.await_count == 2


@pytest.mark.parametrize("with_nonce", [False, True])
async def test_callback_revoked_after_map_commit_cannot_seed_credentials(db, projection, setup, monkeypatch, with_nonce):
    from src.admin.installations import revocation

    if with_nonce:
        await fx.prove(db, setup)
        state = (await fx.start(setup)).json()["state_token"]
    else:
        state = None
        org = await db.get(Organization, setup["claims"]["org"])
        org.github_org_id = "999"
        await db.commit()
        setup["metadata"]["account"] = {"id": 999, "login": "inert-org", "type": "Organization"}
    original_attach = connections._attach_org_installation

    async def attach_then_disconnect(**kwargs):
        await original_attach(**kwargs)
        await revocation.revoke_installation(
            installation_id=123456,
            org_id=kwargs["caller_org_id"],
            db=db,
            user_id=setup["user"].id,
            is_admin=True,
            uninstall=False,
            index=projection.index,
        )

    monkeypatch.setattr(connections, "_attach_org_installation", attach_then_disconnect)
    response = await fx.callback(setup, state)
    assert "success=1" not in response.headers.get("location", "")
    assert "Installation complete" not in response.text
    assert (await db.get(InstallationRevocation, "123456")).restored_at is None
    assert not list(await db.scalars(select(ChannelTenantMap)))
    setup["seed"].assert_not_awaited()
    setup["index"].assert_not_awaited()
