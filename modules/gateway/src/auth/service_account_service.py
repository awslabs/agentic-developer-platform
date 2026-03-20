"""
Service Account Service for CRUD operations on service accounts.

This module provides comprehensive service account management including:
- Creating, reading, updating, and deleting service accounts
- Input validation and business logic
- Database integration with proper error handling
- Support for pagination and filtering
"""

import logging

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.organization import Department, ServiceAccount, Team

from .exceptions import DuplicateServiceAccountError, ServiceAccountNotFoundError, TenantResolutionError
from .schemas import ServiceAccountCreate, ServiceAccountListResponse, ServiceAccountResponse, ServiceAccountUpdate

logger = logging.getLogger(__name__)


class ServiceAccountService:
    """
    Service for managing service account CRUD operations.

    Features:
    - Full CRUD operations with validation
    - IAM role ARN uniqueness enforcement
    - Organization/department/team relationship validation
    - Pagination and filtering support
    - Comprehensive error handling
    """

    def __init__(self):
        """Initialize the service account service."""
        pass

    async def create_service_account(self, create_data: ServiceAccountCreate, org_id: str, db: AsyncSession) -> ServiceAccountResponse:
        """
        Create a new service account.

        Args:
            create_data: Service account creation data
            org_id: Organization ID
            db: Database session

        Returns:
            ServiceAccountResponse: Created service account information

        Raises:
            DuplicateServiceAccountError: If IAM role ARN already exists
            TenantResolutionError: If department/team validation fails
        """
        try:
            logger.debug(f"Creating service account: {create_data.name}")

            # Validate that department and team exist and belong to the organization
            await self._validate_department_and_team(create_data.department_id, create_data.team_id, org_id, db)

            # Check for duplicate IAM role ARN
            existing = await self._find_by_role_arn(create_data.iam_role_arn, db)
            if existing:
                raise DuplicateServiceAccountError(create_data.iam_role_arn)

            # Create the service account
            service_account = ServiceAccount(
                org_id=org_id,
                name=create_data.name,
                department_id=create_data.department_id,
                team_id=create_data.team_id,
                iam_role_arn=create_data.iam_role_arn,
            )

            db.add(service_account)
            await db.commit()
            await db.refresh(service_account)

            logger.info(f"Created service account: {service_account.id}")

            return ServiceAccountResponse(
                id=service_account.id,
                org_id=service_account.org_id,
                name=service_account.name,
                department_id=service_account.department_id,
                team_id=service_account.team_id,
                iam_role_arn=service_account.iam_role_arn,
                created_at=service_account.created_at,
            )

        except DuplicateServiceAccountError:
            # No rollback needed - no changes were made to the database
            raise
        except TenantResolutionError:
            await db.rollback()
            raise
        except IntegrityError as e:
            await db.rollback()
            logger.error(f"Database integrity error creating service account: {e}")
            if "iam_role_arn" in str(e):
                raise DuplicateServiceAccountError(create_data.iam_role_arn)
            raise TenantResolutionError("Invalid department or team reference")
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to create service account: {e}")
            raise TenantResolutionError(f"Service account creation failed: {str(e)}")

    async def get_service_account(self, service_account_id: str, org_id: str, db: AsyncSession) -> ServiceAccountResponse:
        """
        Get a service account by ID.

        Args:
            service_account_id: Service account ID
            org_id: Organization ID
            db: Database session

        Returns:
            ServiceAccountResponse: Service account information

        Raises:
            ServiceAccountNotFoundError: If service account not found
        """
        try:
            result = await db.execute(select(ServiceAccount).where(ServiceAccount.id == service_account_id, ServiceAccount.org_id == org_id))
            service_account = result.scalar_one_or_none()

            if not service_account:
                raise ServiceAccountNotFoundError(service_account_id)

            return ServiceAccountResponse(
                id=service_account.id,
                org_id=service_account.org_id,
                name=service_account.name,
                department_id=service_account.department_id,
                team_id=service_account.team_id,
                iam_role_arn=service_account.iam_role_arn,
                created_at=service_account.created_at,
            )

        except ServiceAccountNotFoundError:
            raise
        except Exception as e:
            logger.error(f"Failed to get service account {service_account_id}: {e}")
            raise TenantResolutionError(f"Service account retrieval failed: {str(e)}")

    async def update_service_account(
        self, service_account_id: str, update_data: ServiceAccountUpdate, org_id: str, db: AsyncSession
    ) -> ServiceAccountResponse:
        """
        Update a service account.

        Args:
            service_account_id: Service account ID
            update_data: Update data
            org_id: Organization ID
            db: Database session

        Returns:
            ServiceAccountResponse: Updated service account information

        Raises:
            ServiceAccountNotFoundError: If service account not found
            DuplicateServiceAccountError: If IAM role ARN conflicts
            TenantResolutionError: If validation fails
        """
        try:
            # Check that service account exists
            existing = await self.get_service_account(service_account_id, org_id, db)

            # Build update data
            update_dict = {}
            if update_data.name is not None:
                update_dict["name"] = update_data.name
            if update_data.department_id is not None:
                update_dict["department_id"] = update_data.department_id
            if update_data.team_id is not None:
                update_dict["team_id"] = update_data.team_id
            if update_data.iam_role_arn is not None:
                update_dict["iam_role_arn"] = update_data.iam_role_arn

            if not update_dict:
                # No changes, return existing
                return existing

            # Validate department/team if they're being updated
            department_id = update_data.department_id or existing.department_id
            team_id = update_data.team_id or existing.team_id
            await self._validate_department_and_team(department_id, team_id, org_id, db)

            # Check for IAM role ARN conflicts if it's being updated
            if update_data.iam_role_arn and update_data.iam_role_arn != existing.iam_role_arn:
                conflict = await self._find_by_role_arn(update_data.iam_role_arn, db)
                if conflict:
                    raise DuplicateServiceAccountError(update_data.iam_role_arn)

            # Perform the update
            await db.execute(
                update(ServiceAccount).where(ServiceAccount.id == service_account_id, ServiceAccount.org_id == org_id).values(**update_dict)
            )
            await db.commit()

            # Return updated service account
            return await self.get_service_account(service_account_id, org_id, db)

        except (ServiceAccountNotFoundError, DuplicateServiceAccountError, TenantResolutionError):
            await db.rollback()
            raise
        except IntegrityError as e:
            await db.rollback()
            logger.error(f"Database integrity error updating service account: {e}")
            if "iam_role_arn" in str(e):
                raise DuplicateServiceAccountError(update_data.iam_role_arn)
            raise TenantResolutionError("Invalid department or team reference")
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to update service account {service_account_id}: {e}")
            raise TenantResolutionError(f"Service account update failed: {str(e)}")

    async def delete_service_account(self, service_account_id: str, org_id: str, db: AsyncSession) -> bool:
        """
        Delete a service account.

        Args:
            service_account_id: Service account ID
            org_id: Organization ID
            db: Database session

        Returns:
            bool: True if service account was deleted

        Raises:
            ServiceAccountNotFoundError: If service account not found
        """
        try:
            # Verify service account exists
            await self.get_service_account(service_account_id, org_id, db)

            # Delete the service account
            result = await db.execute(delete(ServiceAccount).where(ServiceAccount.id == service_account_id, ServiceAccount.org_id == org_id))
            await db.commit()

            if result.rowcount > 0:
                logger.info(f"Deleted service account: {service_account_id}")
                return True
            else:
                raise ServiceAccountNotFoundError(service_account_id)

        except ServiceAccountNotFoundError:
            await db.rollback()
            raise
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to delete service account {service_account_id}: {e}")
            raise TenantResolutionError(f"Service account deletion failed: {str(e)}")

    async def list_service_accounts(
        self, org_id: str, db: AsyncSession, department_id: str | None = None, team_id: str | None = None, page: int = 1, page_size: int = 50
    ) -> ServiceAccountListResponse:
        """
        List service accounts with optional filtering and pagination.

        Args:
            org_id: Organization ID
            db: Database session
            department_id: Optional filter by department
            team_id: Optional filter by team
            page: Page number (1-based)
            page_size: Number of items per page

        Returns:
            ServiceAccountListResponse: Paginated list of service accounts
        """
        try:
            # Build query with filters
            query = select(ServiceAccount).where(ServiceAccount.org_id == org_id)

            if department_id:
                query = query.where(ServiceAccount.department_id == department_id)
            if team_id:
                query = query.where(ServiceAccount.team_id == team_id)

            # Get total count
            count_query = select(func.count(ServiceAccount.id)).where(ServiceAccount.org_id == org_id)
            if department_id:
                count_query = count_query.where(ServiceAccount.department_id == department_id)
            if team_id:
                count_query = count_query.where(ServiceAccount.team_id == team_id)

            total_count_result = await db.execute(count_query)
            total_count = total_count_result.scalar()

            # Apply pagination
            offset = (page - 1) * page_size
            query = query.offset(offset).limit(page_size)

            # Execute query
            result = await db.execute(query)
            service_accounts = result.scalars().all()

            # Convert to response objects
            service_account_responses = [
                ServiceAccountResponse(
                    id=sa.id,
                    org_id=sa.org_id,
                    name=sa.name,
                    department_id=sa.department_id,
                    team_id=sa.team_id,
                    iam_role_arn=sa.iam_role_arn,
                    created_at=sa.created_at,
                )
                for sa in service_accounts
            ]

            logger.debug(f"Listed {len(service_account_responses)} service accounts for org {org_id}")

            return ServiceAccountListResponse(service_accounts=service_account_responses, total_count=total_count, page=page, page_size=page_size)

        except Exception as e:
            logger.error(f"Failed to list service accounts: {e}")
            raise TenantResolutionError(f"Service account listing failed: {str(e)}")

    async def find_service_account_by_role_arn(self, iam_role_arn: str, db: AsyncSession) -> ServiceAccountResponse | None:
        """
        Find service account by IAM role ARN.

        Args:
            iam_role_arn: IAM role ARN
            db: Database session

        Returns:
            Optional[ServiceAccountResponse]: Service account if found, None otherwise
        """
        try:
            service_account = await self._find_by_role_arn(iam_role_arn, db)
            if not service_account:
                return None

            return ServiceAccountResponse(
                id=service_account.id,
                org_id=service_account.org_id,
                name=service_account.name,
                department_id=service_account.department_id,
                team_id=service_account.team_id,
                iam_role_arn=service_account.iam_role_arn,
                created_at=service_account.created_at,
            )

        except Exception as e:
            logger.error(f"Failed to find service account by role ARN {iam_role_arn}: {e}")
            return None

    async def _find_by_role_arn(self, iam_role_arn: str, db: AsyncSession) -> ServiceAccount | None:
        """Find service account by IAM role ARN."""
        result = await db.execute(select(ServiceAccount).where(ServiceAccount.iam_role_arn == iam_role_arn))
        return result.scalar_one_or_none()

    async def _validate_department_and_team(self, department_id: str, team_id: str, org_id: str, db: AsyncSession) -> None:
        """
        Validate that department and team exist and belong to the organization.

        Args:
            department_id: Department ID
            team_id: Team ID
            org_id: Organization ID
            db: Database session

        Raises:
            TenantResolutionError: If validation fails
        """
        # Check department exists and belongs to org
        dept_result = await db.execute(select(Department).where(Department.id == department_id, Department.org_id == org_id))
        department = dept_result.scalar_one_or_none()
        if not department:
            raise TenantResolutionError(f"Department {department_id} not found in organization {org_id}")

        # Check team exists and belongs to org and department
        team_result = await db.execute(select(Team).where(Team.id == team_id, Team.org_id == org_id, Team.department_id == department_id))
        team = team_result.scalar_one_or_none()
        if not team:
            raise TenantResolutionError(f"Team {team_id} not found in department {department_id} of organization {org_id}")
