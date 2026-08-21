"""Transactional user CRUD service.

Issue #387: Single authoritative writer for user records within an organization.
Issue #401: DDB write-through for channel_user identity entries.

Pattern: Postgres transaction first, then DDB write-through + Cognito invite post-commit.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.memberships import is_admin_level_role, upsert_tenant_membership
from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity

from .cognito_sync import CognitoSyncService
from .identity_index_writer import IdentityIndexWriter
from .schemas import UserCreateRequest, UserResponse

logger = logging.getLogger(__name__)


def _user_to_response(user: User) -> UserResponse:
    """Convert a User model to API response."""
    return UserResponse(
        id=user.id,
        org_id=user.org_id,
        team_id=user.team_id,
        email=user.email,
        name=user.name,
        role=user.role,
        cognito_sub=user.cognito_sub,
        created_at=user.created_at,
    )


class UsersService:
    """Transactional user CRUD with post-commit DDB + Cognito side-effects."""

    def __init__(
        self,
        db: AsyncSession,
        cognito_sync: CognitoSyncService | None = None,
        identity_writer: IdentityIndexWriter | None = None,
    ):
        self._db = db
        self._cognito_sync = cognito_sync or CognitoSyncService()
        self._identity_writer = identity_writer

    async def create_user(self, org_id: str, req: UserCreateRequest) -> UserResponse:
        """Create user + identities in Postgres, then write-through to DDB and Cognito.

        Args:
            org_id: Organization ID (from URL path).
            req: User creation request.

        Returns:
            Created user response.
        """
        # Default team_id if not provided
        team_id = req.team_id or f"{org_id}-team-default"

        # Step 1: Insert user
        user = User(
            org_id=org_id,
            team_id=team_id,
            email=req.email,
            name=req.name,
            role=req.role,
        )
        self._db.add(user)
        await self._db.flush()  # Get the generated ID

        # Step 2: Insert identities
        github_username = None
        for identity in req.identities:
            self._db.add(
                UserIdentity(
                    org_id=org_id,
                    team_id=team_id,
                    user_id=user.id,
                    provider=identity.provider,
                    provider_user_id=identity.provider_user_id,
                    provider_username=identity.provider_username,
                    verification_method="admin_manual",
                )
            )
            if identity.provider == "github" and identity.provider_username:
                github_username = identity.provider_username

        # Step 2b (Issue #4006): if this create grants admin-level authority, the
        # tenant_memberships row is what actually carries it (#3987/#3998) — write
        # it in the same transaction, or the new admin is a "no-row" principal that
        # loses its authority when the legacy fallback flips to least-privilege.
        if is_admin_level_role(req.role):
            await upsert_tenant_membership(
                self._db,
                user_id=user.id,
                tenant_id=org_id,
                role=req.role,
                joined_via="admin_create",
            )

        # Step 3: Commit transaction
        await self._db.commit()
        await self._db.refresh(user)

        logger.info("User created: %s in org %s", user.id, org_id)

        # Step 4: Post-commit — DDB write-through for channel_user entries
        if self._identity_writer and req.identities:
            try:
                await self._identity_writer.sync_user_identities(
                    user_id=user.id,
                    org_id=org_id,
                    identities=[
                        {
                            "provider_user_id": ident.provider_user_id,
                            "provider_username": ident.provider_username,
                        }
                        for ident in req.identities
                    ],
                )
            except Exception:
                logger.exception("DDB write-through failed for user %s (non-fatal)", user.id)

        # Step 5: Post-commit — Cognito user creation + invite
        dept_id = f"{org_id}-dept-default"
        await self._cognito_sync.create_user_and_invite(
            email=req.email,
            org_id=org_id,
            dept_id=dept_id,
            team_id=team_id,
            name=req.name,
            role=req.role,
            send_invite=req.send_invite,
            github_username=github_username,
        )

        # Audit
        logger.info("audit: user_created user_id=%s org_id=%s email=%s", user.id, org_id, req.email)

        return _user_to_response(user)

    async def list_users(self, org_id: str) -> list[UserResponse]:
        """List all users in an organization."""
        result = await self._db.execute(select(User).where(User.org_id == org_id).order_by(User.email))
        users = result.scalars().all()
        return [_user_to_response(u) for u in users]

    async def get_user(self, user_id: str) -> UserResponse | None:
        """Get a user by ID."""
        result = await self._db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            return None
        return _user_to_response(user)

    async def delete_user(self, org_id: str, user_id: str) -> bool:
        """Delete a user from the organization.

        Removes from Postgres, then deletes all channel_user DDB entries + Cognito user.
        Returns False if not found.
        """
        result = await self._db.execute(select(User).where(User.id == user_id, User.org_id == org_id))
        user = result.scalar_one_or_none()
        if user is None:
            return False

        # Query all identities for this user before deleting (for DDB cleanup)
        identities_result = await self._db.execute(select(UserIdentity).where(UserIdentity.user_id == user_id))
        identities = identities_result.scalars().all()
        provider_user_ids = [i.provider_user_id for i in identities]

        # Explicitly delete identities first (portable across DBs)
        for ident in identities:
            await self._db.delete(ident)

        email = user.email
        await self._db.delete(user)
        await self._db.commit()

        # Post-commit: remove channel_user entries from DDB (best-effort)
        if self._identity_writer and provider_user_ids:
            try:
                await self._identity_writer.delete_all_user_identities(provider_user_ids)
            except Exception:
                logger.exception("DDB delete failed for user %s identities (non-fatal)", user_id)

        # Post-commit: remove from Cognito (best-effort)
        await self._cognito_sync.delete_user(email)

        logger.info("audit: user_deleted user_id=%s org_id=%s", user_id, org_id)
        return True
