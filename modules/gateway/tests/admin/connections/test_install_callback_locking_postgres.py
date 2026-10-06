"""Reconnect must release its organization lock before independent bot seeding."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin.connections import service
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import ChannelTenantMap, InstallationOwnershipConflict, InstallationRevocation, MagicLinkNonce, UserIdentity
from tests.admin import install_setup_fixtures
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url

__all__ = ["pg_server", "pg_url"]
offline_setup_boundaries = install_setup_fixtures.offline_setup_boundaries


@pytest.mark.parametrize("account_type", ["User", "Organization"])
@pytest.mark.parametrize("revoked", [False, True])
async def test_reconnect_commits_before_independent_bot_transaction(pg_url, offline_setup_boundaries, monkeypatch, account_type, revoked):
    # A short database lock timeout makes the old self-wait fail deterministically
    # instead of waiting for CloudFront to retry the already-consumed nonce.
    engine = create_async_engine(to_async_url(pg_url), connect_args={"server_settings": {"lock_timeout": "750ms"}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    routing = AsyncMock()
    monkeypatch.setattr(service, "_write_installation_identity_index", routing)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        model.__table__
                        for model in (
                            Organization,
                            Department,
                            Team,
                            User,
                            TenantMembership,
                            UserIdentity,
                            ChannelTenantMap,
                            MagicLinkNonce,
                            InstallationRevocation,
                            InstallationOwnershipConflict,
                        )
                    ],
                )
            )
        async with sessions() as db:
            db.add(Organization(id="reconnect-org", name="Reconnect", github_installation_ids=["111"] if account_type == "Organization" else []))
            await db.flush()
            db.add(Department(id="human-dept", org_id="reconnect-org", name="Humans"))
            await db.flush()
            db.add(Team(id="human-team", org_id="reconnect-org", department_id="human-dept", name="Humans"))
            await db.flush()
            user = User(id="installer", org_id="reconnect-org", team_id="human-team", email="installer@example.test", cognito_sub="installer-sub")
            db.add(user)
            await db.commit()
            await install_setup_fixtures.issue_install_nonce(db, user, jti="inert-reconnect-state")
            db.add(
                ChannelTenantMap(
                    provider="github",
                    provider_scope_id="12345",
                    org_id="reconnect-org",
                    installation_id="111",
                    installed_by_user_id=user.id,
                    install_metadata={"account_id": "12345", "account_type": account_type, "account_login": "provider-owner"},
                )
            )
            await db.commit()
            if revoked:
                db.add(InstallationRevocation(installation_id="111", org_id="reconnect-org"))
                await db.commit()
            client = MagicMock()
            client.get_installation = AsyncMock(return_value={"id": 111, "account": {"id": 12345, "login": "provider-owner", "type": account_type}})
            client.list_installation_repository_names = AsyncMock(return_value=["provider-owner/demo"])
            client.get_bot_user = AsyncMock(return_value={"id": 424242, "login": "test-adp-agent[bot]", "type": "Bot"})
            install_setup_fixtures.bind_real_org_control(client)
            if revoked:
                with pytest.raises(PermissionError, match="existing tenant claim"):
                    await service.install_callback(
                        installation_id=111, setup_action="update", state="inert-reconnect-state", db=db, github_client=client
                    )
                assert (await db.get(MagicLinkNonce, "inert-reconnect-state")).consumed_at is None
                assert (await db.get(InstallationRevocation, "111")).restored_at is None
                client.get_bot_user.assert_not_awaited()
                routing.assert_not_awaited()
                return
            result = await asyncio.wait_for(
                service.install_callback(installation_id=111, setup_action="update", state="inert-reconnect-state", db=db, github_client=client),
                timeout=5,
            )
            assert result["success"] is True
            assert not db.in_transaction()
        async with sessions() as check:
            bot = (await check.scalars(select(User).where(User.user_kind == "bot"))).one()
            assert (await check.scalars(select(TenantMembership).where(TenantMembership.user_id == bot.id))).one().tenant_id == "reconnect-org"
            assert (await check.get(MagicLinkNonce, "inert-reconnect-state")).consumed_at is not None
            mapping = (await check.scalars(select(ChannelTenantMap))).one()
            assert mapping.org_id == "reconnect-org" and mapping.installation_id == "111"
            assert mapping.install_metadata["account_id"] == "12345"
            assert (await check.get(Organization, "reconnect-org")).github_installation_ids == (["111"] if account_type == "Organization" else [])
            routing.assert_awaited_once_with(installation_id=111, org_id="reconnect-org")
            assert any(call.kwargs.get("user_kind") == "bot" for call in offline_setup_boundaries.put_user_identity.await_args_list)
    finally:
        await engine.dispose()
