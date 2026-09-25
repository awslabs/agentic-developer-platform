"""Admin service for organization CRUD, pool management, and configuration."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.cognito_claims import sync_cognito_role_claims
from src.admin.cognito_service import CognitoService, CognitoServiceError
from src.admin.config import get_admin_config
from src.admin.exceptions import MemberRemovalConflictError, PoolConfigurationError, ResourceConflictError, ResourceNotFoundError
from src.admin.installations.guards import assert_new_installation_ids_claimable_by, lock_installation_organization
from src.admin.memberships import (
    is_admin_level_role,
    project_member_org_ids,
    set_membership_role,
    upsert_tenant_membership,
)
from src.shared.identity.verification import PROVEN_METHODS

if TYPE_CHECKING:
    from src.admin.identity.identity_index_writer import IdentityIndexWriter
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
from src.shared.exceptions import ConflictError
from src.shared.identity import resolve_root_user_entity_id, resolve_user_entity_id
from src.shared.interfaces.budget import IBudgetService
from src.shared.interfaces.ratelimit import IRateLimitService
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import (
    CREATED_VIA_OPERATOR,
    Department,
    Organization,
    ServiceAccount,
    Team,
    TeamMembership,
    User,
)
from src.shared.models.usage import BedrockPoolAccount, RateLimitConfig
from src.shared.models.vault import UserCredential, UserIdentity
from src.shared.schemas.admin import (
    DepartmentCreateRequest,
    DepartmentResponse,
    DepartmentUpdateRequest,
    PlatformUserResponse,
    ServiceAccountCreateRequest,
    ServiceAccountResponse,
    TeamCreateRequest,
    TeamResponse,
    TeamUpdateRequest,
    UserCreateRequest,
    UserResponse,
    UserUpdateRequest,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserAuthzState:
    """A target user's identifiers plus the role that actually confers authority.

    Issue #4019: returned by :meth:`AdminService.get_user_authz_state` so a route
    can make an authz decision about a target user without reaching for
    ``users.role``, which is a display mirror nothing in authz reads.
    """

    user_id: str
    org_id: str
    cognito_sub: str | None
    users_role: str | None
    membership_role: str | None


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
        identity_index=None,
    ):
        """
        Initialize admin service.

        Args:
            db: Database session
            budget_service: Optional budget service for budget config operations
            ratelimit_service: Optional rate limit service
            identity_index: Optional IdentityIndexClient for write-through (Issue #375)
        """
        self.db = db
        self.budget_service = budget_service
        self.ratelimit_service = ratelimit_service
        self.identity_index = identity_index
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

        from src.shared.models.base import new_uuid

        new_org_id = new_uuid()
        await assert_new_installation_ids_claimable_by(new_org_id, new_ids=list(request.github_installation_ids or []), old_ids=[], db=self.db)
        org = Organization(
            id=new_org_id,
            name=request.name,
            aws_accounts=request.aws_accounts,
            role_mappings=request.role_mappings,
            settings=request.settings,
            github_installation_ids=request.github_installation_ids,
            cognito_client_ids=request.cognito_client_ids,
            # Issue #4842 (R6=a): stamped, not inherited. A platform admin
            # provisioned this tenant, which is exactly what CREATED_VIA_OPERATOR
            # means, so the row is trusted BY INTENT rather than because the
            # column default happens to be a trusted value.
            created_via=CREATED_VIA_OPERATOR,
        )

        self.db.add(org)
        await self.db.commit()
        await self.db.refresh(org)

        # Issue #375: Best-effort write-through to identity-index
        if self.identity_index:
            try:
                await self.identity_index.sync_identities_for_org(
                    org_id=org.id,
                    github_installation_ids=org.github_installation_ids or [],
                    cognito_client_ids=org.cognito_client_ids or [],
                )
            except Exception:
                logger.exception("identity-index write-through failed for org %s (create)", org.id)

        return OrganizationResponse(
            id=org.id,
            name=org.name,
            aws_accounts=org.aws_accounts or [],
            role_mappings=org.role_mappings or {},
            settings=org.settings or {},
            github_installation_ids=org.github_installation_ids or [],
            cognito_client_ids=org.cognito_client_ids or [],
            member_approval_policy=org.member_approval_policy,
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
            github_installation_ids=org.github_installation_ids or [],
            cognito_client_ids=org.cognito_client_ids or [],
            member_approval_policy=org.member_approval_policy,
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
                    github_installation_ids=org.github_installation_ids or [],
                    cognito_client_ids=org.cognito_client_ids or [],
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
        org = await lock_installation_organization(self.db, org_id)

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

        # Issue #375: Track old identity lists for diff-based sync
        old_github_ids = list(org.github_installation_ids or [])
        old_cognito_ids = list(org.cognito_client_ids or [])

        if request.github_installation_ids is not None:
            # Issue #4072 (#11, HIGH): github_installation_ids arrives verbatim
            # from the request body with no validator, and this assignment used
            # to trust it. The route's ORG_UPDATE + target_org_id check confines
            # the caller to their OWN org, but nothing confirmed the
            # *installation* belongs to that org — so an org-admin could name a
            # victim's installation and inherit the victim's webhook events,
            # agent runs and credential context. Checked BEFORE the assignment so
            # neither Postgres nor the DDB write-through below is reached for a
            # foreign claim. Raises InstallationClaimError (409/403).
            await assert_new_installation_ids_claimable_by(
                org_id,
                new_ids=list(request.github_installation_ids),
                old_ids=old_github_ids,
                db=self.db,
            )
            org.github_installation_ids = request.github_installation_ids

        if request.cognito_client_ids is not None:
            org.cognito_client_ids = request.cognito_client_ids

        # Issue #2984: Update member approval policy if provided
        if request.member_approval_policy is not None:
            org.member_approval_policy = request.member_approval_policy

        await self.db.commit()
        await self.db.refresh(org)

        # Issue #375: Best-effort write-through to identity-index (diff-based)
        if self.identity_index and (request.github_installation_ids is not None or request.cognito_client_ids is not None):
            try:
                await self.identity_index.sync_identities_for_org(
                    org_id=org.id,
                    github_installation_ids=org.github_installation_ids or [],
                    cognito_client_ids=org.cognito_client_ids or [],
                    old_github_installation_ids=old_github_ids,
                    old_cognito_client_ids=old_cognito_ids,
                )
            except Exception:
                logger.exception("identity-index write-through failed for org %s (update)", org.id)

        # Issue #3134: When settings change includes trigger_policy or
        # min_author_association, re-sync all installation rows with the new attrs.
        if request.settings is not None:
            settings = org.settings or {}
            trigger_policy = settings.get("trigger_policy")
            min_author_association = settings.get("min_author_association")
            if trigger_policy or min_author_association:
                try:
                    from src.admin.connections.service import _write_installation_identity_index

                    for iid in org.github_installation_ids or []:
                        await _write_installation_identity_index(
                            installation_id=int(iid),
                            org_id=org.id,
                            trigger_policy=trigger_policy,
                            min_author_association=min_author_association,
                        )
                except Exception:
                    logger.exception(
                        "identity-index: failed to re-sync trigger_policy for org %s installations",
                        org.id,
                    )

        return OrganizationResponse(
            id=org.id,
            name=org.name,
            aws_accounts=org.aws_accounts or [],
            role_mappings=org.role_mappings or {},
            settings=org.settings or {},
            github_installation_ids=org.github_installation_ids or [],
            cognito_client_ids=org.cognito_client_ids or [],
            member_approval_policy=org.member_approval_policy,
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

        # Issue #375: Capture identity lists before deletion for index cleanup
        github_ids = list(org.github_installation_ids or [])
        cognito_ids = list(org.cognito_client_ids or [])

        try:
            # ORM delete(org) nulls the non-null TenantMembership backref FK,
            # even when its collection is unloaded. Use the existing database
            # cascades; this deliberately does not delete users or their data.
            await self.db.execute(delete(Organization).where(Organization.id == org_id))
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            # Only a real PostgreSQL FK refusal is a dependency conflict. Do
            # not disguise an unrelated integrity defect as an operator error.
            if getattr(exc.orig, "sqlstate", None) != "23503":
                raise
            raise ConflictError(
                "This organization has related records that prevent permanent deletion. "
                "Use the organization archive operation to retain them. No changes were saved."
            ) from exc

        # Best-effort cleanup of identity-index entries
        if self.identity_index:
            try:
                await self.identity_index.delete_all_for_org(github_ids, cognito_ids)
            except Exception:
                logger.exception("identity-index cleanup failed for org %s (delete)", org_id)

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

    async def resolve_budget_target(self, org_id: str, entity_type: str, entity_id: str) -> tuple[str, str | None]:
        """Resolve only an existing entity in the selected tenant; return its department."""
        if entity_type in {"user", "root_user"}:
            resolved = await self._resolve_person_entity_id(org_id, entity_type, entity_id)
            # The canonicalizer validates the human's tenant ownership.
            user = (
                await self.db.execute(
                    select(User).where(
                        User.org_id == org_id,
                        or_(User.id == entity_id, User.cognito_sub == entity_id, User.id == resolved, User.cognito_sub == resolved),
                    )
                )
            ).scalar_one_or_none()
            if user is None:
                raise ResourceNotFoundError("User", entity_id)
            department = await self.db.scalar(select(Team.department_id).where(Team.id == user.team_id, Team.org_id == org_id))
            return resolved, department
        model = {"org": Organization, "department": Department, "team": Team}.get(entity_type)
        if model is None or (entity_type == "org" and entity_id != org_id):
            raise ResourceNotFoundError("BudgetTarget", entity_id)
        query = select(model).where(model.id == entity_id)
        if entity_type != "org":
            query = query.where(model.org_id == org_id)
        target = (await self.db.execute(query)).scalar_one_or_none()
        if target is None:
            raise ResourceNotFoundError("BudgetTarget", entity_id)
        department = entity_id if entity_type == "department" else getattr(target, "department_id", None)
        return entity_id, department

    async def exact_budget(self, org_id, entity_type, entity_id, period_type, *, lock=False):
        query = select(BudgetConfig).where(
            BudgetConfig.org_id == org_id,
            BudgetConfig.entity_type == entity_type,
            BudgetConfig.entity_id == entity_id,
            BudgetConfig.period_type == period_type,
        )
        if lock:
            query = query.with_for_update()
        return (await self.db.execute(query)).scalar_one_or_none()

    def budget_response(self, budget):
        updated_at = budget.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return BudgetConfigResponse(
            org_id=budget.org_id,
            entity_type=budget.entity_type,
            entity_id=budget.entity_id,
            period_type=budget.period_type,
            budget_amount_usd=budget.budget_amount_usd,
            enforcement_mode=budget.enforcement_mode,
            updated_at=updated_at,
        )

    async def delete_exact_budget(self, org_id, entity_type, entity_id, period_type, expected_revision):
        budget = await self.exact_budget(org_id, entity_type, entity_id, period_type, lock=True)
        if budget is None:
            raise ResourceNotFoundError("BudgetConfig", f"{entity_type}/{entity_id}/{period_type}")
        revision = budget.updated_at
        if revision.tzinfo is None:
            revision = revision.replace(tzinfo=UTC)
        if expected_revision.tzinfo is None or expected_revision != revision:
            raise ResourceConflictError("BudgetConfig", "revision", "changed; inspect before deleting")
        await self.db.delete(budget)
        await self.db.commit()

    async def set_exact_budget(self, org_id, entity_type, entity_id, period_type, request):
        from datetime import UTC

        from sqlalchemy.exc import IntegrityError

        budget = await self.exact_budget(org_id, entity_type, entity_id, period_type, lock=True)
        if budget is not None:
            current = budget.updated_at
            if current.tzinfo is None:
                current = current.replace(tzinfo=UTC)
            if (
                request.expect_absent
                or request.expected_revision is None
                or request.expected_revision.tzinfo is None
                or request.expected_revision != current
            ):
                raise ResourceConflictError("BudgetConfig", "revision", "changed; inspect this exact period before retrying")
            budget.budget_amount_usd = request.budget_amount_usd
            budget.enforcement_mode = request.enforcement_mode
        else:
            if not request.expect_absent or request.expected_revision is not None:
                raise ResourceConflictError("BudgetConfig", "revision", "absent; inspect this exact period before retrying")
            budget = BudgetConfig(
                org_id=org_id,
                entity_type=entity_type,
                entity_id=entity_id,
                period_type=period_type,
                budget_amount_usd=request.budget_amount_usd,
                enforcement_mode=request.enforcement_mode,
            )
            self.db.add(budget)
        try:
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            raise ResourceConflictError("BudgetConfig", "period", "concurrent creation; inspect the existing cap") from None
        await self.db.refresh(budget)
        result = self.budget_response(budget)
        result.advisory = await self._mis_partitioned_cap_advisory(org_id, entity_type, entity_id)
        return result

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
        period_type: str | None = None,
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
                *([BudgetConfig.period_type == period_type] if period_type is not None else []),
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
    ) -> BudgetConfigResponse:
        """
        Update budget configuration for an entity.

        Args:
            org_id: Organization ID
            entity_type: Entity type
            entity_id: Entity ID (from the route path; resolved for `user` and
                `root_user` — #4511, #4536)
            request: Update request

        Returns:
            Updated budget configuration

        Raises:
            ResourceNotFoundError: 404, if no budget exists for this entity
            UnresolvableUserEntityError: 422, if a person-scoped entity id cannot
                be resolved to the key its ledger uses (#4511, #4536)

        Issue #4511: this used to ``return None`` on every miss, which the route
        rendered as **HTTP 200 with a null body** — an operator editing a budget
        got a success response and no change. Every miss is now a 404. The same
        person-scoped id resolution as ``create_budget`` is applied, so editing a
        budget cannot re-introduce a mis-keyed row.
        """
        if not self.budget_service:
            raise ResourceNotFoundError("BudgetConfig", f"{entity_type}/{entity_id}")

        from src.shared.schemas.budget import BudgetUpdateRequest, EntityType

        try:
            entity = EntityType(entity_type)
        except ValueError:
            raise ResourceNotFoundError("BudgetConfig", f"{entity_type}/{entity_id}") from None

        entity_id = await self._resolve_person_entity_id(org_id, entity_type, entity_id)

        # Get existing budget
        budgets = await self.budget_service.get_budgets_for_entity(entity, entity_id, org_id)
        if not budgets:
            raise ResourceNotFoundError("BudgetConfig", f"{entity_type}/{entity_id}")

        budget_id = budgets[0].id
        update_request = BudgetUpdateRequest(
            budget_amount_usd=request.budget_amount_usd,
            enforcement_mode=request.enforcement_mode,
        )
        updated = await self.budget_service.update_budget(budget_id, update_request, org_id)
        if not updated:
            raise ResourceNotFoundError("BudgetConfig", f"{entity_type}/{entity_id}")

        return BudgetConfigResponse(
            org_id=updated.org_id,
            entity_type=updated.entity_type.value,
            entity_id=updated.entity_id,
            period_type=updated.period_type.value,
            budget_amount_usd=updated.budget_amount_usd,
            enforcement_mode=updated.enforcement_mode.value,
            updated_at=updated.updated_at,
        )

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

    async def list_org_teams(
        self,
        org_id: str,
        page: int = 1,
        page_size: int | None = None,
    ) -> tuple[list[TeamResponse], int]:
        """List every team in an org, across all departments (Issue #4840).

        The sibling :meth:`list_teams` is department-scoped, which is the wrong shape
        for the team pickers in the membership UI (T2a/T2b): assigning a user to a
        second team means choosing from every team in the org, and a caller should
        not have to enumerate departments and fan out to build that list.

        Args:
            org_id: Organization ID
            page: Page number (1-indexed)
            page_size: Items per page

        Returns:
            Tuple of (list of teams, total count)
        """
        if page_size is None:
            page_size = self.config.default_page_size

        page_size = min(page_size, self.config.max_page_size)
        offset = (page - 1) * page_size

        count_query = select(func.count()).select_from(Team).where(Team.org_id == org_id)
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        query = select(Team).where(Team.org_id == org_id).offset(offset).limit(page_size).order_by(Team.name)
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

        # Use the same durable provisioning and immutable-subject linking as
        # the identity API. Username is not a Cognito sub, and a failed AWS
        # write must not silently become a successful local-only user.
        if cognito_service:
            from src.admin.identity.cognito_sync import CognitoSyncService
            from src.admin.identity.schemas import UserCreateRequest as IdentityUserCreateRequest
            from src.admin.identity.users_service import UsersService

            created = await UsersService(self.db, cognito_sync=CognitoSyncService(cognito_service)).create_user(
                org_id,
                IdentityUserCreateRequest(email=request.email, team_id=team_id, name=request.name, role=request.role),
            )
            return UserResponse(**created.model_dump())

        # Legacy database-only mode retains its existing conflict behavior.
        existing = await self.db.execute(select(User).where(User.org_id == org_id, User.email == request.email))
        if existing.scalar_one_or_none():
            raise ResourceConflictError("User", "email", request.email)

        user = User(
            org_id=org_id,
            team_id=team_id,
            email=request.email,
            name=request.name,
            role=request.role,
            cognito_sub=None,
            cognito_username=None,
        )

        self.db.add(user)
        await self.db.flush()

        # Issue #4006: an admin-level create must also write the
        # tenant_memberships row that now carries that authority (#3987/#3998) —
        # same transaction as the users row. The role-assignment ceiling is
        # enforced by the caller (routes.py::add_user -> require_assignable_role).
        wrote_membership = is_admin_level_role(request.role)
        if wrote_membership:
            await upsert_tenant_membership(
                self.db,
                user_id=user.id,
                tenant_id=org_id,
                role=request.role,
                joined_via="admin_create",
            )

        await self.db.commit()
        await self.db.refresh(user)

        # Issue #4849: project the new membership to the DDB identity rows. This
        # path wrote a membership but never projected it.
        if wrote_membership:
            await project_member_org_ids(self.db, user_id=user.id)

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

        ``github_username`` is carried for the members panel's person label (Issue
        #4847), read from ``user_identities`` as a correlated scalar subquery for the
        reason ``list_platform_users`` documents at length: the unique index there is
        per (provider, provider_user_id, org_id), so one user CAN hold two GitHub rows
        and a LEFT JOIN would emit that person twice — inflating ``total`` and shifting
        every page boundary. It is the same subquery, kept as its own expression rather
        than shared, because that method applies no tenant filter by design and this one
        must (``User.org_id == org_id``).

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

        github_username = (
            select(UserIdentity.provider_username)
            .where(
                UserIdentity.user_id == User.id,
                func.lower(UserIdentity.provider) == "github",
                UserIdentity.verification_method.in_(PROVEN_METHODS),
            )
            .order_by(UserIdentity.created_at)
            .limit(1)
            .correlate(User)
            .scalar_subquery()
        )

        # Get total count
        count_query = select(func.count()).select_from(User).where(User.org_id == org_id)
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # Get paginated results
        query = (
            select(User, github_username.label("github_username")).where(User.org_id == org_id).offset(offset).limit(page_size).order_by(User.email)
        )
        result = await self.db.execute(query)
        rows = result.all()

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
                    github_username=linked_username,
                    created_at=user.created_at,
                    updated_at=user.updated_at,
                )
                for user, linked_username in rows
            ],
            total,
        )

    async def list_platform_users(
        self,
        q: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> tuple[list[PlatformUserResponse], int]:
        """Every member of the platform, paginated and searchable (Issue #4827).

        Exists because every other member listing in this API is per-org
        (``list_users_org``, ``list_cognito_users``) while a platform admin authoring a
        person-scoped rule may legitimately name **any** user in **any** org. Scoping
        this to the caller's own org would hide exactly the targets that authority
        covers, and the operator would be back to typing a UUID they cannot know.

        The route is what restricts this to platform admins. This method assumes that
        check already ran — it applies no tenant filter of its own, by design.

        **No filtering by kind.** Shadow and bot rows are listed alongside humans
        because ``bedrock_routing.service.require_scope_exists`` accepts any ``users``
        row, and a picker that omitted rows the server accepts would recreate the very
        gap this issue closes — a valid target with no way to select it.

        ``github_username`` comes from ``user_identities``, the same table
        ``getMemberGithubUserId`` reads (#4687): it is the bridge that is actually
        populated for GitHub-onboarded members, whereas ``users.cognito_username`` is
        only written on the admin-invite path. Read as a **correlated scalar subquery**
        rather than a LEFT JOIN on purpose: the unique index on ``user_identities`` is
        per (provider, provider_user_id, org_id), so one user *can* carry two GitHub
        rows, and a join would emit that person twice — inflating ``total``, shifting
        every page boundary, and offering the same option twice in the picker.

        Args:
            q: Case-insensitive substring match over email, display name, and GitHub
                username. Omitted/blank returns the unfiltered first page.
            page: Page number (1-indexed).
            page_size: Items per page, capped at the admin config maximum.

        Returns:
            Tuple of (list of members, total matching count).
        """
        if page_size is None:
            page_size = self.config.default_page_size

        page_size = min(page_size, self.config.max_page_size)
        offset = (page - 1) * page_size

        github_username = (
            select(UserIdentity.provider_username)
            .where(
                UserIdentity.user_id == User.id,
                func.lower(UserIdentity.provider) == "github",
                UserIdentity.verification_method.in_(PROVEN_METHODS),
            )
            .order_by(UserIdentity.created_at)
            .limit(1)
            .correlate(User)
            .scalar_subquery()
        )

        filters = []
        if q and q.strip():
            pattern = f"%{q.strip()}%"
            filters.append(
                or_(
                    User.email.ilike(pattern),
                    User.name.ilike(pattern),
                    github_username.ilike(pattern),
                )
            )

        count_query = select(func.count()).select_from(User).where(*filters)
        total = (await self.db.execute(count_query)).scalar_one()

        # Ordered by email — a stable, total ordering. Without one, two requests for
        # the same page can return different rows and a member becomes unreachable
        # through the picker without anything on screen saying so.
        query = select(User, github_username.label("github_username")).where(*filters).order_by(User.email).offset(offset).limit(page_size)
        rows = (await self.db.execute(query)).all()

        return (
            [
                PlatformUserResponse(
                    id=user.id,
                    org_id=user.org_id,
                    email=user.email,
                    name=user.name,
                    github_username=linked_username,
                )
                for user, linked_username in rows
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

    async def get_user_authz_state(self, org_id: str, user_id: str) -> UserAuthzState:
        """Resolve a target user's *authoritative* role for an authz decision.

        Issue #4019. ``users.role`` is a display mirror that nothing in authz
        reads (see ``AccessControl._resolve_membership_role``), so a permission
        check must not be made against it. This returns the membership role —
        the store that actually confers org-level authority — alongside the
        identifiers a role write needs.

        ``membership_role`` is None when the user has no membership row in this
        org; such a principal resolves to MEMBER (least privilege, #3987 PR 2).

        Args:
            org_id: Organization the user must belong to.
            user_id: ``users.id`` of the target.

        Returns:
            The target's identifiers and resolved membership role.

        Raises:
            ResourceNotFoundError: If the user does not exist in this org.
        """
        user = (await self.db.execute(select(User).where(User.id == user_id, User.org_id == org_id))).scalar_one_or_none()
        if not user:
            raise ResourceNotFoundError("User", user_id)

        membership = (
            await self.db.execute(
                select(TenantMembership).where(
                    TenantMembership.user_id == user.id,
                    TenantMembership.tenant_id == org_id,
                )
            )
        ).scalar_one_or_none()

        from src.shared.identity.workspaces import login_subject_for_user

        return UserAuthzState(
            user_id=user.id,
            org_id=user.org_id,
            cognito_sub=await login_subject_for_user(self.db, user),
            users_role=user.role,
            membership_role=membership.role if membership else None,
        )

    async def update_user(
        self,
        org_id: str,
        user_id: str,
        request: UserUpdateRequest,
    ) -> UserResponse:
        """Update a user's role and/or display name.

        Issue #4019. The authoritative write is ``tenant_memberships.role``:
        post-#3998/#4026 that row is the only store conferring org-level
        authority, so a change that touched only ``users.role`` + the Cognito
        attribute would display as a promotion and grant nothing (and a demotion
        would revoke nothing). Three stores are written, in deliberate order:

        1. ``tenant_memberships.role`` — authority. Via
           :func:`set_membership_role`, which lowers as well as raises;
           ``upsert_tenant_membership`` refuses to lower and would make every
           demotion a silent no-op.
        2. ``users.role`` — the display mirror the admin UI reads back.
        3. Cognito ``custom:role`` — the claims cache the pre-token-generation
           Lambda copies into the next access token.

        The DB transaction commits FIRST and the Cognito sync is best-effort
        afterwards, mirroring ``onboarding/approval.py``. Cognito-first would
        invert the risk: a Cognito success followed by a DB failure leaves a
        token asserting authority the authoritative store never granted. This
        way a sync failure leaves the authority correct and only the token stale
        — self-healing on the next successful role write or token refresh.

        Note ``users.role`` and ``tenant_memberships.role`` legitimately diverge
        for platform-level values: the membership row stores ``org_admin``
        because a tenant-scoped row must never confer platform authority
        (#3981). The response echoes ``users.role`` so the UI reflects what was
        requested.

        Authorization (ceiling, target-rank, org scope) is enforced by the
        caller — ``routes.py::update_user`` — before this runs.

        Args:
            org_id: Organization the user belongs to.
            user_id: ``users.id`` of the target.
            request: Fields to change; None means "leave unchanged".

        Returns:
            The updated user.

        Raises:
            ResourceNotFoundError: If the user does not exist in this org.
        """
        user = (await self.db.execute(select(User).where(User.id == user_id, User.org_id == org_id))).scalar_one_or_none()
        if not user:
            raise ResourceNotFoundError("User", user_id)

        previous_role = user.role
        if request.name is not None:
            user.name = request.name

        if request.role is not None:
            await set_membership_role(
                self.db,
                user_id=user.id,
                tenant_id=org_id,
                role=request.role,
            )
            user.role = request.role

        await self.db.commit()
        await self.db.refresh(user)

        # Issue #4849: a role change can *create* the membership row (when the
        # user had none in this tenant), so the org list can change here too.
        if request.role is not None:
            await project_member_org_ids(self.db, user_id=user.id)

        # Post-commit, best-effort. Serialize the claims cache write against
        # workspace selection, including secondary native-Cognito placements.
        if request.role is not None:
            from src.shared.identity.workspaces import login_subject_for_user, login_user, memberships_for_login

            subject = await login_subject_for_user(self.db, user)
            login = await login_user(self.db, subject) if subject else None
            if login:
                await self.db.execute(select(User.id).where(User.id == login.id).with_for_update())
                workspace_roles = None
                if previous_role in {"platform_admin", "admin"}:
                    from src.admin.config import membership_role_to_admin_role

                    _, memberships = await memberships_for_login(self.db, subject)
                    workspace_roles = {
                        org: membership_role_to_admin_role(pair[1].role if pair[1] else "member").value for org, pair in memberships.items()
                    }
                await asyncio.to_thread(
                    sync_cognito_role_claims,
                    cognito_sub=subject,
                    org_id=org_id,
                    role=request.role,
                    team_id=user.team_id or "",
                    metric_namespace="ADP/Admin",
                    metric_prefix="UserRoleUpdate",
                    only_if_current_org=True,
                    previous_role=previous_role,
                    workspace_roles=workspace_roles,
                )
                await self.db.commit()

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

    async def remove_user(
        self,
        org_id: str,
        user_id: str,
        cognito_service: CognitoService | None = None,
        identity_writer: "IdentityIndexWriter | None" = None,
    ) -> bool:
        """
        Remove a user.

        Delete the org-local account and its owned rows atomically. Refresh the
        sign-in projection and remove an unshared Cognito login only after commit.

        Args:
            org_id: Organization ID
            user_id: User ID
            cognito_service: Optional Cognito service for user deletion

        Returns:
            True if deleted

        Raises:
            ResourceNotFoundError: If user not found
        """
        result = await self.db.execute(select(User).where(User.id == user_id, User.org_id == org_id).with_for_update())
        user = result.scalar_one_or_none()

        if not user:
            raise ResourceNotFoundError("User", user_id)

        if await self.db.scalar(
            select(TenantMembership.id).where(TenantMembership.user_id == user_id, TenantMembership.tenant_id != org_id).limit(1)
        ):
            raise MemberRemovalConflictError(
                "This account also holds membership in another organization. Remove those memberships before deleting this account."
            )

        # Two snapshots of "this user's GitHub ids", because the two consumers below
        # ask different questions (#5664, A10).
        #
        # `github_ids` is the PROVEN set, and feeds the shared-login removal guard:
        # "does deleting this account strand another tenant's sign-in?" — an
        # authority question, so an unproven claim must not be able to block, or to
        # authorize, a deletion.
        #
        # `projected_github_ids` is EVERY GitHub id the user holds, and feeds the
        # `member_org_ids` projection refresh. Every formerly affected key must be
        # refreshed, even if this user's claim was unproven, to clear stale orgs.
        # The recomputation itself includes only proven surviving bindings: the
        # projected list grants sign-in eligibility and satisfies the webhook's
        # strict membership policy. A broad refresh set must not become a broad
        # authority set.
        identity_rows = (
            await self.db.execute(
                select(UserIdentity.provider_user_id, UserIdentity.verification_method).where(
                    UserIdentity.user_id == user_id, UserIdentity.provider == "github"
                )
            )
        ).all()
        github_ids = {pid for pid, method in identity_rows if pid and method in PROVEN_METHODS}
        projected_github_ids = {pid for pid, _ in identity_rows if pid}
        username = user.cognito_username
        if user.cognito_sub:
            from src.shared.identity.workspaces import PLACEMENT_VERIFICATION

            linked_membership = await self.db.scalar(
                select(TenantMembership.id)
                .join(UserIdentity, UserIdentity.user_id == TenantMembership.user_id)
                .where(
                    UserIdentity.provider == "cognito",
                    UserIdentity.provider_user_id == user.cognito_sub,
                    UserIdentity.verification_method == PLACEMENT_VERIFICATION,
                    TenantMembership.user_id != user_id,
                )
                .limit(1)
            )
            if linked_membership:
                raise MemberRemovalConflictError(
                    "This account owns the sign-in used by another organization. "
                    "Remove its other organization memberships before deleting this account."
                )
        if (user.cognito_sub or username) and github_ids:
            shared_login = await self.db.scalar(
                select(TenantMembership.id)
                .join(UserIdentity, UserIdentity.user_id == TenantMembership.user_id)
                .where(
                    UserIdentity.provider == "github",
                    UserIdentity.verification_method.in_(PROVEN_METHODS),
                    UserIdentity.provider_user_id.in_(github_ids),
                    TenantMembership.user_id != user_id,
                )
                .limit(1)
            )
            if shared_login:
                raise MemberRemovalConflictError(
                    "This account owns the sign-in used by another organization. "
                    "Remove its other organization memberships before deleting this account."
                )

        try:
            # ORM delete(User) tries to NULL the non-null backref foreign keys.
            # Delete the owned rows explicitly, then the scoped user, so the
            # operation also works on SQLite installations without FK cascades.
            for model in (TeamMembership, TenantMembership, UserIdentity, UserCredential):
                await self.db.execute(delete(model).where(model.user_id == user_id))
            await self.db.execute(delete(User).where(User.id == user_id, User.org_id == org_id))
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise MemberRemovalConflictError(
                "This member has related records that must be retained and cannot be deleted. No membership changes were saved."
            ) from exc

        await project_member_org_ids(self.db, user_id=user_id, provider_user_ids=projected_github_ids, writer=identity_writer)

        # Never remove the login for a database deletion that rolled back.
        if cognito_service and username:
            try:
                cognito_service.delete_user(username=username)
            except CognitoServiceError:
                pass  # Non-critical

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
        department_id: str | None = None,
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
        from decimal import Decimal

        # Local import, matching budget_helper.py: importing src.budget at module
        # scope pulls in src.budget.__init__ -> routes -> src.auth.
        from src.budget.utils import CALENDAR_PERIOD_TYPES, get_period_start_end
        from src.shared.schemas.budget import PeriodType

        if page_size is None:
            page_size = self.config.default_page_size

        page_size = min(page_size, self.config.max_page_size)
        offset = (page - 1) * page_size

        # Build base query. Issue #4328: calendar budgets only. A lifetime-scoped
        # run/chain cap has no calendar period, and this listing used to render one
        # as a monthly budget with a wrong current_usage_usd and utilization_pct —
        # a quiet mislabel rather than an error, so nobody noticed. Filtering the
        # count query too keeps `total`/`has_more` consistent with `items`.
        query = select(BudgetConfig).where(
            BudgetConfig.org_id == org_id,
            BudgetConfig.period_type.in_(CALENDAR_PERIOD_TYPES),
        )
        count_query = (
            select(func.count())
            .select_from(BudgetConfig)
            .where(
                BudgetConfig.org_id == org_id,
                BudgetConfig.period_type.in_(CALENDAR_PERIOD_TYPES),
            )
        )

        if entity_type:
            query = query.where(BudgetConfig.entity_type == entity_type)
            count_query = count_query.where(BudgetConfig.entity_type == entity_type)

        if department_id is not None:
            teams = select(Team.id).where(Team.org_id == org_id, Team.department_id == department_id)
            users = select(User.id).where(User.org_id == org_id, User.team_id.in_(teams))
            subjects = select(User.cognito_sub).where(User.org_id == org_id, User.team_id.in_(teams))
            allowed = or_(
                (BudgetConfig.entity_type == "department") & (BudgetConfig.entity_id == department_id),
                (BudgetConfig.entity_type == "team") & BudgetConfig.entity_id.in_(teams),
                (BudgetConfig.entity_type == "user") & BudgetConfig.entity_id.in_(subjects),
                (BudgetConfig.entity_type == "root_user") & BudgetConfig.entity_id.in_(users),
            )
            query = query.where(allowed)
            count_query = count_query.where(allowed)

        # Get total count
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # Get paginated budget configs
        query = query.offset(offset).limit(page_size).order_by(BudgetConfig.entity_type, BudgetConfig.entity_id, BudgetConfig.period_type)
        result = await self.db.execute(query)
        budget_configs = result.scalars().all()

        # Resolve user display names from Cognito (batch lookup)
        user_display_names: dict[str, str] = {}
        user_entity_ids = [c.entity_id for c in budget_configs if c.entity_type == "user"]
        # Issue #4536: cloud-agent rows are keyed by canonical `users.id`, which
        # Cognito knows nothing about, so they need their own lookup — through
        # `users` — or every cloud-agent budget renders as a bare UUID.
        root_user_display_names = await self._resolve_root_user_display_names(
            org_id, [c.entity_id for c in budget_configs if c.entity_type == "root_user"]
        )
        # Issue #4948: name the tenancy entities, and learn which ones no longer exist.
        tenancy_names = await self._resolve_tenancy_entities(org_id, [(c.entity_type, c.entity_id) for c in budget_configs])
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

        for config in budget_configs:
            # Issue #4328: use the shared period helper rather than re-deriving the
            # window inline. The previous `if daily / elif weekly / else: monthly`
            # treated every unrecognised period type as monthly, which is what made
            # a run cap render as a monthly budget instead of being excluded. The
            # query above guarantees only calendar types reach here, so this cannot
            # raise.
            period_start, _ = get_period_start_end(PeriodType(config.period_type))

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

            # Resolve display name for the two person-scoped entity types. They are
            # keyed in different namespaces, so each reads its own map — a shared
            # lookup would silently show one person's name against another's row.
            display_name = None
            # Issue #4948: `False` for every kind this check does not apply to, so a
            # person-scoped row is never flagged by a lookup that was never run for it.
            unresolved = False
            if config.entity_type == "user":
                display_name = user_display_names.get(config.entity_id)
            elif config.entity_type == "root_user":
                display_name = root_user_display_names.get(config.entity_id)
            elif config.entity_type in ("org", "department", "team"):
                display_name = tenancy_names.get((config.entity_type, config.entity_id))
                unresolved = display_name is None

            items.append(
                BudgetListItem(
                    entity_type=config.entity_type,
                    entity_id=config.entity_id,
                    entity_display_name=display_name,
                    entity_unresolved=unresolved,
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

    async def _resolve_tenancy_entities(self, org_id: str, keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
        """Resolve org/department/team config keys to their tenancy row's name.

        Issue #4948. Returns a map keyed by ``(entity_type, entity_id)`` holding the
        display name of the row each key points at. A key that is ABSENT from the
        result has no such row in this org — which is the only signal available for
        "this config governs nobody", and the reason the list surfaces it (see
        ``entity_unresolved``) instead of silently rendering a bare id that looks
        exactly like a working one.

        Why the caller must not treat absence as "delete it": the entity may have been
        renamed away, deleted, or — the case that made this issue — authored from the
        old Cognito-sourced picker, which stored a Cognito *group name* where the
        column holds a ``teams.id``. Those rows are real spend controls someone
        believed they had set, so they are flagged for an operator to fix, never
        hidden. Hiding them reproduces #4511 with the evidence removed.

        Scoped to ``org_id`` in every query: a department or team id from another
        tenant must read as unresolved here, not borrow that tenant's name.
        """
        wanted: dict[str, set[str]] = {"org": set(), "department": set(), "team": set()}
        for entity_type, entity_id in keys:
            if entity_type in wanted:
                wanted[entity_type].add(entity_id)

        resolved: dict[tuple[str, str], str] = {}

        if wanted["org"]:
            # An org-level config resolves only when its entity id IS the partition it
            # is stored in. Enforcement fills both columns from the same
            # `attributed_org_id`, so an `org` row naming a DIFFERENT org than its own
            # partition can never be matched however real that other org is — the
            # partition trap this issue's org picker had to be built around. Looking it
            # up in `organizations` by id alone would resolve it to a name and make an
            # unmatchable row read as healthy.
            rows = await self.db.execute(select(Organization).where(Organization.id.in_(wanted["org"] & {org_id})))
            for org in rows.scalars().all():
                resolved[("org", org.id)] = org.name or org.id

        if wanted["department"]:
            rows = await self.db.execute(select(Department).where(Department.org_id == org_id, Department.id.in_(wanted["department"])))
            for dept in rows.scalars().all():
                resolved[("department", dept.id)] = dept.name or dept.id

        if wanted["team"]:
            rows = await self.db.execute(select(Team).where(Team.org_id == org_id, Team.id.in_(wanted["team"])))
            for team in rows.scalars().all():
                resolved[("team", team.id)] = team.name or team.id

        return resolved

    async def _resolve_root_user_display_names(self, org_id: str, entity_ids: list[str]) -> dict[str, str]:
        """Map canonical ``users.id`` budget keys to a human-readable name.

        Issue #4536. ``root_user`` rows are keyed by canonical ``users.id``, so the
        Cognito batch lookup used for ``user`` rows cannot name them — it is keyed
        by sub. This resolves them from ``users`` instead, org-scoped, so a row from
        another tenant can never be labelled with a local person's name.

        ``service:``-qualified ids (unattended CI/EventBridge triggers, #4344) have
        no ``users`` row by design and are simply absent from the result; the caller
        renders the raw id for them, which is the honest label for an automation.
        """
        resolvable = [eid for eid in entity_ids if not eid.startswith("service:")]
        if not resolvable:
            return {}

        rows = await self.db.execute(select(User).where(User.org_id == org_id, User.id.in_(resolvable)))
        return {user.id: (user.name or user.email or user.id) for user in rows.scalars().all()}

    async def _resolve_person_entity_id(self, org_id: str, entity_type: str, entity_id: str) -> str:
        """Normalise a person-scoped entity id to the key its ledger is written with.

        The two per-person budget kinds are keyed in different namespaces, and each
        must be written with the one its own ledger and read path use:

        * ``user`` — the person's **direct** traffic, keyed by Cognito sub
          (#4511; ``enforcement_service`` builds ``(USER, context.user_id)``).
        * ``root_user`` — the spend of agent runs they triggered, keyed by canonical
          ``users.id`` (#4536; ``me_routes._resolve_root_principal`` derives the
          same key for the read path).

        Every other entity type is returned untouched: org/team/department ids are
        already their own keys, and running them through a ``users`` lookup could
        only ever 422 a valid budget.

        Raises:
            UnresolvableUserEntityError: 422; the caller persists nothing.
        """
        if entity_type == "user":
            return await resolve_user_entity_id(self.db, org_id, entity_id)
        if entity_type == "root_user":
            return await resolve_root_user_entity_id(self.db, org_id, entity_id)
        return entity_id

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
            UnresolvableUserEntityError: 422, if a person-scoped entity id cannot
                be resolved to the key its ledger uses (#4511, #4536)
        """
        # Issue #4511: normalise a person-scoped entity id to its ledger key BEFORE
        # the conflict probe, so the duplicate check and the persisted row agree with
        # each other and with the key enforcement matches on. Resolving after the
        # probe would let `GitHub_123` and its own sub both insert, colliding on
        # uq_budget_config.
        entity_id = await self._resolve_person_entity_id(org_id, request.entity_type, request.entity_id)

        # Check for existing budget with same entity and period
        existing = await self.db.execute(
            select(BudgetConfig).where(
                BudgetConfig.org_id == org_id,
                BudgetConfig.entity_type == request.entity_type,
                BudgetConfig.entity_id == entity_id,
                BudgetConfig.period_type == request.period_type,
            )
        )
        if existing.scalar_one_or_none():
            raise ResourceConflictError(
                "BudgetConfig",
                "entity_type/entity_id/period_type",
                # .value: period_type is a PeriodType (#4328), and str()/f-string on a
                # str-Enum renders "PeriodType.MONTHLY", not "monthly".
                f"{request.entity_type}/{entity_id}/{request.period_type.value}",
            )

        budget = BudgetConfig(
            org_id=org_id,
            entity_type=request.entity_type,
            entity_id=entity_id,
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
            # Deliberately computed AFTER the commit: the cap exists either way, and
            # an advisory is a sentence about a write that already happened.
            advisory=await self._mis_partitioned_cap_advisory(org_id, budget.entity_type, budget.entity_id),
        )

    async def _mis_partitioned_cap_advisory(self, org_id: str, entity_type: str, entity_id: str) -> str | None:
        """Warn when a cloud-agent cap was authored where the person's spend does not land.

        Issue #4669. Only `root_user` caps can have this defect: they are keyed by
        canonical `users.id` and a person can hold a different one per tenant, so the
        cap and the accrual can end up in different partitions (#4620). Every other
        entity type is scoped to the partition it was written in by construction.

        **Never raises, and never blocks.** Two independent reasons, and both are
        requirements rather than caution:

        - The budget is already committed. Letting this read fail the request would
          report a successful create as an error, and the operator would author it
          again — reaching a 409 for a row they were told did not exist.
        - The check reads a foreign tenant's ledger. A cross-tenant read must not be
          able to veto a write inside this tenant, so its failure mode is "no advice",
          never "no cap".

        `except Exception` rather than a fault tuple for exactly that reason: the
        distinction between an outage and a code defect matters to the log, not to the
        caller — every outcome here is still a 201 with the cap in place.
        """
        if entity_type != "root_user":
            return None

        # Local import, matching `budget_helper.py` and the `src.budget.utils` import
        # above: importing `src.budget` at module scope pulls in
        # `src.budget.__init__` -> routes -> `src.auth`. `person_accrual` is a leaf
        # module precisely so this import stays cheap (#4669).
        from src.budget.person_accrual import count_foreign_accrual_partitions

        try:
            partitions = await count_foreign_accrual_partitions(self.db, org_id, entity_id)
        except Exception:
            logger.warning(
                "Could not check where cloud-agent spend accrues for the budget just created; returning it without an advisory",
                exc_info=True,
            )
            return None

        if partitions == 0:
            return None

        # The COUNT only — never which workspaces, never their figures. The author is
        # an admin of this tenant with no authority to learn the others (design note
        # `4620-cross-org-person-budgets.md` §7.2).
        workspaces = "workspace" if partitions == 1 else "workspaces"
        return (
            f"This person's agent spend currently accrues in {partitions} other {workspaces}, not here. "
            "This cap governs only the spend that bills to this workspace, so it may never be reached."
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

        # Issue #4948: name the tenancy entities, and flag the ones that resolve to
        # nothing in this org — a limit on a stale or wrong-namespace id is silently
        # not in force, and the list is where an operator can see that.
        tenancy_names = await self._resolve_tenancy_entities(org_id, [(c.entity_type, c.entity_id) for c in configs])

        items = [
            RateLimitListItem(
                entity_type=config.entity_type,
                entity_id=config.entity_id,
                entity_display_name=tenancy_names.get((config.entity_type, config.entity_id)),
                entity_unresolved=(
                    config.entity_type in ("org", "department", "team") and (config.entity_type, config.entity_id) not in tenancy_names
                ),
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
    # Dashboard Metrics (Issue #1003)
    # =============================================================================

    async def get_platform_metrics_24h(self) -> dict:
        """
        Get platform-wide dashboard metrics for the last 24 hours.

        Returns aggregate counts from usage_logs: total requests, tokens, cost,
        active users, error rate, and active organizations.
        """
        from datetime import datetime

        from sqlalchemy import case, func

        from src.shared.models.usage import UsageLog

        cutoff = datetime.now(UTC) - timedelta(hours=24)

        query = select(
            func.count(UsageLog.id).label("total_requests"),
            func.coalesce(func.sum(UsageLog.input_tokens + UsageLog.output_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("total_cost"),
            func.count(func.distinct(UsageLog.user_id)).label("active_users"),
            func.count(func.distinct(UsageLog.org_id)).label("total_organizations"),
            (func.count(case((UsageLog.status_code >= 500, 1))) * 100.0 / func.coalesce(func.nullif(func.count(UsageLog.id), 0), 1)).label(
                "error_rate"
            ),
        ).where(UsageLog.timestamp >= cutoff)

        result = await self.db.execute(query)
        row = result.one()

        return {
            "total_requests_24h": row.total_requests or 0,
            "total_tokens_24h": row.total_tokens or 0,
            "total_cost_24h": row.total_cost or 0,
            "active_users_24h": row.active_users or 0,
            "total_organizations": row.total_organizations or 0,
            "error_rate_24h": float(row.error_rate or 0.0),
        }

    async def get_org_metrics_24h(self, org_id: str) -> dict:
        """
        Get per-org dashboard metrics for the last 24 hours.

        Same shape as platform metrics but scoped to a single org.
        """
        from datetime import datetime

        from sqlalchemy import case, func

        from src.shared.models.usage import UsageLog

        cutoff = datetime.now(UTC) - timedelta(hours=24)

        query = select(
            func.count(UsageLog.id).label("total_requests"),
            func.coalesce(func.sum(UsageLog.input_tokens + UsageLog.output_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(UsageLog.cost_usd), 0).label("total_cost"),
            func.count(func.distinct(UsageLog.user_id)).label("active_users"),
            (func.count(case((UsageLog.status_code >= 500, 1))) * 100.0 / func.coalesce(func.nullif(func.count(UsageLog.id), 0), 1)).label(
                "error_rate"
            ),
        ).where(
            UsageLog.timestamp >= cutoff,
            UsageLog.org_id == org_id,
        )

        result = await self.db.execute(query)
        row = result.one()

        return {
            "total_requests_24h": row.total_requests or 0,
            "total_tokens_24h": row.total_tokens or 0,
            "total_cost_24h": row.total_cost or 0,
            "active_users_24h": row.active_users or 0,
            "error_rate_24h": float(row.error_rate or 0.0),
        }

    async def get_top_organizations_24h(self, limit: int = 5) -> list[dict]:
        """
        Get top organizations by request count in the last 24 hours.
        """
        from datetime import datetime

        from sqlalchemy import func

        from src.shared.models.usage import UsageLog

        cutoff = datetime.now(UTC) - timedelta(hours=24)

        query = (
            select(
                UsageLog.org_id,
                func.count(UsageLog.id).label("request_count"),
                func.coalesce(func.sum(UsageLog.input_tokens + UsageLog.output_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(UsageLog.cost_usd), 0).label("total_cost"),
            )
            .where(UsageLog.timestamp >= cutoff)
            .group_by(UsageLog.org_id)
            .order_by(func.count(UsageLog.id).desc())
            .limit(limit)
        )

        result = await self.db.execute(query)
        rows = result.all()

        top_orgs = []
        for row in rows:
            # Try to get org name from the organizations table
            org_name = row.org_id
            try:
                org = await self.get_organization(row.org_id)
                org_name = org.name
            except Exception:
                pass
            top_orgs.append(
                {
                    "org_id": row.org_id,
                    "name": org_name,
                    "request_count": row.request_count,
                    "total_tokens": row.total_tokens or 0,
                    "total_cost": float(row.total_cost or 0),
                }
            )

        return top_orgs

    async def get_top_departments_24h(self, org_id: str, limit: int = 5) -> list[dict]:
        """
        Get top departments by request count for an org in the last 24 hours.
        """
        from datetime import datetime

        from sqlalchemy import func

        from src.shared.models.usage import UsageLog

        cutoff = datetime.now(UTC) - timedelta(hours=24)

        query = (
            select(
                UsageLog.department_id,
                func.count(UsageLog.id).label("request_count"),
                func.coalesce(func.sum(UsageLog.input_tokens + UsageLog.output_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(UsageLog.cost_usd), 0).label("total_cost"),
            )
            .where(
                UsageLog.timestamp >= cutoff,
                UsageLog.org_id == org_id,
            )
            .group_by(UsageLog.department_id)
            .order_by(func.count(UsageLog.id).desc())
            .limit(limit)
        )

        result = await self.db.execute(query)
        rows = result.all()

        return [
            {
                "department_id": row.department_id,
                "request_count": row.request_count,
                "total_tokens": row.total_tokens or 0,
                "total_cost": float(row.total_cost or 0),
            }
            for row in rows
        ]

    async def get_top_models_24h(self, org_id: str, limit: int = 5) -> list[dict]:
        """
        Get top models by request count for an org in the last 24 hours.
        """
        from datetime import datetime

        from sqlalchemy import func

        from src.shared.models.usage import UsageLog

        cutoff = datetime.now(UTC) - timedelta(hours=24)

        query = (
            select(
                UsageLog.model,
                func.count(UsageLog.id).label("request_count"),
                func.coalesce(func.sum(UsageLog.input_tokens + UsageLog.output_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(UsageLog.cost_usd), 0).label("total_cost"),
            )
            .where(
                UsageLog.timestamp >= cutoff,
                UsageLog.org_id == org_id,
            )
            .group_by(UsageLog.model)
            .order_by(func.count(UsageLog.id).desc())
            .limit(limit)
        )

        result = await self.db.execute(query)
        rows = result.all()

        return [
            {
                "model": row.model,
                "request_count": row.request_count,
                "total_tokens": row.total_tokens or 0,
                "total_cost": float(row.total_cost or 0),
            }
            for row in rows
        ]

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
