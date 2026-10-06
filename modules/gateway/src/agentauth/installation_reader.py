"""Resolve a chat user's permission to read one connected installation."""

from dataclasses import dataclass

from sqlalchemy import select

from src.admin.installations.resolver import InstallationOwnershipError, assert_installation_owned_by
from src.agentauth.chat_capability import ChatAuthorizationRefusedError
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.vault import ChannelTenantMap


@dataclass(frozen=True)
class InstallationReader:
    tenant_id: str
    installation_id: int


async def authorize_installation_reader(db, *, tenant_id: str, user_id: str, installation_id: int) -> InstallationReader:
    """Require current human identity, corroborated ownership and installer binding."""
    refused = ChatAuthorizationRefusedError("installation read refused")
    if isinstance(installation_id, bool) or not isinstance(installation_id, int) or not 0 < installation_id < 10**20:
        raise refused
    user = await db.scalar(select(User.id).where(User.id == user_id, User.org_id == tenant_id, User.user_kind == "human", User.is_shadow.is_(False)))
    if user is None:
        raise refused
    revoked = await db.scalar(
        select(TenantMembership.id).where(
            TenantMembership.user_id == user_id,
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.revoked_at.is_not(None),
        )
    )
    if revoked is not None:
        raise refused
    try:
        await assert_installation_owned_by(tenant_id, installation_id, db=db)
    except InstallationOwnershipError:
        raise refused from None
    installer = await db.scalar(
        select(ChannelTenantMap.id).where(
            ChannelTenantMap.provider == "github",
            ChannelTenantMap.org_id == tenant_id,
            ChannelTenantMap.installation_id == str(installation_id),
            ChannelTenantMap.installed_by_user_id == user_id,
            ChannelTenantMap.ownership_disputed.is_(False),
        )
    )
    if installer is None:
        raise refused
    return InstallationReader(tenant_id=tenant_id, installation_id=installation_id)
