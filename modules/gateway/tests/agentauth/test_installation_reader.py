"""Installation reader grants are narrower than tenant membership and administration."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.agentauth.installation_reader import InstallationReader, authorize_installation_reader
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import ChannelTenantMap


async def installation(db, tenant, installation_id, installer):
    db.add(
        ChannelTenantMap(
            provider="github",
            org_id=tenant,
            provider_scope_id=f"account-{installation_id}",
            installation_id=str(installation_id),
            installed_by_user_id=installer,
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_only_installer_of_owned_installation_may_read(db_session):
    db_session.add_all(
        [
            Organization(id="tenant-a", name="Tenant A"),
            Organization(id="tenant-b", name="Tenant B"),
            User(id="installer-a", org_id="tenant-a", team_id="team", email="a@example.test"),
            User(id="admin-a", org_id="tenant-a", team_id="team", email="admin@example.test"),
            User(id="installer-b", org_id="tenant-b", team_id="team", email="b@example.test"),
            TenantMembership(user_id="admin-a", tenant_id="tenant-a", role="org_admin"),
        ]
    )
    await db_session.commit()
    await installation(db_session, "tenant-a", 101, "installer-a")
    await installation(db_session, "tenant-b", 202, "installer-b")

    assert await authorize_installation_reader(db_session, tenant_id="tenant-a", user_id="installer-a", installation_id=101) == InstallationReader(
        tenant_id="tenant-a", installation_id=101
    )
    for tenant, user, target in [
        ("tenant-a", "admin-a", 101),
        ("tenant-a", "installer-a", 202),
        ("tenant-b", "installer-b", 101),
        ("tenant-a", "installer-b", 101),
        ("tenant-a", "installer-a", 303),
    ]:
        with pytest.raises(ChatAuthorizationRefusedError, match="installation read refused"):
            await authorize_installation_reader(db_session, tenant_id=tenant, user_id=user, installation_id=target)


@pytest.mark.asyncio
async def test_revocation_and_disputed_claim_do_not_grant_read(db_session):
    db_session.add_all(
        [
            Organization(id="tenant-a", name="Tenant A"),
            User(id="installer", org_id="tenant-a", team_id="team", email="a@example.test"),
            TenantMembership(user_id="installer", tenant_id="tenant-a", revoked_at=datetime.now(UTC)),
        ]
    )
    await db_session.commit()
    await installation(db_session, "tenant-a", 101, "installer")
    with pytest.raises(ChatAuthorizationRefusedError):
        await authorize_installation_reader(db_session, tenant_id="tenant-a", user_id="installer", installation_id=101)
    membership = await db_session.scalar(select(TenantMembership).where(TenantMembership.user_id == "installer"))
    membership.revoked_at = None
    db_session.add(Organization(id="tenant-b", name="Tenant B", github_installation_ids=["101"]))
    await db_session.commit()
    with pytest.raises(ChatAuthorizationRefusedError):
        await authorize_installation_reader(db_session, tenant_id="tenant-a", user_id="installer", installation_id=101)


@pytest.mark.asyncio
async def test_self_asserted_installation_and_invalid_reference_denied(db_session):
    db_session.add_all(
        [
            Organization(id="tenant-a", name="Tenant A", github_installation_ids=["101"]),
            User(id="installer", org_id="tenant-a", team_id="team", email="a@example.test"),
        ]
    )
    await db_session.commit()
    for target in (101, 0, -1, True, "101"):
        with pytest.raises(ChatAuthorizationRefusedError):
            await authorize_installation_reader(db_session, tenant_id="tenant-a", user_id="installer", installation_id=target)
