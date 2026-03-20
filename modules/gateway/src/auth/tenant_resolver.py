"""
Tenant Resolver for mapping AWS STS identity to organization/department/team structures.

This module resolves AWS STS GetCallerIdentity responses to internal tenant information,
handling:
- Human users via AWS SSO (Identity Center)
- Human users via Cognito authentication
- Service accounts (IAM roles)
"""

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.exceptions import UnknownOrganizationError, UnregisteredServiceAccountError
from src.shared.models.organization import Department, Organization, ServiceAccount, Team, User

from .exceptions import TenantResolutionError
from .schemas import AWSCallerIdentity, TenantInfo

logger = logging.getLogger(__name__)


# Constants for Cognito gateway-caller role detection
COGNITO_GATEWAY_CALLER_PATTERN = re.compile(r"gateway-caller|cognito-identity")


@dataclass
class CognitoTenantInfo:
    """Tenant information extracted from Cognito session."""

    org_id: str | None = None
    department_id: str | None = None
    team_id: str | None = None
    role: str | None = None


class TenantResolver:
    """
    Resolves AWS STS identity information to internal tenant structures.

    Handles:
    - AWS account to organization mapping
    - Service account resolution via IAM role ARN
    - Human user resolution via Identity Center integration
    - Human user resolution via Cognito authentication
    - Unknown organization handling (US-9.2)
    - Unregistered service account handling (US-9.5)
    """

    def __init__(self):
        """Initialize the tenant resolver."""
        pass

    def is_cognito_user(self, role_arn: str) -> bool:
        """
        Check if the role ARN indicates a Cognito-authenticated user.

        Cognito-authenticated users use roles containing 'gateway-caller' or
        'cognito-identity' in the role name.

        Args:
            role_arn: IAM role ARN

        Returns:
            bool: True if this is a Cognito-authenticated user
        """
        if not role_arn:
            return False
        return bool(COGNITO_GATEWAY_CALLER_PATTERN.search(role_arn))

    def extract_cognito_tenant_info(self, caller_identity: AWSCallerIdentity) -> CognitoTenantInfo:
        """
        Extract tenant information from Cognito session.

        Cognito users have their tenant info (org_id, department_id, team_id)
        encoded in the session name or available via session tags.

        Session name format: {org_id}_{department_id}_{team_id}_{user_id}
        Or via STS session tags: principalTag/org_id, principalTag/department_id, etc.

        Args:
            caller_identity: AWS caller identity response

        Returns:
            CognitoTenantInfo: Extracted tenant information
        """
        info = CognitoTenantInfo()

        # Try to extract from session name (last part of assumed-role ARN)
        if caller_identity.arn and ":assumed-role/" in caller_identity.arn:
            parts = caller_identity.arn.split("/")
            if len(parts) >= 3:
                session_name = parts[-1]
                # Try parsing session name for tenant info
                # Format: org-{org_id}_dept-{dept_id}_team-{team_id}
                session_parts = session_name.split("_")
                for part in session_parts:
                    if part.startswith("org-"):
                        info.org_id = part[4:]  # Remove "org-" prefix
                    elif part.startswith("dept-"):
                        info.department_id = part[5:]  # Remove "dept-" prefix
                    elif part.startswith("team-"):
                        info.team_id = part[5:]  # Remove "team-" prefix
                    elif part.startswith("role-"):
                        info.role = part[5:]  # Remove "role-" prefix

        # Note: In production, you would also check STS session tags
        # which can be set when the Identity Pool issues credentials.
        # This would require making an additional STS:GetSessionToken or
        # examining the token claims.

        return info

    async def resolve_tenant(self, caller_identity: AWSCallerIdentity, db: AsyncSession) -> TenantInfo:
        """
        Resolve AWS caller identity to tenant information.

        Args:
            caller_identity: AWS STS GetCallerIdentity response
            db: Database session

        Returns:
            TenantInfo: Resolved tenant information

        Raises:
            UnknownOrganizationError: If AWS account is not registered (US-9.2)
            UnregisteredServiceAccountError: If service account is not registered (US-9.5)
            TenantResolutionError: If tenant resolution fails
        """
        try:
            logger.debug(f"Resolving tenant for ARN: {caller_identity.arn}")

            # Check if this is a Cognito-authenticated user
            if self.is_cognito_user(caller_identity.arn):
                return await self._resolve_cognito_user(caller_identity, db)

            # First, find the organization for this AWS account
            organization = await self._find_organization_by_account(caller_identity.account, db)
            if not organization:
                logger.warning(f"Unknown organization for AWS account: {caller_identity.account}")
                raise UnknownOrganizationError(caller_identity.account)

            # Determine if this is a service account or human user
            if self._is_service_account(caller_identity.arn):
                return await self._resolve_service_account(caller_identity, organization, db)
            else:
                return await self._resolve_human_user(caller_identity, organization, db)

        except (UnknownOrganizationError, UnregisteredServiceAccountError):
            # Re-raise specific exceptions as-is
            raise
        except Exception as e:
            logger.error(f"Failed to resolve tenant for ARN {caller_identity.arn}: {e}")
            raise TenantResolutionError(f"Tenant resolution failed: {str(e)}")

    async def _resolve_cognito_user(self, caller_identity: AWSCallerIdentity, db: AsyncSession) -> TenantInfo:
        """
        Resolve Cognito-authenticated user information.

        Cognito users authenticate via Cognito User Pool and receive temporary
        AWS credentials from Cognito Identity Pool. Their tenant info is encoded
        in the session name or available via session tags.

        Args:
            caller_identity: AWS caller identity
            db: Database session

        Returns:
            TenantInfo: Cognito user tenant information

        Raises:
            TenantResolutionError: If user resolution fails
            UnknownOrganizationError: If organization not found
        """
        try:
            logger.debug(f"Resolving Cognito user: {caller_identity.arn}")

            # Extract tenant info from session
            cognito_info = self.extract_cognito_tenant_info(caller_identity)

            # If we have org_id from session, use it directly
            if cognito_info.org_id:
                org_result = await db.execute(select(Organization).where(Organization.id == cognito_info.org_id))
                organization = org_result.scalar_one_or_none()
                if not organization:
                    raise UnknownOrganizationError(f"Organization {cognito_info.org_id} not found")
            else:
                # Fall back to AWS account lookup
                organization = await self._find_organization_by_account(caller_identity.account, db)
                if not organization:
                    raise UnknownOrganizationError(caller_identity.account)

            # Try to find user by cognito_sub (user_id from STS)
            user = None
            if caller_identity.user_id:
                result = await db.execute(select(User).where(User.cognito_sub == caller_identity.user_id, User.org_id == organization.id))
                user = result.scalar_one_or_none()

            # If user found in database, use their team/department
            if user:
                team = await self._get_team(user.team_id, db)
                department_id = team.department_id if team else cognito_info.department_id or "default"
            else:
                department_id = cognito_info.department_id or "default"

            # Determine admin status from role
            is_admin = cognito_info.role == "admin" if cognito_info.role else False

            return TenantInfo(
                org_id=organization.id,
                org_name=organization.name,
                department_id=department_id,
                team_id=cognito_info.team_id or user.team_id if user else "default",
                account_type="cognito",
                entity_id=user.id if user else caller_identity.user_id or "unknown",
                is_admin=is_admin,
            )

        except UnknownOrganizationError:
            raise
        except Exception as e:
            logger.error(f"Failed to resolve Cognito user: {e}")
            raise TenantResolutionError(f"Cognito user resolution failed: {str(e)}")

    async def _find_organization_by_account(self, aws_account_id: str, db: AsyncSession) -> Organization | None:
        """
        Find organization by AWS account ID.

        Args:
            aws_account_id: AWS account ID
            db: Database session

        Returns:
            Optional[Organization]: Organization if found, None otherwise

        Raises:
            TenantResolutionError: If database operation fails
        """
        try:
            # Query all organizations and filter in Python for database-agnostic JSON handling
            # This avoids PostgreSQL-specific JSON operators that don't work with SQLite
            result = await db.execute(select(Organization))
            organizations = result.scalars().all()

            for org in organizations:
                # Check if aws_account_id is in the organization's aws_accounts list
                aws_accounts = org.aws_accounts or []
                if aws_account_id in aws_accounts:
                    return org

            return None

        except Exception as e:
            logger.error(f"Failed to find organization for account {aws_account_id}: {e}")
            raise TenantResolutionError(f"Database error while looking up organization: {str(e)}")

    async def _resolve_service_account(self, caller_identity: AWSCallerIdentity, organization: Organization, db: AsyncSession) -> TenantInfo:
        """
        Resolve service account information.

        Args:
            caller_identity: AWS caller identity
            organization: Organization information
            db: Database session

        Returns:
            TenantInfo: Service account tenant information

        Raises:
            UnregisteredServiceAccountError: If service account is not registered
        """
        try:
            # Extract role ARN from the caller identity
            role_arn = self._extract_role_arn(caller_identity.arn)
            if not role_arn:
                logger.error(f"Could not extract role ARN from: {caller_identity.arn}")
                raise UnregisteredServiceAccountError(caller_identity.arn)

            # Find the service account by role ARN
            result = await db.execute(select(ServiceAccount).where(ServiceAccount.iam_role_arn == role_arn, ServiceAccount.org_id == organization.id))
            service_account = result.scalar_one_or_none()

            if not service_account:
                logger.warning(f"Unregistered service account role: {role_arn}")
                raise UnregisteredServiceAccountError(role_arn)

            # Verify department and team exist (validation)
            await self._get_department(service_account.department_id, db)
            await self._get_team(service_account.team_id, db)

            logger.debug(f"Resolved service account: {service_account.name}")

            return TenantInfo(
                org_id=organization.id,
                org_name=organization.name,
                department_id=service_account.department_id,
                team_id=service_account.team_id,
                account_type="service",
                entity_id=service_account.id,
                is_admin=self._check_admin_privileges(organization, role_arn),
            )

        except UnregisteredServiceAccountError:
            raise
        except Exception as e:
            logger.error(f"Failed to resolve service account: {e}")
            raise TenantResolutionError(f"Service account resolution failed: {str(e)}")

    async def _resolve_human_user(self, caller_identity: AWSCallerIdentity, organization: Organization, db: AsyncSession) -> TenantInfo:
        """
        Resolve human user information via AWS Identity Center.

        Args:
            caller_identity: AWS caller identity
            organization: Organization information
            db: Database session

        Returns:
            TenantInfo: Human user tenant information

        Raises:
            TenantResolutionError: If user resolution fails
        """
        try:
            # Extract user information from the ARN
            user_identifier = self._extract_user_identifier(caller_identity.arn)
            if not user_identifier:
                logger.error(f"Could not extract user identifier from: {caller_identity.arn}")
                raise TenantResolutionError("Unable to extract user information from ARN")

            # Try to find user by Identity Center user ID or email
            user = await self._find_user_by_identifier(user_identifier, organization.id, db)

            if not user:
                # Create a temporary user entry for unknown users
                # This supports the case where users exist in AWS SSO but not yet in our database
                user = await self._create_temporary_user(user_identifier, caller_identity, organization.id, db)

            # Get team and then department information
            team = await self._get_team(user.team_id, db)
            if not team:
                raise TenantResolutionError("User's team not found")

            department = await self._get_department(team.department_id, db)
            if not department:
                raise TenantResolutionError("User's team has no associated department")

            logger.debug(f"Resolved human user: {user.email}")

            return TenantInfo(
                org_id=organization.id,
                org_name=organization.name,
                department_id=department.id,
                team_id=user.team_id,
                account_type="human",
                entity_id=user.id,
                is_admin=self._check_user_admin_privileges(organization, caller_identity.arn),
            )

        except Exception as e:
            logger.error(f"Failed to resolve human user: {e}")
            raise TenantResolutionError(f"Human user resolution failed: {str(e)}")

    async def _get_department(self, department_id: str, db: AsyncSession) -> Department | None:
        """Get department by ID."""
        result = await db.execute(select(Department).where(Department.id == department_id))
        return result.scalar_one_or_none()

    async def _get_team(self, team_id: str, db: AsyncSession) -> Team | None:
        """Get team by ID."""
        result = await db.execute(select(Team).where(Team.id == team_id))
        return result.scalar_one_or_none()

    async def _find_user_by_identifier(self, identifier: str, org_id: str, db: AsyncSession) -> User | None:
        """Find user by email or Identity Center user ID."""
        # Try by email first
        result = await db.execute(select(User).where(User.email == identifier, User.org_id == org_id))
        user = result.scalar_one_or_none()

        if user:
            return user

        # Try by Identity Center user ID
        result = await db.execute(select(User).where(User.identity_center_user_id == identifier, User.org_id == org_id))
        return result.scalar_one_or_none()

    async def _create_temporary_user(self, identifier: str, caller_identity: AWSCallerIdentity, org_id: str, db: AsyncSession) -> User:
        """
        Create a temporary user entry for users not yet in the database.

        This supports just-in-time user provisioning for AWS SSO users.
        """
        # For now, assign to a default team/department
        # In a production system, this would integrate with Identity Center groups
        default_team = await self._get_or_create_default_team(org_id, db)

        user = User(
            org_id=org_id,
            team_id=default_team.id,
            email=identifier if "@" in identifier else f"{identifier}@unknown.local",
            identity_center_user_id=caller_identity.user_id,
        )

        db.add(user)
        await db.commit()
        await db.refresh(user)

        logger.info(f"Created temporary user entry for {identifier}")
        return user

    async def _get_or_create_default_team(self, org_id: str, db: AsyncSession) -> Team:
        """Get or create a default team for unknown users."""
        # This is a simplified implementation
        # In production, you'd want more sophisticated team assignment logic
        result = await db.execute(select(Team).where(Team.name == "Default", Team.org_id == org_id).limit(1))
        team = result.scalar_one_or_none()

        if team:
            return team

        # Create default department and team if they don't exist
        # This is a fallback for organizations without proper setup
        raise TenantResolutionError("No default team configured for organization")

    def _is_service_account(self, arn: str) -> bool:
        """
        Determine if an ARN represents a service account (role) or human user.

        Args:
            arn: AWS resource ARN

        Returns:
            bool: True if this is a service account (role)
        """
        # Human users coming through AWS SSO have ARNs like:
        # arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Developer/john.doe@test.com
        # These should NOT be treated as service accounts
        if ":assumed-role/AWSReservedSSO_" in arn:
            return False

        # IAM users are human users
        if ":user/" in arn:
            return False

        # Service accounts use roles, which appear as assumed roles in STS
        return ":assumed-role/" in arn or ":role/" in arn

    def _extract_role_arn(self, assumed_role_arn: str) -> str | None:
        """
        Extract the original IAM role ARN from an assumed role ARN.

        Args:
            assumed_role_arn: Assumed role ARN from STS

        Returns:
            Optional[str]: Original role ARN or None if extraction fails
        """
        try:
            # Format: arn:aws:sts::account:assumed-role/RoleName/SessionName
            if ":assumed-role/" in assumed_role_arn:
                parts = assumed_role_arn.split("/")
                if len(parts) >= 2:
                    role_name = parts[-2]
                    account_id = assumed_role_arn.split(":")[4]
                    return f"arn:aws:iam::{account_id}:role/{role_name}"

            # Format: arn:aws:iam::account:role/RoleName
            elif ":role/" in assumed_role_arn:
                return assumed_role_arn

            return None

        except Exception:
            return None

    def _extract_user_identifier(self, user_arn: str) -> str | None:
        """
        Extract user identifier (email or username) from user ARN.

        Args:
            user_arn: User ARN from AWS

        Returns:
            Optional[str]: User identifier or None if extraction fails
        """
        try:
            # Format: arn:aws:iam::account:user/username
            # Format: arn:aws:sts::account:assumed-role/AWSReservedSSO_*/username
            if ":user/" in user_arn:
                return user_arn.split("/")[-1]
            elif ":assumed-role/AWSReservedSSO_" in user_arn:
                return user_arn.split("/")[-1]

            return None

        except Exception:
            return None

    def _check_admin_privileges(self, organization: Organization, role_arn: str) -> bool:
        """
        Check if a service account role has admin privileges.

        Args:
            organization: Organization information
            role_arn: IAM role ARN

        Returns:
            bool: True if the role has admin privileges
        """
        # Check role mappings in organization settings
        role_mappings = organization.role_mappings or {}
        admin_roles = role_mappings.get("admin_roles", [])

        role_name = self._extract_role_name(role_arn)
        return role_name in admin_roles if role_name else False

    def _check_user_admin_privileges(self, organization: Organization, user_arn: str) -> bool:
        """
        Check if a human user has admin privileges.

        Args:
            organization: Organization information
            user_arn: User ARN

        Returns:
            bool: True if the user has admin privileges
        """
        # Check for admin groups in the ARN or role mappings
        role_mappings = organization.role_mappings or {}
        admin_groups = role_mappings.get("admin_groups", [])

        # Check if user is in an admin SSO group
        return any(group in user_arn for group in admin_groups)

    def _extract_role_name(self, role_arn: str) -> str | None:
        """Extract role name from role ARN."""
        try:
            # Valid role ARNs contain "/" to separate role name
            # e.g., arn:aws:iam::123456789012:role/TestRole
            if "/" not in role_arn:
                return None
            return role_arn.split("/")[-1]
        except Exception:
            return None
