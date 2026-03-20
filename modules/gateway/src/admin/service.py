"""Admin service for organization CRUD, pool management, and configuration."""

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.cognito_service import CognitoService, CognitoServiceError
from src.admin.config import get_admin_config
from src.admin.exceptions import PoolConfigurationError, ResourceConflictError, ResourceNotFoundError
from src.admin.schemas import (
    BudgetConfigResponse,
    BudgetConfigUpdateRequest,
    BudgetCreateRequest,
    BudgetListItem,
    BudgetListResponse,
    BudgetStatusResponse,
    OrganizationCreateRequest,
    OrganizationResponse,
    OrganizationUpdateRequest,
    PoolAccountCreateRequest,
    PoolAccountResponse,
    PoolStatusResponse,
    RateLimitConfigResponse,
    RateLimitConfigUpdateRequest,
    RateLimitCreateRequest,
    RateLimitListItem,
    RateLimitListResponse,
)
from src.shared.interfaces.budget import IBudgetService
from src.shared.interfaces.ratelimit import IRateLimitService
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.organization import Department, Organization, ServiceAccount, Team, User
from src.shared.models.usage import BedrockPoolAccount, RateLimitConfig
from src.shared.schemas.admin import (
    DepartmentCreateRequest,
    DepartmentResponse,
    DepartmentUpdateRequest,
    ServiceAccountCreateRequest,
    ServiceAccountResponse,
    TeamCreateRequest,
    TeamResponse,
    TeamUpdateRequest,
    UserCreateRequest,
    UserResponse,
)


