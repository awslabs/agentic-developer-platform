"""Transactional organization CRUD service.

Issue #387: Single authoritative writer for tenant records.
Pattern: Postgres transaction first, then DDB write-through + Cognito side-effects post-commit.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.organization import Department, Organization, Team
from src.shared.models.vault import ChannelTenantMap

from .cognito_sync import CognitoSyncService
from .identity_index_writer import IdentityIndexWriter
from .schemas import (
    ChannelsConfig,
    OrganizationCreateRequest,
    OrganizationResponse,
    OrganizationUpdateRequest,
)

logger = logging.getLogger(__name__)


def _extract_github_installation_ids(channels: ChannelsConfig) -> list[str]:
    """Extract GitHub installation IDs from channels config."""
    return [entry.installation_id for entry in channels.github if entry.installation_id]


def _org_to_response(org: Organization) -> OrganizationResponse:
    """Convert an Organization model to API response."""
    # Reconstruct channels from settings if stored there
    channels = None
    if org.settings and "channels" in org.settings:
        channels = ChannelsConfig(**org.settings["channels"])

    return OrganizationResponse(
        id=org.id,
        name=org.name,
        plan=org.settings.get("plan") if org.settings else None,
        channels=channels,
        aws_accounts=org.settings.get("aws_accounts", []) if org.settings else [],
        settings=org.settings or {},
        github_installation_ids=org.github_installation_ids or [],
        cognito_client_ids=org.cognito_client_ids or [],
        created_at=org.created_at,
    )


class OrganizationsService:
    """Transactional organization CRUD with post-commit side-effects."""

    def __init__(
        self,
        db: AsyncSession,
        identity_index: IdentityIndexWriter | None = None,
        cognito_sync: CognitoSyncService | None = None,
    ):
        self._db = db
        self._identity_index = identity_index or IdentityIndexWriter()
        self._cognito_sync = cognito_sync or CognitoSyncService()

    async def create_organization(self, req: OrganizationCreateRequest) -> OrganizationResponse:
        """Create org + default dept + team + channel_tenant_map in one transaction.

        Post-commit: DDB write-through + Cognito group creation.
        """
        github_ids = _extract_github_installation_ids(req.channels)

        # Build settings JSON
        settings = {
            "plan": req.plan,
            "channels": req.channels.model_dump(),
            "aws_accounts": [a.model_dump() for a in req.aws_accounts],
            "user_auto_provision_mode": req.settings.user_auto_provision_mode,
        }

        # Step 1-4: All in one Postgres transaction
        org = Organization(
            id=req.id,
            name=req.name,
            aws_accounts=[a.model_dump() for a in req.aws_accounts],
            settings=settings,
            github_installation_ids=github_ids,
            cognito_client_ids=[],
        )
        self._db.add(org)

        # Default department
        dept_id = f"{req.id}-dept-default"
        dept = Department(
            id=dept_id,
            org_id=req.id,
            name="Default",
            description="Default department",
        )
        self._db.add(dept)

        # Default team
        team_id = f"{req.id}-team-default"
        team = Team(
            id=team_id,
            org_id=req.id,
            department_id=dept_id,
            name="Default",
            description="Default team",
        )
        self._db.add(team)

        # Channel tenant map entries
        for entry in req.channels.github:
            if entry.installation_id:
                self._db.add(
                    ChannelTenantMap(
                        provider="github",
                        provider_scope_id=entry.installation_id,
                        org_id=req.id,
                    )
                )
        for entry in req.channels.slack:
            if entry.workspace_id:
                self._db.add(
                    ChannelTenantMap(
                        provider="slack",
                        provider_scope_id=entry.workspace_id,
                        org_id=req.id,
                    )
                )
        for entry in req.channels.whatsapp:
            if entry.workspace_id:
                self._db.add(
                    ChannelTenantMap(
                        provider="whatsapp",
                        provider_scope_id=entry.workspace_id,
                        org_id=req.id,
                    )
                )

        # Commit the transaction
        await self._db.commit()
        await self._db.refresh(org)

        logger.info("Organization created: %s", req.id)

        # Post-commit side-effects (best-effort, do not roll back Postgres)
        # Step 6: DDB write-through
        await self._identity_index.sync_org_channels(
            org_id=req.id,
            github_installation_ids=github_ids,
            cognito_client_ids=[],
        )

        # Step 7: Cognito group creation (idempotent)
        await self._cognito_sync.ensure_org_group(req.id)

        # Step 8: Audit event
        logger.info(
            "audit: organization_created org_id=%s name=%s plan=%s",
            req.id,
            req.name,
            req.plan,
        )

        return _org_to_response(org)

    async def get_organization(self, org_id: str) -> OrganizationResponse | None:
        """Get a single organization by ID."""
        result = await self._db.execute(select(Organization).where(Organization.id == org_id))
        org = result.scalar_one_or_none()
        if org is None:
            return None
        return _org_to_response(org)

    async def list_organizations(self) -> list[OrganizationResponse]:
        """List all organizations."""
        result = await self._db.execute(select(Organization).order_by(Organization.name))
        orgs = result.scalars().all()
        return [_org_to_response(org) for org in orgs]

    async def update_organization(self, org_id: str, req: OrganizationUpdateRequest) -> OrganizationResponse | None:
        """Update an organization. Returns None if not found."""
        result = await self._db.execute(select(Organization).where(Organization.id == org_id))
        org = result.scalar_one_or_none()
        if org is None:
            return None

        old_github_ids = list(org.github_installation_ids or [])

        if req.name is not None:
            org.name = req.name

        settings = dict(org.settings or {})
        if req.plan is not None:
            settings["plan"] = req.plan
        if req.channels is not None:
            settings["channels"] = req.channels.model_dump()
            new_github_ids = _extract_github_installation_ids(req.channels)
            org.github_installation_ids = new_github_ids

            # Update channel_tenant_map: delete old, insert new
            await self._db.execute(ChannelTenantMap.__table__.delete().where(ChannelTenantMap.org_id == org_id))
            for entry in req.channels.github:
                if entry.installation_id:
                    self._db.add(
                        ChannelTenantMap(
                            provider="github",
                            provider_scope_id=entry.installation_id,
                            org_id=org_id,
                        )
                    )
            for entry in req.channels.slack:
                if entry.workspace_id:
                    self._db.add(
                        ChannelTenantMap(
                            provider="slack",
                            provider_scope_id=entry.workspace_id,
                            org_id=org_id,
                        )
                    )
        if req.aws_accounts is not None:
            settings["aws_accounts"] = [a.model_dump() for a in req.aws_accounts]
            org.aws_accounts = [a.model_dump() for a in req.aws_accounts]
        if req.settings is not None:
            settings["user_auto_provision_mode"] = req.settings.user_auto_provision_mode

        org.settings = settings
        await self._db.commit()
        await self._db.refresh(org)

        # Post-commit: sync DDB if channels changed
        if req.channels is not None:
            new_github_ids = _extract_github_installation_ids(req.channels)
            await self._identity_index.sync_org_channels(
                org_id=org_id,
                github_installation_ids=new_github_ids,
                cognito_client_ids=org.cognito_client_ids or [],
                old_github_installation_ids=old_github_ids,
            )

        logger.info("audit: organization_updated org_id=%s", org_id)
        return _org_to_response(org)

    async def delete_organization(self, org_id: str) -> bool:
        """Soft-delete (archive) an organization. Returns False if not found."""
        result = await self._db.execute(select(Organization).where(Organization.id == org_id))
        org = result.scalar_one_or_none()
        if org is None:
            return False

        # Soft-delete: set status in settings
        settings = dict(org.settings or {})
        settings["status"] = "archived"
        org.settings = settings
        await self._db.commit()

        # Post-commit: remove from DDB
        await self._identity_index.delete_org_identities(
            github_installation_ids=org.github_installation_ids or [],
            cognito_client_ids=org.cognito_client_ids or [],
        )

        logger.info("audit: organization_archived org_id=%s", org_id)
        return True
