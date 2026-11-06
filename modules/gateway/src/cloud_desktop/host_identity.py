"""Server-recorded ownership of the current assistant host generation."""

import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.dev_box import DevBox, DevBoxAccessSession, DevBoxDevice, DevBoxInstance, DevBoxPolicy, DevBoxTemplate
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext


async def ec2_crew_allowed(db: AsyncSession, org_id: str) -> bool:
    from src.crew.targets import CrewTargetPolicy

    settings = await db.scalar(select(Organization.settings).where(Organization.id == org_id))
    if not isinstance(settings, dict):
        return False
    try:
        return "ec2-instance" in CrewTargetPolicy.from_tenant_settings(settings).allowed_targets
    except (TypeError, ValueError):
        return False


async def current_host(
    db: AsyncSession,
    *,
    box_id: str,
    generation: int,
    provider_instance_id: str,
    user_id: str,
    org_id: str,
    deployment_id: str,
    owner: TokenContext | None = None,
    run_id: str | None = None,
) -> DevBoxInstance | None:
    """Check recorded scope after independent controller instance attestation."""
    if not provider_instance_id or isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        return None
    if owner is not None:
        if owner.account_type != "human" or owner.org_id != org_id:
            return None
        # Same owner resolver as the Crew and Dev Box routes (#8108): the recorded box
        # owner must be the caller's tenant member row, not whichever row their sub names.
        # Imported here: chat_authority reaches this module through the crew proxy.
        from src.agentauth.chat_authority import tenant_member_principal
        from src.agentauth.chat_capability import ChatAuthorizationRefusedError

        try:
            resolved = await tenant_member_principal(db, org_id, owner.user_id, username=owner.cognito_username)
        except ChatAuthorizationRefusedError:
            return None
        if resolved.id != user_id or resolved.is_shadow:
            return None
    human = await db.scalar(select(User.id).where(User.id == user_id, User.org_id == org_id, User.user_kind == "human", User.is_shadow.is_(False)))
    if human is None:
        return None
    revoked = await db.scalar(
        select(TenantMembership.id).where(
            TenantMembership.user_id == user_id,
            TenantMembership.tenant_id == org_id,
            TenantMembership.revoked_at.is_not(None),
        )
    )
    if revoked is not None:
        return None
    box = await db.scalar(
        select(DevBox).where(
            DevBox.id == box_id,
            DevBox.user_id == user_id,
            DevBox.org_id == org_id,
            DevBox.deployment_id == deployment_id,
            DevBox.machine_generation == generation,
            DevBox.status == "running",
            DevBox.desired_state == "running",
            DevBox.observed_state == "running",
        )
    )
    if box is None or (
        box.lease_expires_at is not None and box.lease_expires_at.replace(tzinfo=box.lease_expires_at.tzinfo or UTC) <= datetime.now(UTC)
    ):
        return None
    policy = await db.scalar(
        select(DevBoxPolicy).where(
            DevBoxPolicy.org_id == org_id,
            DevBoxPolicy.deployment_id == deployment_id,
            DevBoxPolicy.enabled.is_(True),
        )
    )
    template = await db.get(DevBoxTemplate, box.template_id, populate_existing=True)
    if policy is None or template is None or not template.approved:
        return None
    if box.profile == "crew" and not await ec2_crew_allowed(db, org_id):
        return None
    if run_id:
        if box.auth_readiness != "ready":
            return None
        authorized = await db.scalar(
            select(DevBoxAccessSession.id)
            .join(
                DevBoxDevice,
                DevBoxDevice.id == DevBoxAccessSession.device_id,
            )
            .where(
                DevBoxAccessSession.box_id == box_id,
                DevBoxAccessSession.user_id == user_id,
                DevBoxAccessSession.org_id == org_id,
                DevBoxAccessSession.deployment_id == deployment_id,
                DevBoxAccessSession.machine_generation == generation,
                DevBoxAccessSession.provider_session_ref == run_id,
                DevBoxAccessSession.terminated_at.is_(None),
                DevBoxAccessSession.expires_at > datetime.now(UTC),
                DevBoxDevice.revoked_at.is_(None),
                DevBoxDevice.machine_generation == generation,
            )
        )
        if authorized is None:
            return None
    return await db.scalar(
        select(DevBoxInstance).where(
            DevBoxInstance.box_id == box_id,
            DevBoxInstance.user_id == user_id,
            DevBoxInstance.org_id == org_id,
            DevBoxInstance.deployment_id == deployment_id,
            DevBoxInstance.machine_generation == generation,
            DevBoxInstance.provider_instance_id == provider_instance_id,
            DevBoxInstance.observed_state == "running",
        )
    )


async def store_host_fingerprint(
    db: AsyncSession,
    *,
    box_id: str,
    generation: int,
    provider_instance_id: str,
    user_id: str,
    org_id: str,
    deployment_id: str,
    fingerprint: str,
) -> bool:
    """Record an attested box fingerprint once; the caller must attest its source."""
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return False
    box = await db.scalar(select(DevBox).where(DevBox.id == box_id).with_for_update())
    if box is None or box.machine_generation != generation or box.profile != "crew":
        return False
    host = await current_host(
        db,
        box_id=box_id,
        generation=generation,
        provider_instance_id=provider_instance_id,
        user_id=user_id,
        org_id=org_id,
        deployment_id=deployment_id,
    )
    if host is None or not host.private_address:
        return False
    instance = await db.scalar(select(DevBoxInstance).where(DevBoxInstance.id == host.id).with_for_update())
    if instance is None or instance.certificate_fingerprint not in (None, fingerprint):
        return False
    instance.certificate_fingerprint = fingerprint
    await db.flush()
    return True
