"""Transactional user CRUD service.

Issue #387: Single authoritative writer for user records within an organization.
Issue #401: DDB write-through for channel_user identity entries.

Pattern: Postgres transaction first, then DDB write-through + Cognito invite post-commit.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.cognito_service import UserAlreadyExistsError
from src.admin.config import membership_role_to_admin_role
from src.admin.memberships import project_member_org_ids, upsert_tenant_membership
from src.shared.exceptions import BedrockGatewayError, ConflictError, NotFoundError
from src.shared.identity.workspaces import link_login_to_workspace, login_subject_for_user
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, Team, User
from src.shared.models.vault import UserIdentity

from .cognito_sync import CognitoSyncService, cognito_identity
from .identity_index_writer import IdentityIndexWriter
from .schemas import CognitoLinkRequest, UserCreateRequest, UserResponse

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
        cognito_username=user.cognito_username,
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
        # Serialize native creates within an org, including requests for
        # different teams, before checking for an existing provisioning row.
        org = await self._db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
        if org is None:
            raise NotFoundError("Organization does not exist")
        team = await self._db.scalar(select(Team).where(Team.id == team_id, Team.org_id == org_id))
        if team is None:
            raise NotFoundError("Team does not exist in the requested organization")
        existing = await self._db.scalar(select(User).where(User.org_id == org_id, User.email == req.email).limit(1))
        if existing:
            raise self._provisioning_error(existing.id, org_id, "An ADP user already exists; use its provisioning or Cognito-link endpoint", 409)

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

        # Every native user needs a real membership, including ordinary members.
        await upsert_tenant_membership(self._db, user_id=user.id, tenant_id=org_id, role=req.role, joined_via="admin_create")

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

        # Issue #4849: sync_user_identities above calls put_user_identity WITHOUT
        # member_org_ids, which takes the UpdateItem branch that deliberately
        # *preserves* an existing member_org_ids — correct for wipe-safety, but it
        # means a membership written by this path was never projected. Must run
        # after the identity rows exist, so the targeted update has a row to hit.
        await project_member_org_ids(self._db, user_id=user.id, writer=self._identity_writer)

        # Step 5: Post-commit — Cognito user creation + invite
        if req.cognito_identity:
            return await self.link_cognito_user(org_id, user.id, req.cognito_identity)
        return await self.provision_user(org_id, user.id, send_invite=req.send_invite, github_username=github_username)

    @staticmethod
    def _provisioning_error(user_id: str, org_id: str, message: str, status_code: int = 502) -> BedrockGatewayError:
        base = f"/admin/identity/organizations/{org_id}/users/{user_id}"
        return BedrockGatewayError(
            "cognito_provisioning_failed",
            message,
            status_code,
            {"user_id": user_id, "org_id": org_id, "retry_path": f"{base}/provision", "link_path": f"{base}/cognito"},
        )

    async def _locked_user(self, org_id: str, user_id: str) -> User:
        user = await self._db.scalar(
            select(User).where(User.id == user_id, User.org_id == org_id).with_for_update().execution_options(populate_existing=True)
        )
        if user is None:
            raise NotFoundError("ADP user does not exist in the requested organization")
        return user

    async def _bind_cognito_identity(self, user: User, result: dict) -> None:
        subject, username = cognito_identity(result)
        previous = await login_subject_for_user(self._db, user)
        if previous and previous != subject:
            raise ConflictError("ADP user already belongs to a different Cognito subject")
        canonical = await self._db.scalar(select(User).where(User.cognito_sub == subject).with_for_update())
        if canonical and canonical.id != user.id:
            if canonical.org_id == user.org_id:
                raise ConflictError("Cognito subject already belongs to another user in this organization")
            # Secondary orgs use the established, verified placement link. The
            # unique canonical sub remains the one login/person identity.
            await link_login_to_workspace(self._db, canonical, user)
        else:
            user.cognito_sub = subject
            user.cognito_username = username
        if not await self._db.scalar(
            select(TenantMembership.id).where(TenantMembership.user_id == user.id, TenantMembership.tenant_id == user.org_id)
        ):
            # Healing an old unlinked row never turns its display role into
            # authority. New creates already wrote the explicitly granted role.
            await upsert_tenant_membership(self._db, user_id=user.id, tenant_id=user.org_id, role="member", joined_via="cognito_link")
        user.is_shadow = False
        await self._db.commit()
        await self._db.refresh(user)

    async def provision_user(self, org_id: str, user_id: str, *, send_invite: bool = False, github_username: str | None = None) -> UserResponse:
        """Retry the stable ADP row, never re-adopt a Cognito user by email.

        A lost AdminCreateUser response requires explicit subject verification
        through link_cognito_user. A persisted subject can be retried safely.
        """
        user = await self._locked_user(org_id, user_id)
        try:
            subject = await login_subject_for_user(self._db, user)
            if subject:
                canonical = await self._db.scalar(select(User).where(User.cognito_sub == subject))
                if canonical is None or not canonical.cognito_username:
                    raise ConflictError("Use the Cognito-link endpoint to verify this login's username and subject")
                result = await self._cognito_sync.verified_user(canonical.cognito_username, subject)
            else:
                team = await self._db.scalar(select(Team).where(Team.id == user.team_id, Team.org_id == org_id))
                if team is None:
                    raise ConflictError("User has no valid team in the requested organization")
                membership = await self._db.scalar(
                    select(TenantMembership).where(TenantMembership.user_id == user.id, TenantMembership.tenant_id == org_id)
                )
                # Display roles on old unlinked rows are not authority. The
                # membership was explicitly authorized at create/placement.
                role = membership_role_to_admin_role(membership.role if membership else "member").value
                result = await self._cognito_sync.create_user_and_invite(
                    email=user.email,
                    org_id=org_id,
                    dept_id=team.department_id,
                    team_id=team.id,
                    name=user.name,
                    role=role,
                    send_invite=send_invite,
                    github_username=github_username,
                )
            await self._bind_cognito_identity(user, result)
            # Persist the immutable result BEFORE group sync so a failed group
            # operation can be retried without resetting or recreating the login.
            _, username = cognito_identity(result)
            await self._cognito_sync.ensure_user_group(username, org_id)
        except Exception as exc:
            await self._db.rollback()
            logger.warning("Cognito provisioning incomplete for ADP user %s (%s)", user_id, type(exc).__name__)
            status = 409 if isinstance(exc, ConflictError | UserAlreadyExistsError) else 502
            message = (
                "Cognito identity conflict. Verify the existing username and immutable subject using the Cognito-link endpoint."
                if status == 409
                else "ADP user was saved, but Cognito provisioning failed. Retry provisioning this user."
            )
            raise self._provisioning_error(user_id, org_id, message, status) from exc
        logger.info("audit: user_provisioned user_id=%s org_id=%s", user_id, org_id)
        return _user_to_response(user)

    async def link_cognito_user(self, org_id: str, user_id: str, req: CognitoLinkRequest) -> UserResponse:
        """Platform-admin-only Cognito-first onboarding, verified in the pool."""
        user = await self._locked_user(org_id, user_id)
        try:
            result = await self._cognito_sync.verified_user(req.username, req.expected_sub)
            await self._bind_cognito_identity(user, result)
            _, username = cognito_identity(result)
            await self._cognito_sync.ensure_user_group(username, org_id)
        except Exception as exc:
            await self._db.rollback()
            status = 409 if isinstance(exc, ConflictError | ValueError) else 502
            message = "Cognito identity could not be linked. Verify the username and immutable subject; existing identity ownership is preserved."
            raise self._provisioning_error(user_id, org_id, message, status) from exc
        logger.info("audit: cognito_identity_linked user_id=%s org_id=%s", user_id, org_id)
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

        Uses the shared membership-removal guards and FK cleanup, then syncs
        channel identities and deletes only an unshared, verified Cognito login.
        Returns False if not found.
        """
        result = await self._db.execute(select(User).where(User.id == user_id, User.org_id == org_id))
        user = result.scalar_one_or_none()
        if user is None:
            return False

        # Query all identities for this user before deleting (for DDB cleanup)
        identities_result = await self._db.execute(select(UserIdentity).where(UserIdentity.user_id == user_id))
        identities = identities_result.scalars().all()
        # GitHub membership projection is handled by remove_user and must not
        # be deleted afterward: another org may still share that identity.
        provider_user_ids = [i.provider_user_id for i in identities if i.provider not in {"github", "cognito"}]

        # Reuse the canonical membership removal guards and explicit FK cleanup.
        # In particular, deleting a login anchor must not strand another org's
        # workspace, and deleting a secondary row must not delete the login.
        from src.admin.service import AdminService

        username = user.cognito_username if user.cognito_sub else None
        await AdminService(self._db).remove_user(org_id, user_id, identity_writer=self._identity_writer)

        # Post-commit: remove channel_user entries from DDB (best-effort)
        if self._identity_writer and provider_user_ids:
            try:
                await self._identity_writer.delete_all_user_identities(provider_user_ids)
            except Exception:
                logger.exception("DDB delete failed for user %s identities (non-fatal)", user_id)

        # Post-commit: remove from Cognito (best-effort)
        if username:
            await self._cognito_sync.delete_user(username)

        logger.info("audit: user_deleted user_id=%s org_id=%s", user_id, org_id)
        return True
