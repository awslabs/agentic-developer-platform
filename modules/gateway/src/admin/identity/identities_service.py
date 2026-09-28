"""User identity linkage CRUD service.

Issue #387: Cross-channel identity linkage for existing users.
Issue #401: DDB write-through for channel_user entries on add/delete.

Allows adding/removing provider identities (e.g. linking a Slack account to a GitHub user).
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity

from .identity_index_writer import IdentityIndexWriter
from .schemas import IdentityCreateRequest, IdentityResponse

logger = logging.getLogger(__name__)


def _identity_to_response(identity: UserIdentity) -> IdentityResponse:
    """Convert a UserIdentity model to API response."""
    return IdentityResponse(
        id=identity.id,
        user_id=identity.user_id,
        org_id=identity.org_id,
        team_id=identity.team_id,
        provider=identity.provider,
        provider_user_id=identity.provider_user_id,
        provider_username=identity.provider_username,
        verification_method=identity.verification_method,
        verified_at=identity.verified_at,
        created_at=identity.created_at,
    )


class IdentitiesService:
    """CRUD for user identity linkage with DDB write-through."""

    def __init__(
        self,
        db: AsyncSession,
        identity_writer: IdentityIndexWriter | None = None,
    ):
        self._db = db
        self._identity_writer = identity_writer

    async def add_identity(self, user_id: str, req: IdentityCreateRequest) -> IdentityResponse | None:
        """Add a new identity to an existing user.

        Writes to Postgres then write-through to DDB channel_user entry.
        Returns None if user not found.
        """
        # Verify user exists
        result = await self._db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            return None

        identity = UserIdentity(
            org_id=user.org_id,
            team_id=user.team_id,
            user_id=user_id,
            provider=req.provider,
            provider_user_id=req.provider_user_id,
            provider_username=req.provider_username,
            verification_method="admin_manual",
        )
        self._db.add(identity)
        await self._db.commit()
        await self._db.refresh(identity)

        # Post-commit: write channel_user entry to DDB (best-effort)
        if self._identity_writer:
            try:
                await self._identity_writer.put_user_identity(
                    provider_user_id=req.provider_user_id,
                    user_id=user_id,
                    org_id=user.org_id,
                    provider_username=req.provider_username,
                )
            except Exception:
                logger.exception(
                    "DDB write-through failed for identity %s (non-fatal)",
                    req.provider_user_id,
                )

        logger.info(
            "audit: identity_added user_id=%s provider=%s provider_user_id=%s",
            user_id,
            req.provider,
            req.provider_user_id,
        )
        return _identity_to_response(identity)

    async def list_identities(self, user_id: str) -> list[IdentityResponse]:
        """List all identities for a user."""
        result = await self._db.execute(select(UserIdentity).where(UserIdentity.user_id == user_id))
        identities = result.scalars().all()
        return [_identity_to_response(i) for i in identities]

    async def delete_identity(self, user_id: str, identity_id: str) -> bool:
        """Delete an identity from a user.

        Removes from Postgres then deletes the channel_user DDB row.
        Returns False if not found.
        """
        result = await self._db.execute(
            select(UserIdentity).where(
                UserIdentity.id == identity_id,
                UserIdentity.user_id == user_id,
            )
        )
        identity = result.scalar_one_or_none()
        if identity is None:
            return False

        provider_user_id = identity.provider_user_id
        await self._db.delete(identity)
        await self._db.commit()

        # Post-commit: remove channel_user entry from DDB (best-effort)
        if self._identity_writer:
            try:
                await self._identity_writer.delete_user_identity(provider_user_id)
            except Exception:
                logger.exception(
                    "DDB delete failed for identity %s (non-fatal)",
                    provider_user_id,
                )

        logger.info(
            "audit: identity_deleted user_id=%s identity_id=%s provider=%s",
            user_id,
            identity_id,
            identity.provider,
        )
        return True
