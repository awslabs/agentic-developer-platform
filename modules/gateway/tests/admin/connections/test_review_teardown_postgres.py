"""Real PostgreSQL serialization review on an isolated Unix-socket-only cluster."""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin.connections import service as connections
from src.admin.installations import revocation
from src.admin.org_connections import service as org_connections
from src.admin.org_connections.schemas import GitHubConnectionAttachRequest
from src.shared.models.organization import Organization
from src.shared.models.vault import ChannelTenantMap, InstallationRevocation
from tests.admin.connections import test_installation_revocation_durable as fx
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url, upgrade

projection = fx.projection
__all__ = ["pg_server", "pg_url"]


@pytest.fixture
async def sessions(pg_url):
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_two_overlapping_disconnects_do_not_resurrect_asserted_installation_ids(sessions, projection):
    async with sessions() as seed:
        await fx._seed(seed, projection, other=True)
    async with sessions() as first, sessions() as second:
        # A real route may have loaded the org through workspace/map resolution
        # before acquiring the lifecycle lock. Retain those ORM identity objects.
        first_snapshot = await first.get(Organization, fx.ORG)
        second_snapshot = await second.get(Organization, fx.ORG)
        assert first_snapshot.github_installation_ids == second_snapshot.github_installation_ids == [str(fx.INSTALL), str(fx.OTHER)]

        async def disconnect(db, installation_id):
            return await revocation.revoke_installation(
                installation_id=installation_id, org_id=fx.ORG, db=db, user_id="installer", is_admin=False, uninstall=False, index=projection.index
            )

        results = await asyncio.wait_for(asyncio.gather(disconnect(first, fx.INSTALL), disconnect(second, fx.OTHER)), timeout=10)
        assert all(result.local_revoked for result in results)
    async with sessions() as check:
        current = await check.get(Organization, fx.ORG)
        assert current.github_installation_ids == [], f"stale installation assertion resurrected: {current.github_installation_ids}"
        assert not list(await check.scalars(select(ChannelTenantMap)))
        assert len(list(await check.scalars(select(InstallationRevocation)))) == 2


async def test_overlapping_operator_attach_and_disconnect_preserve_current_installation_list(sessions, projection, monkeypatch):
    async with sessions() as seed:
        await fx._seed(seed, projection, other=True)
    new_install = 789012
    entered, release = asyncio.Event(), asyncio.Event()
    original_guard = org_connections.assert_installation_claimable_by

    async def guard(org_id, installation_id, **kwargs):
        await original_guard(org_id, installation_id, **kwargs)
        if installation_id == new_install:
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=10)

    monkeypatch.setattr(org_connections, "assert_installation_claimable_by", guard)
    async with sessions() as attaching, sessions() as disconnecting:
        service = org_connections.OrgConnectionsService(attaching, identity_index=projection.writer)
        task = asyncio.create_task(
            service.attach_github(fx.ORG, GitHubConnectionAttachRequest(installation_id=new_install, github_org_id="fresh-account"))
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            disconnect_task = asyncio.create_task(
                revocation.revoke_installation(
                    installation_id=fx.INSTALL,
                    org_id=fx.ORG,
                    db=disconnecting,
                    user_id="installer",
                    is_admin=False,
                    uninstall=False,
                    index=projection.index,
                )
            )
            # A correct shared lifecycle lock may make disconnect wait here;
            # permit that serial order instead of deadlocking the test itself.
            await asyncio.wait({disconnect_task}, timeout=0.2)
        finally:
            release.set()
        assert (await asyncio.wait_for(task, timeout=10)).routable
        assert (await asyncio.wait_for(disconnect_task, timeout=10)).local_revoked
    async with sessions() as check:
        current = await check.get(Organization, fx.ORG)
        assert set(current.github_installation_ids) == {str(fx.OTHER), str(new_install)}, (
            f"stale IDs survived attach: {current.github_installation_ids}"
        )
        assert (await check.get(InstallationRevocation, str(fx.INSTALL))).restored_at is None


async def test_callback_append_does_not_restore_stale_ids_after_other_install_disconnect(sessions, projection):
    new_install = 789012
    async with sessions() as seed:
        await fx._seed(seed, projection, other=True)
    async with sessions() as callback, sessions() as disconnecting:
        # Callback resolution loaded the organization before writing its map.
        # Its commit does not expire ORM attributes in production sessions.
        original = await callback.get(Organization, fx.ORG)
        assert original.github_installation_ids == [str(fx.INSTALL), str(fx.OTHER)]
        await connections._attach_org_installation(
            installation_id=new_install,
            github_org_id=1000,
            github_org_login="new-account",
            caller_org_id=fx.ORG,
            installed_by_user_id="installer",
            db=callback,
        )
        await revocation.revoke_installation(
            installation_id=fx.INSTALL, org_id=fx.ORG, db=disconnecting, user_id="installer", is_admin=False, uninstall=False, index=projection.index
        )
        await connections._append_installation_id_to_org(installation_id=new_install, caller_org_id=fx.ORG, db=callback)
    async with sessions() as check:
        current = await check.get(Organization, fx.ORG)
        assert set(current.github_installation_ids) == {str(fx.OTHER), str(new_install)}, (
            f"callback append restored stale IDs: {current.github_installation_ids}"
        )


async def test_map_only_survivor_keeps_account_fields_and_can_still_resolve(sessions, projection):
    from src.admin.installations.resolver import OwnerState, resolve_installation_owner

    async with sessions() as db:
        await fx._seed(db, projection, org_assertion=False, other=True)
        org = await db.get(Organization, fx.ORG)
        org.github_installation_ids = []
        await db.commit()
        result = await connections.delete_connection(
            installation_id=fx.INSTALL,
            caller_org_id=fx.ORG,
            caller_user_id="installer",
            caller_is_admin=False,
            github_client=fx._provider(),
            db=db,
        )
        assert result.local_revoked
        await db.refresh(org)
        assert org.github_installation_ids == []
        assert org.github_org_id == "999" and org.github_app_id == "app-1"
        owner, state = await resolve_installation_owner(fx.OTHER, db=db)
        assert state is OwnerState.RESOLVED and owner.tenant_id == fx.ORG
        assert await db.scalar(select(ChannelTenantMap).where(ChannelTenantMap.installation_id == str(fx.OTHER))) is not None
