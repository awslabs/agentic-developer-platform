"""Stored Product actors retain their selected tenant and current global authority."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import update

from src.admin.platform_authority import current_cognito_admin, set_platform_admin_grant
from src.knowledge.product_access import require_product_access
from src.shared.identity.product_authority import stored_product_actor
from src.shared.models.organization import User
from tests.agentauth.test_knowledge_service_postgres import OWNER, sessions  # noqa: F401
from tests.agentauth.test_product_knowledge import OTHER, PRODUCT, current_platform_admin, product_db  # noqa: F401
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401


async def test_global_admin_from_another_org_preserves_initiator_and_selected_tenant(product_db, current_platform_admin):  # noqa: F811
    current_platform_admin.return_value = True
    async with product_db() as db:
        await db.execute(update(User).where(User.id == "human-one").values(role="member", cognito_username="canonical-login"))
        await db.commit()
        actor = await stored_product_actor(db, user_id="human-one", tenant_id="tenant-two")
        assert actor.user_id == "human-one"
        assert actor.org_id == "tenant-two"
        assert actor.is_admin is True
        assert actor.account_type == "human"
        assert actor.cognito_username == ""  # A persisted hint is not signed workspace proof.
        current_platform_admin.assert_called_once_with(OWNER, "canonical-login")
        access = await require_product_access(db, actor, OTHER)
        assert access.product.id == OTHER
        with pytest.raises(HTTPException) as denied:
            await require_product_access(db, actor, PRODUCT)
        assert denied.value.status_code == 404  # Administrator still selects exactly one tenant.


@pytest.mark.parametrize("demotion", ["durable", "idp"])
async def test_stored_admin_demotion_takes_effect_without_changing_saved_initiator(product_db, current_platform_admin, demotion):  # noqa: F811
    current_platform_admin.return_value = True
    async with product_db() as db:
        await db.execute(update(User).where(User.id == "human-one").values(role="platform_admin"))
        await set_platform_admin_grant(db, OWNER, enabled=True)
        await db.commit()
        assert (await stored_product_actor(db, user_id="human-one", tenant_id="tenant-two")).is_admin
        if demotion == "durable":
            await set_platform_admin_grant(db, OWNER, enabled=False)
            await db.commit()
        else:
            current_platform_admin.return_value = False
        actor = await stored_product_actor(db, user_id="human-one", tenant_id="tenant-two")
        assert not actor.is_admin
        assert actor.user_id == "human-one" and actor.org_id == "tenant-two"
        with pytest.raises(HTTPException) as denied:
            await require_product_access(db, actor, OTHER)
        assert denied.value.status_code == 404  # A matching Product owner label adds no authority.


@pytest.mark.parametrize("invalid", ["missing", "login-subject", "bot", "shadow"])
async def test_only_existing_canonical_human_ids_can_be_reconstructed(product_db, current_platform_admin, invalid):  # noqa: F811
    current_platform_admin.return_value = True
    async with product_db() as db:
        user_id = "human-one"
        if invalid == "missing":
            user_id = "missing"
        elif invalid == "login-subject":
            user_id = OWNER  # The shared helper deliberately accepts stored canonical IDs only.
        else:
            changes = {"user_kind": "bot"} if invalid == "bot" else {"is_shadow": True}
            await db.execute(update(User).where(User.id == user_id).values(**changes))
            await db.commit()
        with pytest.raises(HTTPException) as denied:
            await stored_product_actor(db, user_id=user_id, tenant_id="tenant-two")
        assert denied.value.status_code == 404
        current_platform_admin.assert_not_called()


async def test_display_admin_without_proven_login_remains_unprivileged(product_db, current_platform_admin):  # noqa: F811
    current_platform_admin.return_value = True
    async with product_db() as db:
        await db.execute(update(User).where(User.id == "human-one").values(role="platform_admin", cognito_sub=None))
        await db.commit()
        actor = await stored_product_actor(db, user_id="human-one", tenant_id="tenant-two")
        assert not actor.is_admin
        current_platform_admin.assert_not_called()


async def test_authority_outage_cannot_restore_cached_display_privilege(product_db, current_platform_admin):  # noqa: F811
    current_platform_admin.side_effect = OSError("IdP unavailable")
    async with product_db() as db:
        await db.execute(update(User).where(User.id == "human-one").values(role="platform_admin"))
        await db.commit()
        with pytest.raises(HTTPException) as unavailable:
            await stored_product_actor(db, user_id="human-one", tenant_id="tenant-two")
        assert unavailable.value.status_code == 503


async def test_stored_federated_actor_without_username_can_access_product_and_observes_demotion(product_db, monkeypatch):  # noqa: F811
    class UserNotFoundError(Exception):
        pass

    client = Mock()
    client.exceptions.UserNotFoundException = UserNotFoundError
    enabled = [True]

    def get_user(**kwargs):
        if kwargs["Username"] == OWNER:
            raise UserNotFoundError()
        assert kwargs["Username"] == "github_12345"
        return {
            "Username": "github_12345",
            "Enabled": enabled[0],
            "UserAttributes": [{"Name": "sub", "Value": OWNER}, {"Name": "custom:role", "Value": "platform_admin"}],
        }

    client.admin_get_user.side_effect = get_user
    client.list_users.return_value = {"Users": [{"Username": "github_12345"}]}
    monkeypatch.setattr("src.admin.platform_authority.current_cognito_admin", current_cognito_admin)
    monkeypatch.setattr("src.admin.platform_authority.get_settings", lambda: SimpleNamespace(cognito_user_pool_id="pool", aws_region="us-east-1"))
    monkeypatch.setattr("src.admin.platform_authority.boto3.client", lambda *a, **kw: client)
    async with product_db() as db:
        await db.execute(update(User).where(User.id == "human-one").values(cognito_username=None))
        await db.commit()
        actor = await stored_product_actor(db, user_id="human-one", tenant_id="tenant-two")
        assert actor.is_admin and actor.user_id == "human-one" and actor.cognito_username == ""
        assert (await require_product_access(db, actor, OTHER)).product.id == OTHER
        enabled[0] = False
        denied = await stored_product_actor(db, user_id="human-one", tenant_id="tenant-two")
        assert not denied.is_admin
        with pytest.raises(HTTPException) as error:
            await require_product_access(db, denied, OTHER)
        assert error.value.status_code == 404