class AdminService:
    """
    Admin service for managing organizations, pool accounts, and configurations.

    This service provides:
    - Organization CRUD operations
    - Bedrock pool account management
    - Budget and rate limit configuration
    """

    def __init__(
        self,
        db: AsyncSession,
        budget_service: IBudgetService | None = None,
        ratelimit_service: IRateLimitService | None = None,
    ):
        """
        Initialize admin service.

        Args:
            db: Database session
            budget_service: Optional budget service for budget config operations
            ratelimit_service: Optional rate limit service
        """
        self.db = db
        self.budget_service = budget_service
        self.ratelimit_service = ratelimit_service
        self.config = get_admin_config()

    # Organization CRUD Operations

    async def create_organization(self, request: OrganizationCreateRequest) -> OrganizationResponse:
        """
        Create a new organization.

        Args:
            request: Organization creation request

        Returns:
            Created organization data

        Raises:
            ResourceConflictError: If organization name already exists
        """
        # Check for existing organization with same name
        existing = await self.db.execute(select(Organization).where(Organization.name == request.name))
        if existing.scalar_one_or_none():
            raise ResourceConflictError("Organization", "name", request.name)

        org = Organization(
            name=request.name,
            aws_accounts=request.aws_accounts,
            role_mappings=request.role_mappings,
            settings=request.settings,
        )

        self.db.add(org)
        await self.db.commit()
        await self.db.refresh(org)

        return OrganizationResponse(
            id=org.id,
            name=org.name,
            aws_accounts=org.aws_accounts or [],
            role_mappings=org.role_mappings or {},
            settings=org.settings or {},
            created_at=org.created_at,
        )

    async def get_organization(self, org_id: str) -> OrganizationResponse:
        """
        Get an organization by ID.

        Args:
            org_id: Organization ID

        Returns:
            Organization data

        Raises:
            ResourceNotFoundError: If organization not found
        """
        result = await self.db.execute(select(Organization).where(Organization.id == org_id))
        org = result.scalar_one_or_none()

        if not org:
            raise ResourceNotFoundError("Organization", org_id)

        return OrganizationResponse(
            id=org.id,
            name=org.name,
            aws_accounts=org.aws_accounts or [],
            role_mappings=org.role_mappings or {},
            settings=org.settings or {},
            created_at=org.created_at,
        )

    async def list_organizations(
        self,
        page: int = 1,
        page_size: int | None = None,
        org_ids: list[str] | None = None,
    ) -> tuple[list[OrganizationResponse], int]:
        """
        List organizations with pagination.

        Args:
            page: Page number (1-indexed)
            page_size: Items per page
            org_ids: Optional list of org IDs to filter by

        Returns:
            Tuple of (list of organizations, total count)
        """
        if page_size is None:
            page_size = self.config.default_page_size

        page_size = min(page_size, self.config.max_page_size)
        offset = (page - 1) * page_size

        # Build query
        query = select(Organization)
        count_query = select(func.count()).select_from(Organization)

        if org_ids is not None:
            query = query.where(Organization.id.in_(org_ids))
            count_query = count_query.where(Organization.id.in_(org_ids))

        # Get total count
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # Get paginated results
        query = query.offset(offset).limit(page_size).order_by(Organization.name)
        result = await self.db.execute(query)
        orgs = result.scalars().all()

        return (
            [
                OrganizationResponse(
                    id=org.id,
                    name=org.name,
                    aws_accounts=org.aws_accounts or [],
                    role_mappings=org.role_mappings or {},
                    settings=org.settings or {},
                    created_at=org.created_at,
                )
                for org in orgs
            ],
            total,
        )

    async def update_organization(self, org_id: str, request: OrganizationUpdateRequest) -> OrganizationResponse:
        """
        Update an organization.

        Args:
            org_id: Organization ID
            request: Update request

        Returns:
            Updated organization data

        Raises:
            ResourceNotFoundError: If organization not found
            ResourceConflictError: If new name already exists
        """
        result = await self.db.execute(select(Organization).where(Organization.id == org_id))
        org = result.scalar_one_or_none()

        if not org:
            raise ResourceNotFoundError("Organization", org_id)

        # Check name uniqueness if updating name
        if request.name and request.name != org.name:
            existing = await self.db.execute(select(Organization).where(Organization.name == request.name))
            if existing.scalar_one_or_none():
                raise ResourceConflictError("Organization", "name", request.name)
            org.name = request.name

        if request.aws_accounts is not None:
            org.aws_accounts = request.aws_accounts

        if request.role_mappings is not None:
            org.role_mappings = request.role_mappings

        if request.settings is not None:
            org.settings = request.settings

        await self.db.commit()
        await self.db.refresh(org)

        return OrganizationResponse(
            id=org.id,
            name=org.name,
            aws_accounts=org.aws_accounts or [],
            role_mappings=org.role_mappings or {},
            settings=org.settings or {},
            created_at=org.created_at,
        )

    async def delete_organization(self, org_id: str) -> bool:
        """
        Delete an organization.

        Args:
            org_id: Organization ID

        Returns:
            True if deleted

        Raises:
            ResourceNotFoundError: If organization not found
        """
        result = await self.db.execute(select(Organization).where(Organization.id == org_id))
        org = result.scalar_one_or_none()

        if not org:
            raise ResourceNotFoundError("Organization", org_id)

        await self.db.delete(org)
        await self.db.commit()
        return True

    # Pool Management

    async def get_pool_status(self) -> PoolStatusResponse:
        """
        Get the status of all Bedrock pool accounts.

        Returns:
            Pool status including healthy/unhealthy counts and account details
        """
        result = await self.db.execute(select(BedrockPoolAccount))
        accounts = result.scalars().all()

        account_responses = [
            PoolAccountResponse(
                id=acc.id,
                account_id=acc.account_id,
                role_arn=acc.role_arn,
                region=acc.region,
                is_healthy=acc.is_healthy,
                last_health_check=acc.last_health_check,
                created_at=acc.created_at,
            )
            for acc in accounts
        ]

        healthy_count = sum(1 for acc in accounts if acc.is_healthy)
        unhealthy_count = len(accounts) - healthy_count

        return PoolStatusResponse(
            total_accounts=len(accounts),
            healthy_accounts=healthy_count,
            unhealthy_accounts=unhealthy_count,
            accounts=account_responses,
        )

    async def add_pool_account(self, request: PoolAccountCreateRequest) -> PoolAccountResponse:
        """
        Add a new account to the Bedrock pool.

        Args:
            request: Pool account creation request

        Returns:
            Created pool account data

        Raises:
            PoolConfigurationError: If account or role ARN already exists
        """
        # Check for existing account with same role ARN
        existing = await self.db.execute(select(BedrockPoolAccount).where(BedrockPoolAccount.role_arn == request.role_arn))
        if existing.scalar_one_or_none():
            raise PoolConfigurationError(f"Pool account with role ARN '{request.role_arn}' already exists")

        account = BedrockPoolAccount(
            account_id=request.account_id,
            role_arn=request.role_arn,
            region=request.region,
            is_healthy=True,
        )

        self.db.add(account)
        await self.db.commit()
        await self.db.refresh(account)

        return PoolAccountResponse(
            id=account.id,
            account_id=account.account_id,
            role_arn=account.role_arn,
            region=account.region,
            is_healthy=account.is_healthy,
            last_health_check=account.last_health_check,
            created_at=account.created_at,
        )

    async def remove_pool_account(self, account_id: str) -> bool:
        """
        Remove an account from the Bedrock pool.

        Args:
            account_id: Pool account ID (internal ID, not AWS account ID)

        Returns:
            True if removed

        Raises:
            ResourceNotFoundError: If account not found
        """
        result = await self.db.execute(select(BedrockPoolAccount).where(BedrockPoolAccount.id == account_id))
        account = result.scalar_one_or_none()

        if not account:
            raise ResourceNotFoundError("PoolAccount", account_id)

        await self.db.delete(account)
        await self.db.commit()
        return True

    # Budget Configuration

    async def get_budget_config(self, org_id: str, entity_type: str, entity_id: str) -> BudgetConfigResponse | None:
        """
        Get budget configuration for an entity.

        Args:
            org_id: Organization ID
            entity_type: Entity type (org, department, team, user)
            entity_id: Entity ID

        Returns:
            Budget configuration or None if not found
        """
        if self.budget_service:
            from src.shared.schemas.budget import EntityType

            try:
                entity = EntityType(entity_type)
            except ValueError:
                return None

            budgets = await self.budget_service.get_budgets_for_entity(entity, entity_id, org_id)
            if budgets:
                budget = budgets[0]  # Get first budget
                return BudgetConfigResponse(
                    org_id=budget.org_id,
                    entity_type=budget.entity_type.value,
                    entity_id=budget.entity_id,
                    period_type=budget.period_type.value,
                    budget_amount_usd=budget.budget_amount_usd,
                    enforcement_mode=budget.enforcement_mode.value,
                    updated_at=budget.updated_at,
                )
        return None

    async def get_budget_status(
        self,
        org_id: str,
        entity_type: str,
        entity_id: str,
    ) -> BudgetStatusResponse:
        """
        Get budget status with current spend for an entity.

        Args:
            org_id: Organization ID
            entity_type: Entity type (org, department, team, user)
            entity_id: Entity ID

        Returns:
            Budget status with current spend information

        Raises:
            ResourceNotFoundError: If no budget config found
        """
        from datetime import date
        from decimal import Decimal

        # Get budget config
        result = await self.db.execute(
            select(BudgetConfig).where(
                BudgetConfig.org_id == org_id,
                BudgetConfig.entity_type == entity_type,
                BudgetConfig.entity_id == entity_id,
            )
        )
        config = result.scalar_one_or_none()
        if not config:
            raise ResourceNotFoundError("BudgetConfig", f"{entity_type}/{entity_id}")

        # Calculate period boundaries
        today = date.today()
        if config.period_type == "daily":
            period_start = today
            period_end = today + timedelta(days=1)
        elif config.period_type == "weekly":
            period_start = today - timedelta(days=today.weekday())
            period_end = period_start + timedelta(days=7)
        else:  # monthly
            period_start = today.replace(day=1)
            next_month = today.replace(day=28) + timedelta(days=4)
            period_end = next_month.replace(day=1)

        # Get current usage
        usage_result = await self.db.execute(
            select(BudgetUsage).where(
                BudgetUsage.org_id == org_id,
                BudgetUsage.entity_type == entity_type,
                BudgetUsage.entity_id == entity_id,
                BudgetUsage.period_start == period_start,
                BudgetUsage.period_type == config.period_type,
            )
        )
        usage = usage_result.scalar_one_or_none()
        current_spend = usage.total_cost_usd if usage else Decimal("0.00")
        remaining = config.budget_amount_usd - current_spend
        utilization = float(current_spend / config.budget_amount_usd * 100) if config.budget_amount_usd > 0 else 0.0
        exceeded = current_spend >= config.budget_amount_usd

        warnings: list[str] = []
        if utilization >= 90:
            warnings.append("Budget utilization is above 90%")
        elif utilization >= 75:
            warnings.append("Budget utilization is above 75%")

        return BudgetStatusResponse(
            budget_amount_usd=config.budget_amount_usd,
            current_spend_usd=current_spend,
            remaining_budget_usd=max(remaining, Decimal("0.00")),
            budget_utilization_percent=round(utilization, 1),
            period_start=str(period_start),
            period_end=str(period_end),
            period_type=config.period_type,
            enforcement_mode=config.enforcement_mode,
            budget_exceeded=exceeded,
            warnings=warnings,
        )

    async def update_budget_config(
        self,
        org_id: str,
        entity_type: str,
        entity_id: str,
        request: BudgetConfigUpdateRequest,
    ) -> BudgetConfigResponse | None:
        """
        Update budget configuration for an entity.

        Args:
            org_id: Organization ID
            entity_type: Entity type
            entity_id: Entity ID
            request: Update request

        Returns:
            Updated budget configuration
        """
        if self.budget_service:
            from src.shared.schemas.budget import BudgetUpdateRequest, EntityType

            try:
                entity = EntityType(entity_type)
            except ValueError:
                return None

            # Get existing budget
            budgets = await self.budget_service.get_budgets_for_entity(entity, entity_id, org_id)
            if budgets:
                budget_id = budgets[0].id
                update_request = BudgetUpdateRequest(
                    budget_amount_usd=request.budget_amount_usd,
                    enforcement_mode=request.enforcement_mode,
                )
                updated = await self.budget_service.update_budget(budget_id, update_request, org_id)
                if updated:
                    return BudgetConfigResponse(
                        org_id=updated.org_id,
                        entity_type=updated.entity_type.value,
                        entity_id=updated.entity_id,
                        period_type=updated.period_type.value,
                        budget_amount_usd=updated.budget_amount_usd,
                        enforcement_mode=updated.enforcement_mode.value,
                        updated_at=updated.updated_at,
                    )
        return None

    # Rate Limit Configuration

    async def get_ratelimit_config(self, org_id: str, entity_type: str, entity_id: str) -> RateLimitConfigResponse | None:
        """
        Get rate limit configuration for an entity.

        Args:
            org_id: Organization ID
            entity_type: Entity type
            entity_id: Entity ID

        Returns:
            Rate limit configuration or None if not found
        """
        result = await self.db.execute(
            select(RateLimitConfig).where(
                RateLimitConfig.org_id == org_id,
                RateLimitConfig.entity_type == entity_type,
                RateLimitConfig.entity_id == entity_id,
            )
        )
        config = result.scalar_one_or_none()

        if config:
            return RateLimitConfigResponse(
                org_id=config.org_id,
                entity_type=config.entity_type,
                entity_id=config.entity_id,
                rpm=config.rpm,
                tpm=config.tpm,
                concurrent_requests=config.concurrent_requests,
                updated_at=config.updated_at,
            )
        return None

    async def update_ratelimit_config(
        self,
        org_id: str,
        entity_type: str,
        entity_id: str,
        request: RateLimitConfigUpdateRequest,
    ) -> RateLimitConfigResponse:
        """
        Update rate limit configuration for an entity.

        Args:
            org_id: Organization ID
            entity_type: Entity type
            entity_id: Entity ID
            request: Update request

        Returns:
            Updated rate limit configuration
        """
        result = await self.db.execute(
            select(RateLimitConfig).where(
                RateLimitConfig.org_id == org_id,
                RateLimitConfig.entity_type == entity_type,
                RateLimitConfig.entity_id == entity_id,
            )
        )
        config = result.scalar_one_or_none()

        if config:
            if request.rpm is not None:
                config.rpm = request.rpm
            if request.tpm is not None:
                config.tpm = request.tpm
            if request.concurrent_requests is not None:
                config.concurrent_requests = request.concurrent_requests
        else:
            config = RateLimitConfig(
                org_id=org_id,
                entity_type=entity_type,
                entity_id=entity_id,
                rpm=request.rpm,
                tpm=request.tpm,
                concurrent_requests=request.concurrent_requests,
            )
            self.db.add(config)

        await self.db.commit()
        await self.db.refresh(config)

        return RateLimitConfigResponse(
            org_id=config.org_id,
            entity_type=config.entity_type,
            entity_id=config.entity_id,
            rpm=config.rpm,
            tpm=config.tpm,
            concurrent_requests=config.concurrent_requests,
            updated_at=config.updated_at,
        )

    # Department CRUD Operations

    async def create_department(
        self,
        org_id: str,
        request: DepartmentCreateRequest,
        cognito_service: CognitoService | None = None,
    ) -> DepartmentResponse:
        """
        Create a new department within an organization.

        Args:
            org_id: Organization ID
            request: Department creation request
            cognito_service: Optional Cognito service for group creation

        Returns:
            Created department data

        Raises:
            ResourceNotFoundError: If organization not found
            ResourceConflictError: If department name already exists in org
        """
        # Verify organization exists
        org_result = await self.db.execute(select(Organization).where(Organization.id == org_id))
        if not org_result.scalar_one_or_none():
            raise ResourceNotFoundError("Organization", org_id)

        # Check for existing department with same name in org
        existing = await self.db.execute(select(Department).where(Department.org_id == org_id, Department.name == request.name))
        if existing.scalar_one_or_none():
            raise ResourceConflictError("Department", "name", request.name)

        dept = Department(
            org_id=org_id,
            name=request.name,
            description=request.description,
            budget_limit=request.budget_limit,
        )

        self.db.add(dept)
        await self.db.commit()
        await self.db.refresh(dept)

        # Create Cognito group if service provided
        if cognito_service:
            try:
                group_name = f"dept-{dept.id}"
                cognito_service.create_org_group(dept.id)
                dept.cognito_group_name = group_name
                await self.db.commit()
                await self.db.refresh(dept)
            except CognitoServiceError:
                pass  # Non-critical, continue without Cognito group

        return DepartmentResponse(
            id=dept.id,
            org_id=dept.org_id,
            name=dept.name,
            budget_limit=dept.budget_limit,
            description=dept.description,
            cognito_group_name=dept.cognito_group_name,
            created_at=dept.created_at,
            updated_at=dept.updated_at,
        )

    async def get_department(self, org_id: str, dept_id: str) -> DepartmentResponse:
        """
        Get a department by ID.

        Args:
            org_id: Organization ID
            dept_id: Department ID

        Returns:
            Department data

        Raises:
            ResourceNotFoundError: If department not found
        """
        result = await self.db.execute(select(Department).where(Department.id == dept_id, Department.org_id == org_id))
        dept = result.scalar_one_or_none()

        if not dept:
            raise ResourceNotFoundError("Department", dept_id)

        return DepartmentResponse(
            id=dept.id,
            org_id=dept.org_id,
            name=dept.name,
            budget_limit=dept.budget_limit,
            description=dept.description,
            cognito_group_name=dept.cognito_group_name,
            created_at=dept.created_at,
            updated_at=dept.updated_at,
        )

    async def list_departments(
        self,
        org_id: str,
        page: int = 1,
        page_size: int | None = None,
    ) -> tuple[list[DepartmentResponse], int]:
        """
        List departments in an organization with pagination.

        Args:
            org_id: Organization ID
            page: Page number (1-indexed)
            page_size: Items per page

        Returns:
            Tuple of (list of departments, total count)
        """
        if page_size is None:
            page_size = self.config.default_page_size

        page_size = min(page_size, self.config.max_page_size)
        offset = (page - 1) * page_size

        # Get total count
        count_query = select(func.count()).select_from(Department).where(Department.org_id == org_id)
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # Get paginated results
        query = select(Department).where(Department.org_id == org_id).offset(offset).limit(page_size).order_by(Department.name)
        result = await self.db.execute(query)
        depts = result.scalars().all()

        return (
            [
                DepartmentResponse(
                    id=dept.id,
                    org_id=dept.org_id,
                    name=dept.name,
                    budget_limit=dept.budget_limit,
                    description=dept.description,
                    cognito_group_name=dept.cognito_group_name,
                    created_at=dept.created_at,
                    updated_at=dept.updated_at,
                )
                for dept in depts
            ],
            total,
        )

    async def update_department(self, org_id: str, dept_id: str, request: DepartmentUpdateRequest) -> DepartmentResponse:
        """
        Update a department.

        Args:
            org_id: Organization ID
            dept_id: Department ID
            request: Update request

        Returns:
            Updated department data

        Raises:
            ResourceNotFoundError: If department not found
            ResourceConflictError: If new name already exists
        """
        result = await self.db.execute(select(Department).where(Department.id == dept_id, Department.org_id == org_id))
        dept = result.scalar_one_or_none()

        if not dept:
            raise ResourceNotFoundError("Department", dept_id)

        # Check name uniqueness if updating name
        if request.name and request.name != dept.name:
            existing = await self.db.execute(select(Department).where(Department.org_id == org_id, Department.name == request.name))
            if existing.scalar_one_or_none():
                raise ResourceConflictError("Department", "name", request.name)
            dept.name = request.name

        if request.description is not None:
            dept.description = request.description

        if request.budget_limit is not None:
            dept.budget_limit = request.budget_limit

        await self.db.commit()
        await self.db.refresh(dept)

        return DepartmentResponse(
            id=dept.id,
            org_id=dept.org_id,
            name=dept.name,
            budget_limit=dept.budget_limit,
            description=dept.description,
            cognito_group_name=dept.cognito_group_name,
            created_at=dept.created_at,
            updated_at=dept.updated_at,
        )

    async def delete_department(
        self,
        org_id: str,
        dept_id: str,
        cognito_service: CognitoService | None = None,
    ) -> bool:
        """
        Delete a department.

        Args:
            org_id: Organization ID
            dept_id: Department ID
            cognito_service: Optional Cognito service for group deletion

        Returns:
            True if deleted

        Raises:
            ResourceNotFoundError: If department not found
        """
        result = await self.db.execute(select(Department).where(Department.id == dept_id, Department.org_id == org_id))
        dept = result.scalar_one_or_none()

        if not dept:
            raise ResourceNotFoundError("Department", dept_id)

        # Delete Cognito group if service provided
        if cognito_service and dept.cognito_group_name:
            try:
                cognito_service.delete_org_group(dept_id)
            except CognitoServiceError:
                pass  # Non-critical

        await self.db.delete(dept)
        await self.db.commit()
        return True

    # Team CRUD Operations

    async def create_team(self, org_id: str, dept_id: str, request: TeamCreateRequest) -> TeamResponse:
        """
        Create a new team within a department.

        Args:
            org_id: Organization ID
            dept_id: Department ID
            request: Team creation request

        Returns:
            Created team data

        Raises:
            ResourceNotFoundError: If department not found
            ResourceConflictError: If team name already exists in department
        """
        # Verify department exists and belongs to org
        dept_result = await self.db.execute(select(Department).where(Department.id == dept_id, Department.org_id == org_id))
        if not dept_result.scalar_one_or_none():
            raise ResourceNotFoundError("Department", dept_id)

        # Check for existing team with same name in department
        existing = await self.db.execute(select(Team).where(Team.department_id == dept_id, Team.name == request.name))
        if existing.scalar_one_or_none():
            raise ResourceConflictError("Team", "name", request.name)

        team = Team(
            org_id=org_id,
            department_id=dept_id,
            name=request.name,
            description=request.description,
        )

        self.db.add(team)
        await self.db.commit()
        await self.db.refresh(team)

        return TeamResponse(
            id=team.id,
            org_id=team.org_id,
            department_id=team.department_id,
            name=team.name,
            description=team.description,
            created_at=team.created_at,
            updated_at=team.updated_at,
        )

    async def get_team(self, org_id: str, team_id: str) -> TeamResponse:
        """
        Get a team by ID.

        Args:
            org_id: Organization ID
            team_id: Team ID

        Returns:
            Team data

        Raises:
            ResourceNotFoundError: If team not found
        """
        result = await self.db.execute(select(Team).where(Team.id == team_id, Team.org_id == org_id))
        team = result.scalar_one_or_none()

        if not team:
            raise ResourceNotFoundError("Team", team_id)

        return TeamResponse(
            id=team.id,
            org_id=team.org_id,
            department_id=team.department_id,
            name=team.name,
            description=team.description,
            created_at=team.created_at,
            updated_at=team.updated_at,
        )

    async def list_teams(
        self,
        org_id: str,
        dept_id: str,
        page: int = 1,
        page_size: int | None = None,
    ) -> tuple[list[TeamResponse], int]:
        """
        List teams in a department with pagination.

        Args:
            org_id: Organization ID
            dept_id: Department ID
            page: Page number (1-indexed)
            page_size: Items per page

        Returns:
            Tuple of (list of teams, total count)
        """
        if page_size is None:
            page_size = self.config.default_page_size

        page_size = min(page_size, self.config.max_page_size)
        offset = (page - 1) * page_size

        # Get total count
        count_query = select(func.count()).select_from(Team).where(Team.org_id == org_id, Team.department_id == dept_id)
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # Get paginated results
        query = select(Team).where(Team.org_id == org_id, Team.department_id == dept_id).offset(offset).limit(page_size).order_by(Team.name)
        result = await self.db.execute(query)
        teams = result.scalars().all()

        return (
            [
                TeamResponse(
                    id=team.id,
                    org_id=team.org_id,
                    department_id=team.department_id,
                    name=team.name,
                    description=team.description,
                    created_at=team.created_at,
                    updated_at=team.updated_at,
                )
                for team in teams
            ],
            total,
        )

    async def update_team(self, org_id: str, team_id: str, request: TeamUpdateRequest) -> TeamResponse:
        """
        Update a team.

        Args:
            org_id: Organization ID
            team_id: Team ID
            request: Update request

        Returns:
            Updated team data

        Raises:
            ResourceNotFoundError: If team not found
            ResourceConflictError: If new name already exists
        """
        result = await self.db.execute(select(Team).where(Team.id == team_id, Team.org_id == org_id))
        team = result.scalar_one_or_none()

        if not team:
            raise ResourceNotFoundError("Team", team_id)

        # Check name uniqueness if updating name
        if request.name and request.name != team.name:
            existing = await self.db.execute(select(Team).where(Team.department_id == team.department_id, Team.name == request.name))
            if existing.scalar_one_or_none():
                raise ResourceConflictError("Team", "name", request.name)
            team.name = request.name

        if request.description is not None:
            team.description = request.description

        await self.db.commit()
        await self.db.refresh(team)

        return TeamResponse(
            id=team.id,
            org_id=team.org_id,
            department_id=team.department_id,
            name=team.name,
            description=team.description,
            created_at=team.created_at,
            updated_at=team.updated_at,
        )

    async def delete_team(self, org_id: str, team_id: str) -> bool:
        """
        Delete a team.

        Args:
            org_id: Organization ID
            team_id: Team ID

        Returns:
            True if deleted

        Raises:
            ResourceNotFoundError: If team not found
        """
        result = await self.db.execute(select(Team).where(Team.id == team_id, Team.org_id == org_id))
        team = result.scalar_one_or_none()

        if not team:
            raise ResourceNotFoundError("Team", team_id)

        await self.db.delete(team)
        await self.db.commit()
        return True

    # User Management Operations

    async def add_user(
        self,
        org_id: str,
        team_id: str,
        request: UserCreateRequest,
        cognito_service: CognitoService | None = None,
    ) -> UserResponse:
        """
        Add a new user to a team.

        Creates user in Cognito (if service provided) and database.

        Args:
            org_id: Organization ID
            team_id: Team ID
            request: User creation request
            cognito_service: Optional Cognito service for user creation

        Returns:
            Created user data

        Raises:
            ResourceNotFoundError: If team not found
            ResourceConflictError: If user email already exists
        """
        # Verify team exists and belongs to org
        team_result = await self.db.execute(select(Team).where(Team.id == team_id, Team.org_id == org_id))
        team = team_result.scalar_one_or_none()
        if not team:
            raise ResourceNotFoundError("Team", team_id)

        # Check for existing user with same email in org
        existing = await self.db.execute(select(User).where(User.org_id == org_id, User.email == request.email))
        if existing.scalar_one_or_none():
            raise ResourceConflictError("User", "email", request.email)

        cognito_sub = None
        cognito_username = None

        # Create user in Cognito if service provided
        if cognito_service:
            try:
                cognito_user = cognito_service.create_user(
                    email=request.email,
                    org_id=org_id,
                    dept_id=team.department_id,
                    team_id=team_id,
                    name=request.name,
                    role=request.role,
                )
                cognito_sub = cognito_user.get("Username")
                cognito_username = request.email

                # Add user to org group
                cognito_service.add_user_to_group(request.email, f"org-{org_id}")
            except CognitoServiceError:
                pass  # Continue without Cognito, will create local user

        user = User(
            org_id=org_id,
            team_id=team_id,
            email=request.email,
            name=request.name,
            role=request.role,
            cognito_sub=cognito_sub,
            cognito_username=cognito_username,
        )

        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)

        return UserResponse(
            id=user.id,
            org_id=user.org_id,
            team_id=user.team_id,
            email=user.email,
            name=user.name,
            cognito_sub=user.cognito_sub,
            cognito_username=user.cognito_username,
            role=user.role,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )

    async def get_user(self, org_id: str, user_id: str) -> UserResponse:
        """
        Get a user by ID.

        Args:
            org_id: Organization ID
            user_id: User ID

        Returns:
            User data

        Raises:
            ResourceNotFoundError: If user not found
        """
        result = await self.db.execute(select(User).where(User.id == user_id, User.org_id == org_id))
        user = result.scalar_one_or_none()

        if not user:
            raise ResourceNotFoundError("User", user_id)

        return UserResponse(
            id=user.id,
            org_id=user.org_id,
            team_id=user.team_id,
            email=user.email,
            name=user.name,
            cognito_sub=user.cognito_sub,
            cognito_username=user.cognito_username,
            role=user.role,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )

    async def list_users_org(
        self,
        org_id: str,
        page: int = 1,
        page_size: int | None = None,
    ) -> tuple[list[UserResponse], int]:
        """
        List all users in an organization with pagination.

        Args:
            org_id: Organization ID
            page: Page number (1-indexed)
            page_size: Items per page

        Returns:
            Tuple of (list of users, total count)
        """
        if page_size is None:
            page_size = self.config.default_page_size

        page_size = min(page_size, self.config.max_page_size)
        offset = (page - 1) * page_size

        # Get total count
        count_query = select(func.count()).select_from(User).where(User.org_id == org_id)
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # Get paginated results
        query = select(User).where(User.org_id == org_id).offset(offset).limit(page_size).order_by(User.email)
        result = await self.db.execute(query)
        users = result.scalars().all()

        return (
            [
                UserResponse(
                    id=user.id,
                    org_id=user.org_id,
                    team_id=user.team_id,
                    email=user.email,
                    name=user.name,
                    cognito_sub=user.cognito_sub,
                    cognito_username=user.cognito_username,
                    role=user.role,
                    created_at=user.created_at,
                    updated_at=user.updated_at,
                )
                for user in users
            ],
            total,
        )

    async def list_users_team(
        self,
        org_id: str,
        team_id: str,
        page: int = 1,
        page_size: int | None = None,
    ) -> tuple[list[UserResponse], int]:
        """
        List users in a team with pagination.

        Args:
            org_id: Organization ID
            team_id: Team ID
            page: Page number (1-indexed)
            page_size: Items per page

        Returns:
            Tuple of (list of users, total count)
        """
        if page_size is None:
            page_size = self.config.default_page_size

        page_size = min(page_size, self.config.max_page_size)
        offset = (page - 1) * page_size

        # Get total count
        count_query = select(func.count()).select_from(User).where(User.org_id == org_id, User.team_id == team_id)
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # Get paginated results
        query = select(User).where(User.org_id == org_id, User.team_id == team_id).offset(offset).limit(page_size).order_by(User.email)
        result = await self.db.execute(query)
        users = result.scalars().all()

        return (
            [
                UserResponse(
                    id=user.id,
                    org_id=user.org_id,
                    team_id=user.team_id,
                    email=user.email,
                    name=user.name,
                    cognito_sub=user.cognito_sub,
                    cognito_username=user.cognito_username,
                    role=user.role,
                    created_at=user.created_at,
                    updated_at=user.updated_at,
                )
                for user in users
            ],
            total,
        )

    async def remove_user(
        self,
        org_id: str,
        user_id: str,
        cognito_service: CognitoService | None = None,
    ) -> bool:
        """
        Remove a user.

        Deletes user from Cognito (if service provided) and database.

        Args:
            org_id: Organization ID
            user_id: User ID
            cognito_service: Optional Cognito service for user deletion

        Returns:
            True if deleted

        Raises:
            ResourceNotFoundError: If user not found
        """
        result = await self.db.execute(select(User).where(User.id == user_id, User.org_id == org_id))
        user = result.scalar_one_or_none()

        if not user:
            raise ResourceNotFoundError("User", user_id)

        # Delete from Cognito if service provided
        if cognito_service and user.cognito_username:
            try:
                cognito_service.delete_user(username=user.cognito_username)
            except CognitoServiceError:
                pass  # Non-critical

        await self.db.delete(user)
        await self.db.commit()
        return True

    # Service Account Management

    async def create_service_account(
        self,
        org_id: str,
        dept_id: str,
        team_id: str,
        request: ServiceAccountCreateRequest,
    ) -> ServiceAccountResponse:
        """
        Create a new service account.

        Args:
            org_id: Organization ID
            dept_id: Department ID
            team_id: Team ID
            request: Service account creation request

        Returns:
            Created service account data

        Raises:
            ResourceNotFoundError: If team not found
            ResourceConflictError: If IAM role ARN already exists
        """
        # Verify team exists and belongs to org
        team_result = await self.db.execute(select(Team).where(Team.id == team_id, Team.org_id == org_id))
        if not team_result.scalar_one_or_none():
            raise ResourceNotFoundError("Team", team_id)

        # Check for existing service account with same role ARN
        if request.iam_role_arn:
            existing = await self.db.execute(select(ServiceAccount).where(ServiceAccount.iam_role_arn == request.iam_role_arn))
            if existing.scalar_one_or_none():
                raise ResourceConflictError("ServiceAccount", "iam_role_arn", request.iam_role_arn)

        sa = ServiceAccount(
            org_id=org_id,
            department_id=dept_id,
            team_id=team_id,
            name=request.name,
            description=request.description,
            iam_role_arn=request.iam_role_arn or f"arn:aws:iam::000000000000:role/{request.name}",
        )

        self.db.add(sa)
        await self.db.commit()
        await self.db.refresh(sa)

        return ServiceAccountResponse(
            id=sa.id,
            org_id=sa.org_id,
            department_id=sa.department_id,
            team_id=sa.team_id,
            name=sa.name,
            description=sa.description,
            iam_role_arn=sa.iam_role_arn,
            created_at=sa.created_at,
        )

    async def list_service_accounts(
        self,
        org_id: str,
        page: int = 1,
        page_size: int | None = None,
    ) -> tuple[list[ServiceAccountResponse], int]:
        """
        List service accounts in an organization with pagination.

        Args:
            org_id: Organization ID
            page: Page number (1-indexed)
            page_size: Items per page

        Returns:
            Tuple of (list of service accounts, total count)
        """
        if page_size is None:
            page_size = self.config.default_page_size

        page_size = min(page_size, self.config.max_page_size)
        offset = (page - 1) * page_size

        # Get total count
        count_query = select(func.count()).select_from(ServiceAccount).where(ServiceAccount.org_id == org_id)
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # Get paginated results
        query = select(ServiceAccount).where(ServiceAccount.org_id == org_id).offset(offset).limit(page_size).order_by(ServiceAccount.name)
        result = await self.db.execute(query)
        sas = result.scalars().all()

        return (
            [
                ServiceAccountResponse(
                    id=sa.id,
                    org_id=sa.org_id,
                    department_id=sa.department_id,
                    team_id=sa.team_id,
                    name=sa.name,
                    description=sa.description,
                    iam_role_arn=sa.iam_role_arn,
                    created_at=sa.created_at,
                )
                for sa in sas
            ],
            total,
        )

    async def delete_service_account(self, org_id: str, sa_id: str) -> bool:
        """
        Delete a service account.

        Args:
            org_id: Organization ID
            sa_id: Service account ID

        Returns:
            True if deleted

        Raises:
            ResourceNotFoundError: If service account not found
        """
        result = await self.db.execute(select(ServiceAccount).where(ServiceAccount.id == sa_id, ServiceAccount.org_id == org_id))
        sa = result.scalar_one_or_none()

        if not sa:
            raise ResourceNotFoundError("ServiceAccount", sa_id)

        await self.db.delete(sa)
        await self.db.commit()
        return True

    # =============================================================================
    # Budget List/Create/Delete Operations (Issue #185)
    # =============================================================================

    async def get_budgets_list(
        self,
        org_id: str,
        entity_type: str | None = None,
        page: int = 1,
        page_size: int | None = None,
        cognito_service: CognitoService | None = None,
    ) -> BudgetListResponse:
        """
        Get list of all budget configs for an organization with current usage.

        Args:
            org_id: Organization ID
            entity_type: Optional filter by entity type
            page: Page number (1-indexed)
            page_size: Items per page
            cognito_service: Optional CognitoService for resolving user display names

        Returns:
            Paginated budget list with usage information
        """
        from datetime import date
        from decimal import Decimal

        if page_size is None:
            page_size = self.config.default_page_size

        page_size = min(page_size, self.config.max_page_size)
        offset = (page - 1) * page_size

        # Build base query
        query = select(BudgetConfig).where(BudgetConfig.org_id == org_id)
        count_query = select(func.count()).select_from(BudgetConfig).where(BudgetConfig.org_id == org_id)

        if entity_type:
            query = query.where(BudgetConfig.entity_type == entity_type)
            count_query = count_query.where(BudgetConfig.entity_type == entity_type)

        # Get total count
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # Get paginated budget configs
        query = query.offset(offset).limit(page_size).order_by(BudgetConfig.entity_type, BudgetConfig.entity_id)
        result = await self.db.execute(query)
        budget_configs = result.scalars().all()

        # Resolve user display names from Cognito (batch lookup)
        user_display_names: dict[str, str] = {}
        user_entity_ids = [c.entity_id for c in budget_configs if c.entity_type == "user"]
        if user_entity_ids and cognito_service:
            try:
                cognito_users, _ = cognito_service.list_users_by_org(org_id)
                for cu in cognito_users:
                    sub = cognito_service._get_user_attribute(cu, "sub")
                    username = cu.get("Username", "")
                    github = cognito_service._get_user_attribute(cu, "custom:github_username")
                    email = cognito_service._get_user_attribute(cu, "email")
                    name = cognito_service._get_user_attribute(cu, "name")
                    # Build display name: prefer github, then email, then name
                    display = github or email or name or username
                    if sub and sub in user_entity_ids:
                        user_display_names[sub] = display
                    if username in user_entity_ids:
                        user_display_names[username] = display
            except Exception:
                pass  # Gracefully degrade — show raw IDs

        # Get current usage for each budget
        items: list[BudgetListItem] = []
        today = date.today()

        for config in budget_configs:
            # Calculate period start based on period_type
            if config.period_type == "daily":
                period_start = today
            elif config.period_type == "weekly":
                # Start of the current week (Monday)
                period_start = today - timedelta(days=today.weekday())
            else:  # monthly
                period_start = today.replace(day=1)

            # Query current usage
            usage_result = await self.db.execute(
                select(BudgetUsage).where(
                    BudgetUsage.org_id == org_id,
                    BudgetUsage.entity_type == config.entity_type,
                    BudgetUsage.entity_id == config.entity_id,
                    BudgetUsage.period_start == period_start,
                    BudgetUsage.period_type == config.period_type,
                )
            )
            usage = usage_result.scalar_one_or_none()

            current_usage_usd = usage.total_cost_usd if usage else Decimal("0.00")
            utilization_pct = float(current_usage_usd / config.budget_amount_usd * 100) if config.budget_amount_usd > 0 else 0.0

            # Resolve display name for user entities
            display_name = None
            if config.entity_type == "user":
                display_name = user_display_names.get(config.entity_id)

            items.append(
                BudgetListItem(
                    entity_type=config.entity_type,
                    entity_id=config.entity_id,
                    entity_display_name=display_name,
                    period_type=config.period_type,
                    budget_amount_usd=config.budget_amount_usd,
                    enforcement_mode=config.enforcement_mode,
                    current_usage_usd=current_usage_usd,
                    utilization_pct=round(utilization_pct, 1),
                    updated_at=config.updated_at,
                )
            )

        return BudgetListResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            has_more=(page * page_size) < total,
        )

    async def create_budget(self, org_id: str, request: BudgetCreateRequest) -> BudgetConfigResponse:
        """
        Create a new budget configuration.

        Args:
            org_id: Organization ID
            request: Budget creation request

        Returns:
            Created budget configuration

        Raises:
            ResourceConflictError: If budget already exists for this entity/period
        """
        # Check for existing budget with same entity and period
        existing = await self.db.execute(
            select(BudgetConfig).where(
                BudgetConfig.org_id == org_id,
                BudgetConfig.entity_type == request.entity_type,
                BudgetConfig.entity_id == request.entity_id,
                BudgetConfig.period_type == request.period_type,
            )
        )
        if existing.scalar_one_or_none():
            raise ResourceConflictError(
                "BudgetConfig",
                "entity_type/entity_id/period_type",
                f"{request.entity_type}/{request.entity_id}/{request.period_type}",
            )

        budget = BudgetConfig(
            org_id=org_id,
            entity_type=request.entity_type,
            entity_id=request.entity_id,
            period_type=request.period_type,
            budget_amount_usd=request.budget_amount_usd,
            enforcement_mode=request.enforcement_mode,
        )

        self.db.add(budget)
        await self.db.commit()
        await self.db.refresh(budget)

        return BudgetConfigResponse(
            org_id=budget.org_id,
            entity_type=budget.entity_type,
            entity_id=budget.entity_id,
            period_type=budget.period_type,
            budget_amount_usd=budget.budget_amount_usd,
            enforcement_mode=budget.enforcement_mode,
            updated_at=budget.updated_at,
        )

    async def delete_budget(self, org_id: str, entity_type: str, entity_id: str, period_type: str) -> bool:
        """
        Delete a budget configuration.

        Args:
            org_id: Organization ID
            entity_type: Entity type
            entity_id: Entity ID
            period_type: Period type

        Returns:
            True if deleted

        Raises:
            ResourceNotFoundError: If budget not found
        """
        result = await self.db.execute(
            select(BudgetConfig).where(
                BudgetConfig.org_id == org_id,
                BudgetConfig.entity_type == entity_type,
                BudgetConfig.entity_id == entity_id,
                BudgetConfig.period_type == period_type,
            )
        )
        budget = result.scalar_one_or_none()

        if not budget:
            raise ResourceNotFoundError("BudgetConfig", f"{entity_type}/{entity_id}/{period_type}")

        await self.db.delete(budget)
        await self.db.commit()
        return True

    # =============================================================================
    # Rate Limit List/Create/Delete Operations (Issue #185)
    # =============================================================================

    async def get_ratelimits_list(
        self,
        org_id: str,
        entity_type: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> RateLimitListResponse:
        """
        Get list of all rate limit configs for an organization.

        Args:
            org_id: Organization ID
            entity_type: Optional filter by entity type
            page: Page number (1-indexed)
            page_size: Items per page

        Returns:
            Paginated rate limit list
        """
        if page_size is None:
            page_size = self.config.default_page_size

        page_size = min(page_size, self.config.max_page_size)
        offset = (page - 1) * page_size

        # Build query
        query = select(RateLimitConfig).where(RateLimitConfig.org_id == org_id)
        count_query = select(func.count()).select_from(RateLimitConfig).where(RateLimitConfig.org_id == org_id)

        if entity_type:
            query = query.where(RateLimitConfig.entity_type == entity_type)
            count_query = count_query.where(RateLimitConfig.entity_type == entity_type)

        # Get total count
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # Get paginated results
        query = query.offset(offset).limit(page_size).order_by(RateLimitConfig.entity_type, RateLimitConfig.entity_id)
        result = await self.db.execute(query)
        configs = result.scalars().all()

        items = [
            RateLimitListItem(
                entity_type=config.entity_type,
                entity_id=config.entity_id,
                rpm=config.rpm,
                tpm=config.tpm,
                concurrent_requests=config.concurrent_requests,
                updated_at=config.updated_at,
            )
            for config in configs
        ]

        return RateLimitListResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            has_more=(page * page_size) < total,
        )

    async def create_ratelimit(self, org_id: str, request: RateLimitCreateRequest) -> RateLimitConfigResponse:
        """
        Create a new rate limit configuration.

        Args:
            org_id: Organization ID
            request: Rate limit creation request

        Returns:
            Created rate limit configuration

        Raises:
            ResourceConflictError: If rate limit already exists for this entity
        """
        # Check for existing rate limit with same entity
        existing = await self.db.execute(
            select(RateLimitConfig).where(
                RateLimitConfig.org_id == org_id,
                RateLimitConfig.entity_type == request.entity_type,
                RateLimitConfig.entity_id == request.entity_id,
            )
        )
        if existing.scalar_one_or_none():
            raise ResourceConflictError(
                "RateLimitConfig",
                "entity_type/entity_id",
                f"{request.entity_type}/{request.entity_id}",
            )

        config = RateLimitConfig(
            org_id=org_id,
            entity_type=request.entity_type,
            entity_id=request.entity_id,
            rpm=request.rpm,
            tpm=request.tpm,
            concurrent_requests=request.concurrent_requests,
        )

        self.db.add(config)
        await self.db.commit()
        await self.db.refresh(config)

        return RateLimitConfigResponse(
            org_id=config.org_id,
            entity_type=config.entity_type,
            entity_id=config.entity_id,
            rpm=config.rpm,
            tpm=config.tpm,
            concurrent_requests=config.concurrent_requests,
            updated_at=config.updated_at,
        )

    async def delete_ratelimit(self, org_id: str, entity_type: str, entity_id: str) -> bool:
        """
        Delete a rate limit configuration.

        Args:
            org_id: Organization ID
            entity_type: Entity type
            entity_id: Entity ID

        Returns:
            True if deleted

        Raises:
            ResourceNotFoundError: If rate limit not found
        """
        result = await self.db.execute(
            select(RateLimitConfig).where(
                RateLimitConfig.org_id == org_id,
                RateLimitConfig.entity_type == entity_type,
                RateLimitConfig.entity_id == entity_id,
            )
        )
        config = result.scalar_one_or_none()

        if not config:
            raise ResourceNotFoundError("RateLimitConfig", f"{entity_type}/{entity_id}")

        await self.db.delete(config)
        await self.db.commit()
        return True

    # =============================================================================
    # Usage Timeseries (Issue #179)
    # =============================================================================

    async def get_usage_timeseries(
        self,
        org_id: str,
        period: str = "daily",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        """
        Get usage data aggregated over time for charts.

        Issue #179: Returns time-series data from usage_logs table.

        Args:
            org_id: Organization ID
            period: Aggregation period (daily, weekly, monthly)
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format

        Returns:
            List of data points with date, tokens, cost, and request count
        """
        from datetime import date, timedelta
        from decimal import Decimal

        from sqlalchemy import func

        from src.shared.models.usage import UsageLog

        # Default to last 30 days if no dates provided
        if not end_date:
            end_dt = date.today()
        else:
            end_dt = date.fromisoformat(end_date)

        if not start_date:
            start_dt = end_dt - timedelta(days=30)
        else:
            start_dt = date.fromisoformat(start_date)

        # Query usage_logs and aggregate by date
        # Use func.date() which works with both PostgreSQL and SQLite
        query = (
            select(
                func.date(UsageLog.timestamp).label("date"),
                func.sum(UsageLog.input_tokens).label("input_tokens"),
                func.sum(UsageLog.output_tokens).label("output_tokens"),
                func.sum(UsageLog.cost_usd).label("cost_usd"),
                func.count(UsageLog.id).label("request_count"),
            )
            .where(
                UsageLog.org_id == org_id,
                func.date(UsageLog.timestamp) >= start_dt,
                func.date(UsageLog.timestamp) <= end_dt,
            )
            .group_by(func.date(UsageLog.timestamp))
            .order_by(func.date(UsageLog.timestamp))
        )

        result = await self.db.execute(query)
        rows = result.all()

        # Build result with all dates in range (filling in zeros for missing dates)
        data_by_date = {str(row.date): row for row in rows}

        data_points = []
        current_dt = start_dt
        while current_dt <= end_dt:
            date_str = str(current_dt)
            if date_str in data_by_date:
                row = data_by_date[date_str]
                data_points.append(
                    {
                        "date": date_str,
                        "input_tokens": row.input_tokens or 0,
                        "output_tokens": row.output_tokens or 0,
                        "cost_usd": row.cost_usd or Decimal("0.00"),
                        "request_count": row.request_count or 0,
                    }
                )
            else:
                data_points.append(
                    {
                        "date": date_str,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_usd": Decimal("0.00"),
                        "request_count": 0,
                    }
                )
            current_dt += timedelta(days=1)

        return data_points

    # =============================================================================
    # My Chats (Issue #179)
    # =============================================================================

    async def get_user_chats(
        self,
        user_id: str,
        org_id: str,
        page: int = 1,
        limit: int = 20,
        model_filter: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> tuple[list[dict], int]:
        """
        Get chat history for a specific user.

        Issue #179: Returns usage_logs entries for the user, which represent
        individual chat requests. If chat logging feature (issue #143) is
        available, full conversation content can be retrieved separately.

        Args:
            user_id: User ID (from JWT)
            org_id: Organization ID (from JWT)
            page: Page number (1-indexed)
            limit: Items per page
            model_filter: Optional filter by model name
            start_date: Optional start date filter (YYYY-MM-DD)
            end_date: Optional end date filter (YYYY-MM-DD)

        Returns:
            Tuple of (list of chat summaries, total count)
        """
        from datetime import date as date_type

        from sqlalchemy import cast, func
        from sqlalchemy.types import Date

        from src.shared.models.usage import UsageLog

        offset = (page - 1) * limit

        # Build base query
        base_query = select(UsageLog).where(
            UsageLog.user_id == user_id,
            UsageLog.org_id == org_id,
        )

        # Apply filters
        if model_filter:
            base_query = base_query.where(UsageLog.model.ilike(f"%{model_filter}%"))

        if start_date:
            start_dt = date_type.fromisoformat(start_date)
            base_query = base_query.where(cast(UsageLog.timestamp, Date) >= start_dt)

        if end_date:
            end_dt = date_type.fromisoformat(end_date)
            base_query = base_query.where(cast(UsageLog.timestamp, Date) <= end_dt)

        # Get total count
        count_query = select(func.count()).select_from(base_query.subquery())
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # Get paginated results, ordered by newest first
        query = base_query.order_by(UsageLog.timestamp.desc()).offset(offset).limit(limit)
        result = await self.db.execute(query)
        logs = result.scalars().all()

        chats = []
        for log in logs:
            chats.append(
                {
                    "request_id": log.request_id or log.id,
                    "timestamp": log.timestamp,
                    "model": log.model,
                    "input_tokens": log.input_tokens,
                    "output_tokens": log.output_tokens,
                    "cost_usd": log.cost_usd,
                    "first_message_preview": None,  # Would come from chat logging feature
                    "stop_reason": None,  # Would come from chat logging feature
                }
            )

        return chats, total

    async def get_chat_detail(
        self,
        user_id: str,
        org_id: str,
        request_id: str,
    ) -> dict | None:
        """
        Get full details of a specific chat/request.

        Issue #179: Returns the usage log entry plus any available chat content
        from the chat logging feature (issue #143).

        Args:
            user_id: User ID (from JWT)
            org_id: Organization ID (from JWT)
            request_id: Request ID to retrieve

        Returns:
            Chat detail dict or None if not found
        """
        from sqlalchemy import or_

        from src.shared.models.usage import UsageLog

        # Find the usage log entry (could match either request_id or id)
        query = select(UsageLog).where(
            UsageLog.user_id == user_id,
            UsageLog.org_id == org_id,
            or_(UsageLog.request_id == request_id, UsageLog.id == request_id),
        )

        result = await self.db.execute(query)
        log = result.scalar_one_or_none()

        if not log:
            return None

        # Build the response
        chat_detail = {
            "request_id": log.request_id or log.id,
            "timestamp": log.timestamp,
            "model": log.model,
            "input_tokens": log.input_tokens,
            "output_tokens": log.output_tokens,
            "cost_usd": log.cost_usd,
            "latency_ms": log.latency_ms,
            "status_code": log.status_code,
            "stop_reason": None,
            "request_messages": None,
            "response_content": None,
            "chat_logging_available": False,
        }

        # TODO: When chat logging feature (issue #143) is merged,
        # retrieve full conversation from S3 here
        # try:
        #     from src.chat_logging.service import ChatLoggingService
        #     chat_service = ChatLoggingService()
        #     full_chat = await chat_service.get_chat(request_id)
        #     if full_chat:
        #         chat_detail["request_messages"] = full_chat.get("messages")
        #         chat_detail["response_content"] = full_chat.get("response")
        #         chat_detail["stop_reason"] = full_chat.get("stop_reason")
        #         chat_detail["chat_logging_available"] = True
        # except ImportError:
        #     pass  # Chat logging not available

        return chat_detail
