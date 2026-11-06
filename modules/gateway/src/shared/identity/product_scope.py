"""Resolve current human and explicitly delegated service Product authority."""

from datetime import UTC

from sqlalchemy import and_, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.products.models import Product
from src.shared.identity import workspaces
from src.shared.models.base import utcnow
from src.shared.models.organization import TeamMembership, User
from src.shared.models.vault import ChannelTenantMap, CredentialServiceDelegation, UserCredential
from src.shared.schemas.auth import TokenContext


async def product_member(
    db: AsyncSession, *, user_id: str, org_id: str, username: str = "", account_type: str = "human", is_platform_admin: bool = False
) -> User | None:
    if not user_id or not org_id or account_type != "human":
        return None
    member = await workspaces.workspace_user(db, user_id, org_id, username=username, refresh=True)
    # This flag must come from get_current_user/apply_platform_admin_authority,
    # never a stored display role or caller-supplied identity field. Admins can
    # inspect the selected tenant even without local workspace membership.
    if member is None and is_platform_admin:
        member = await workspaces.login_user(db, user_id, refresh=True)
    if member is None or member.user_kind != "human" or member.is_shadow:
        return None
    return member


def product_visibility_scope(member: User | None, tenant_id: str, *, is_platform_admin: bool = False):
    """Current assigned-team visibility, with a trusted platform-admin exception.

    An owner label or stale users.team_id pointer is not membership evidence.
    Teamless legacy Products remain administrator-only. Every branch retains
    the selected tenant boundary, including the platform administrator branch.
    """
    if is_platform_admin:
        return Product.tenant_id == tenant_id
    if member is None:
        return false()
    membership = (
        select(TeamMembership.id)
        .where(
            TeamMembership.user_id == member.id,
            TeamMembership.org_id == tenant_id,
            TeamMembership.team_id == Product.team_id,
        )
        .exists()
    )
    return and_(Product.tenant_id == tenant_id, Product.team_id.is_not(None), membership)


def owner_scope(member: User, tenant_id: str):
    """Compatibility for delegated callers: no owner shortcut or admin override."""
    return product_visibility_scope(member, tenant_id)


async def current_l5_parent(db: AsyncSession, grant: CredentialServiceDelegation) -> bool:
    from src.auth.l5_devops_knowledge_scope import current_knowledge_scope, is_knowledge_scope
    from src.auth.l5_execution_scope import current_execution_scope
    from src.auth.l5_transform_branch_scope import occurrence_source, parent_scope

    if is_knowledge_scope(grant):
        return await current_knowledge_scope(db, grant)
    if not await current_execution_scope(db, grant):
        return False
    if grant.parent_delegation_id is None:
        return True
    parent = await db.get(CredentialServiceDelegation, grant.parent_delegation_id, populate_existing=True)
    if parent is None:
        return False
    expiry = parent.expires_at.replace(tzinfo=UTC) if parent.expires_at.tzinfo is None else parent.expires_at
    return (
        parent.revoked_at is None
        and expiry > utcnow()
        and parent.credential_id == grant.credential_id
        and parent.tenant_id == grant.tenant_id
        and parent.service_id == grant.service_id
        and parent.product_id == grant.product_id
        and parent.project_id == grant.project_id
        and parent.created_by == grant.created_by
        and parent.execution_scope == parent_scope(grant.execution_scope)
        and parent.binding_generation == grant.binding_generation
        and set(grant.operations).issubset(parent.operations)
        and set(grant.capabilities).issubset(parent.capabilities)
        and all(source in parent.sources or source == occurrence_source(grant) for source in grant.sources)
    )


async def service_product_scope(
    db: AsyncSession, actor: TokenContext, delegation_id: str | None, product_id: str, project_id: str, operation: str
) -> CredentialServiceDelegation | None:
    return await _service_product_scope(db, actor, delegation_id, product_id, project_id, operation, getattr(actor, "agent_registry_id", ""))


async def _service_product_scope(db, actor, delegation_id, product_id, project_id, operation, delegated_service_id):
    """Shared current-owner checks; callers own any narrow service mediation."""
    from src.auth.vault_service import _visible_credential_filter

    if actor.account_type != "service" or actor.auth_source != "iam" or not actor.agent_registry_id or not delegation_id:
        return None
    grant = await db.scalar(
        select(CredentialServiceDelegation).where(CredentialServiceDelegation.id == delegation_id).execution_options(populate_existing=True)
    )
    credential = (
        await db.scalar(select(UserCredential).where(UserCredential.id == grant.credential_id).execution_options(populate_existing=True))
        if grant is not None
        else None
    )
    credential_expiry = credential.expires_at if credential is not None else None
    if credential_expiry is not None and credential_expiry.tzinfo is None:
        credential_expiry = credential_expiry.replace(tzinfo=UTC)
    if (
        grant is None
        or grant.service_id != delegated_service_id
        or (credential is None and grant.installation_id is None)
        or (credential is not None and credential.org_id != grant.tenant_id)
        or (credential_expiry is not None and credential_expiry <= utcnow())
        or grant.product_id != product_id
        or grant.project_id != project_id
        or grant.revoked_at is not None
        or (grant.expires_at.replace(tzinfo=UTC) if grant.expires_at.tzinfo is None else grant.expires_at) <= utcnow()
        or operation not in grant.operations
    ):
        return None
    if not await current_l5_parent(db, grant):
        return None
    member = await product_member(db, user_id=grant.created_by, org_id=grant.tenant_id)
    if member is None or not await db.scalar(select(Product.id).where(Product.id == product_id, owner_scope(member, grant.tenant_id))):
        return None
    owner = actor.model_copy(
        update={
            "account_type": "human",
            "user_id": member.id,
            "team_id": member.team_id,
            "org_id": grant.tenant_id,
            "is_admin": False,
        }
    )
    if grant.installation_id is not None:
        from src.admin.installations.resolver import InstallationOwnershipError, assert_installation_owned_by

        installation = await db.get(ChannelTenantMap, grant.installation_id, populate_existing=True)
        if (
            grant.credential_id is not None
            or installation is None
            or installation.org_id != grant.tenant_id
            or installation.provider != "github"
            or installation.ownership_disputed
            or not installation.installation_id
            or (installation.provider_scope_id.startswith("personal:") and installation.installed_by_user_id != member.id)
        ):
            return None
        try:
            await assert_installation_owned_by(grant.tenant_id, int(installation.installation_id), db=db)
        except (InstallationOwnershipError, ValueError):
            return None
        return grant
    visible = _visible_credential_filter(owner)
    if not visible or not await db.scalar(select(UserCredential.id).where(UserCredential.id == credential.id, or_(*visible))):
        return None
    return grant
