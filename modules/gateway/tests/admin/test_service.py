"""Unit tests for AdminService."""

import pytest

from src.admin.exceptions import PoolConfigurationError, ResourceConflictError, ResourceNotFoundError
from src.admin.schemas import (
    OrganizationCreateRequest,
    OrganizationUpdateRequest,
    PoolAccountCreateRequest,
    RateLimitConfigUpdateRequest,
)
from src.admin.service import AdminService
from src.shared.models.organization import Organization
from src.shared.models.usage import BedrockPoolAccount


class TestAdminServiceOrganizations:
    """Tests for organization CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_organization(self, admin_service: AdminService):
        """Test creating a new organization."""
        request = OrganizationCreateRequest(
            name="New Test Org",
            aws_accounts=["123456789012"],
            role_mappings={"admin": "admin-role"},
            settings={"feature": True},
        )

        result = await admin_service.create_organization(request)

        assert result.name == "New Test Org"
        assert result.aws_accounts == ["123456789012"]
        assert result.role_mappings == {"admin": "admin-role"}
        assert result.settings == {"feature": True}
        assert result.id is not None

    @pytest.mark.asyncio
    async def test_create_organization_duplicate_name(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test creating organization with duplicate name fails."""
        request = OrganizationCreateRequest(
            name="Test Organization 1",  # Already exists
            aws_accounts=["999999999999"],
        )

        with pytest.raises(ResourceConflictError) as exc_info:
            await admin_service.create_organization(request)

        assert "Test Organization 1" in str(exc_info.value.message)

    @pytest.mark.asyncio
    async def test_get_organization(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test getting an organization by ID."""
        result = await admin_service.get_organization("org-001")

        assert result.id == "org-001"
        assert result.name == "Test Organization 1"
        assert result.aws_accounts == ["111111111111"]

    @pytest.mark.asyncio
    async def test_get_organization_not_found(self, admin_service: AdminService):
        """Test getting non-existent organization fails."""
        with pytest.raises(ResourceNotFoundError) as exc_info:
            await admin_service.get_organization("non-existent")

        assert "non-existent" in str(exc_info.value.message)

    @pytest.mark.asyncio
    async def test_list_organizations(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test listing organizations."""
        orgs, total = await admin_service.list_organizations()

        assert total == 3
        assert len(orgs) == 3

    @pytest.mark.asyncio
    async def test_list_organizations_with_filter(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test listing organizations with filter."""
        orgs, total = await admin_service.list_organizations(org_ids=["org-001", "org-002"])

        assert total == 2
        assert len(orgs) == 2

    @pytest.mark.asyncio
    async def test_list_organizations_pagination(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test listing organizations with pagination."""
        orgs, total = await admin_service.list_organizations(page=1, page_size=2)

        assert total == 3
        assert len(orgs) == 2

        orgs2, _ = await admin_service.list_organizations(page=2, page_size=2)
        assert len(orgs2) == 1

    @pytest.mark.asyncio
    async def test_update_organization(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test updating an organization."""
        request = OrganizationUpdateRequest(
            name="Updated Org Name",
            settings={"new_feature": True},
        )

        result = await admin_service.update_organization("org-001", request)

        assert result.name == "Updated Org Name"
        assert result.settings == {"new_feature": True}
        # Unchanged fields remain
        assert result.aws_accounts == ["111111111111"]

    @pytest.mark.asyncio
    async def test_update_organization_partial(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test partial update of organization."""
        request = OrganizationUpdateRequest(
            aws_accounts=["111111111111", "888888888888"],
        )

        result = await admin_service.update_organization("org-001", request)

        assert result.aws_accounts == ["111111111111", "888888888888"]
        assert result.name == "Test Organization 1"  # Unchanged

    @pytest.mark.asyncio
    async def test_update_organization_not_found(self, admin_service: AdminService):
        """Test updating non-existent organization fails."""
        request = OrganizationUpdateRequest(name="New Name")

        with pytest.raises(ResourceNotFoundError):
            await admin_service.update_organization("non-existent", request)

    @pytest.mark.asyncio
    async def test_update_organization_duplicate_name(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test updating to duplicate name fails."""
        request = OrganizationUpdateRequest(name="Test Organization 2")  # Already exists

        with pytest.raises(ResourceConflictError):
            await admin_service.update_organization("org-001", request)

    @pytest.mark.asyncio
    async def test_delete_organization(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test deleting an organization."""
        result = await admin_service.delete_organization("org-001")

        assert result is True

        # Verify it's deleted
        with pytest.raises(ResourceNotFoundError):
            await admin_service.get_organization("org-001")

    @pytest.mark.asyncio
    async def test_delete_organization_not_found(self, admin_service: AdminService):
        """Test deleting non-existent organization fails."""
        with pytest.raises(ResourceNotFoundError):
            await admin_service.delete_organization("non-existent")


class TestAdminServicePool:
    """Tests for pool management operations."""

    @pytest.mark.asyncio
    async def test_get_pool_status(self, admin_service: AdminService, sample_pool_accounts: list[BedrockPoolAccount]):
        """Test getting pool status."""
        result = await admin_service.get_pool_status()

        assert result.total_accounts == 3
        assert result.healthy_accounts == 2
        assert result.unhealthy_accounts == 1
        assert len(result.accounts) == 3

    @pytest.mark.asyncio
    async def test_get_pool_status_empty(self, admin_service: AdminService):
        """Test getting pool status when no accounts exist."""
        result = await admin_service.get_pool_status()

        assert result.total_accounts == 0
        assert result.healthy_accounts == 0
        assert result.unhealthy_accounts == 0

    @pytest.mark.asyncio
    async def test_add_pool_account(self, admin_service: AdminService):
        """Test adding a pool account."""
        request = PoolAccountCreateRequest(
            account_id="888888888888",
            role_arn="arn:aws:iam::888888888888:role/BedrockRole",
            region="eu-west-1",
        )

        result = await admin_service.add_pool_account(request)

        assert result.account_id == "888888888888"
        assert result.role_arn == "arn:aws:iam::888888888888:role/BedrockRole"
        assert result.region == "eu-west-1"
        assert result.is_healthy is True

    @pytest.mark.asyncio
    async def test_add_pool_account_duplicate_role_arn(self, admin_service: AdminService, sample_pool_accounts: list[BedrockPoolAccount]):
        """Test adding pool account with duplicate role ARN fails."""
        request = PoolAccountCreateRequest(
            account_id="999999999999",
            role_arn="arn:aws:iam::555555555555:role/BedrockRole",  # Already exists
            region="us-east-1",
        )

        with pytest.raises(PoolConfigurationError):
            await admin_service.add_pool_account(request)

    @pytest.mark.asyncio
    async def test_remove_pool_account(self, admin_service: AdminService, sample_pool_accounts: list[BedrockPoolAccount]):
        """Test removing a pool account."""
        result = await admin_service.remove_pool_account("pool-001")

        assert result is True

        # Verify it's removed
        status = await admin_service.get_pool_status()
        assert status.total_accounts == 2

    @pytest.mark.asyncio
    async def test_remove_pool_account_not_found(self, admin_service: AdminService):
        """Test removing non-existent pool account fails."""
        with pytest.raises(ResourceNotFoundError):
            await admin_service.remove_pool_account("non-existent")


class TestAdminServiceRateLimitConfig:
    """Tests for rate limit configuration operations."""

    @pytest.mark.asyncio
    async def test_get_ratelimit_config_not_found(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test getting rate limit config when none exists."""
        result = await admin_service.get_ratelimit_config("org-001", "org", "org-001")

        assert result is None

    @pytest.mark.asyncio
    async def test_update_ratelimit_config_create(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test creating rate limit config through update."""
        request = RateLimitConfigUpdateRequest(
            rpm=100,
            tpm=10000,
            concurrent_requests=10,
        )

        result = await admin_service.update_ratelimit_config("org-001", "org", "org-001", request)

        assert result.org_id == "org-001"
        assert result.rpm == 100
        assert result.tpm == 10000
        assert result.concurrent_requests == 10

    @pytest.mark.asyncio
    async def test_update_ratelimit_config_update(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test updating existing rate limit config."""
        # First create
        create_request = RateLimitConfigUpdateRequest(rpm=100, tpm=10000)
        await admin_service.update_ratelimit_config("org-001", "org", "org-001", create_request)

        # Then update
        update_request = RateLimitConfigUpdateRequest(rpm=200)
        result = await admin_service.update_ratelimit_config("org-001", "org", "org-001", update_request)

        assert result.rpm == 200
        assert result.tpm == 10000  # Unchanged

    @pytest.mark.asyncio
    async def test_get_ratelimit_config_after_create(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test getting rate limit config after creation."""
        request = RateLimitConfigUpdateRequest(rpm=50)
        await admin_service.update_ratelimit_config("org-001", "org", "org-001", request)

        result = await admin_service.get_ratelimit_config("org-001", "org", "org-001")

        assert result is not None
        assert result.rpm == 50


# Issue #185: Budget List/Create/Delete Tests


class TestAdminServiceBudgetList:
    """Tests for budget list/create/delete operations (Issue #185)."""

    @pytest.mark.asyncio
    async def test_get_budgets_list_empty(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test getting empty budget list."""
        result = await admin_service.get_budgets_list("org-001")

        assert result.total == 0
        assert len(result.items) == 0
        assert result.page == 1
        assert result.has_more is False

    @pytest.mark.asyncio
    async def test_create_budget(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test creating a new budget."""
        from decimal import Decimal

        from src.admin.schemas import BudgetCreateRequest

        request = BudgetCreateRequest(
            entity_type="team",
            entity_id="platform-team",
            period_type="monthly",
            budget_amount_usd=Decimal("500.00"),
            enforcement_mode="hard",
        )

        result = await admin_service.create_budget("org-001", request)

        assert result.org_id == "org-001"
        assert result.entity_type == "team"
        assert result.entity_id == "platform-team"
        assert result.period_type == "monthly"
        assert result.budget_amount_usd == Decimal("500.00")
        assert result.enforcement_mode == "hard"

    @pytest.mark.asyncio
    async def test_create_budget_duplicate_fails(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test creating duplicate budget fails."""
        from decimal import Decimal

        from src.admin.schemas import BudgetCreateRequest

        request = BudgetCreateRequest(
            entity_type="team",
            entity_id="platform-team",
            period_type="monthly",
            budget_amount_usd=Decimal("500.00"),
            enforcement_mode="hard",
        )

        await admin_service.create_budget("org-001", request)

        # Attempt to create duplicate
        with pytest.raises(ResourceConflictError):
            await admin_service.create_budget("org-001", request)

    @pytest.mark.asyncio
    async def test_get_budgets_list_after_create(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test budget list after creating budgets."""
        from decimal import Decimal

        from src.admin.schemas import BudgetCreateRequest

        # Create multiple budgets
        for i in range(3):
            request = BudgetCreateRequest(
                entity_type="team",
                entity_id=f"team-{i}",
                period_type="monthly",
                budget_amount_usd=Decimal("100.00") * (i + 1),
                enforcement_mode="hard",
            )
            await admin_service.create_budget("org-001", request)

        result = await admin_service.get_budgets_list("org-001")

        assert result.total == 3
        assert len(result.items) == 3

    @pytest.mark.asyncio
    async def test_get_budgets_list_filter_by_entity_type(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test filtering budget list by entity type."""
        from decimal import Decimal

        from src.admin.schemas import BudgetCreateRequest

        # Create budgets for different entity types
        for entity_type, entity_id in [("team", "team-1"), ("user", "user-1"), ("team", "team-2")]:
            request = BudgetCreateRequest(
                entity_type=entity_type,
                entity_id=entity_id,
                period_type="monthly",
                budget_amount_usd=Decimal("100.00"),
                enforcement_mode="hard",
            )
            await admin_service.create_budget("org-001", request)

        # Filter by team
        result = await admin_service.get_budgets_list("org-001", entity_type="team")
        assert result.total == 2

        # Filter by user
        result = await admin_service.get_budgets_list("org-001", entity_type="user")
        assert result.total == 1

    @pytest.mark.asyncio
    async def test_get_budgets_list_pagination(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test budget list pagination."""
        from decimal import Decimal

        from src.admin.schemas import BudgetCreateRequest

        # Create 5 budgets
        for i in range(5):
            request = BudgetCreateRequest(
                entity_type="team",
                entity_id=f"team-{i}",
                period_type="monthly",
                budget_amount_usd=Decimal("100.00"),
                enforcement_mode="hard",
            )
            await admin_service.create_budget("org-001", request)

        # Get first page
        result = await admin_service.get_budgets_list("org-001", page=1, page_size=2)
        assert result.total == 5
        assert len(result.items) == 2
        assert result.has_more is True

        # Get second page
        result = await admin_service.get_budgets_list("org-001", page=2, page_size=2)
        assert len(result.items) == 2
        assert result.has_more is True

        # Get third page
        result = await admin_service.get_budgets_list("org-001", page=3, page_size=2)
        assert len(result.items) == 1
        assert result.has_more is False

    @pytest.mark.asyncio
    async def test_delete_budget(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test deleting a budget."""
        from decimal import Decimal

        from src.admin.schemas import BudgetCreateRequest

        request = BudgetCreateRequest(
            entity_type="team",
            entity_id="platform-team",
            period_type="monthly",
            budget_amount_usd=Decimal("500.00"),
            enforcement_mode="hard",
        )

        await admin_service.create_budget("org-001", request)

        # Delete the budget
        result = await admin_service.delete_budget("org-001", "team", "platform-team", "monthly")
        assert result is True

        # Verify it's gone
        budgets = await admin_service.get_budgets_list("org-001")
        assert budgets.total == 0

    @pytest.mark.asyncio
    async def test_delete_budget_not_found(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test deleting non-existent budget fails."""
        with pytest.raises(ResourceNotFoundError):
            await admin_service.delete_budget("org-001", "team", "non-existent", "monthly")


# Issue #185: Rate Limit List/Create/Delete Tests


class TestAdminServiceRateLimitList:
    """Tests for rate limit list/create/delete operations (Issue #185)."""

    @pytest.mark.asyncio
    async def test_get_ratelimits_list_empty(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test getting empty rate limit list."""
        result = await admin_service.get_ratelimits_list("org-001")

        assert result.total == 0
        assert len(result.items) == 0
        assert result.page == 1
        assert result.has_more is False

    @pytest.mark.asyncio
    async def test_create_ratelimit(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test creating a new rate limit."""
        from src.admin.schemas import RateLimitCreateRequest

        request = RateLimitCreateRequest(
            entity_type="user",
            entity_id="user-123",
            rpm=60,
            tpm=100000,
            concurrent_requests=5,
        )

        result = await admin_service.create_ratelimit("org-001", request)

        assert result.org_id == "org-001"
        assert result.entity_type == "user"
        assert result.entity_id == "user-123"
        assert result.rpm == 60
        assert result.tpm == 100000
        assert result.concurrent_requests == 5

    @pytest.mark.asyncio
    async def test_create_ratelimit_duplicate_fails(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test creating duplicate rate limit fails."""
        from src.admin.schemas import RateLimitCreateRequest

        request = RateLimitCreateRequest(
            entity_type="user",
            entity_id="user-123",
            rpm=60,
        )

        await admin_service.create_ratelimit("org-001", request)

        # Attempt to create duplicate
        with pytest.raises(ResourceConflictError):
            await admin_service.create_ratelimit("org-001", request)

    @pytest.mark.asyncio
    async def test_get_ratelimits_list_after_create(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test rate limit list after creating rate limits."""
        from src.admin.schemas import RateLimitCreateRequest

        # Create multiple rate limits
        for i in range(3):
            request = RateLimitCreateRequest(
                entity_type="user",
                entity_id=f"user-{i}",
                rpm=60 + i * 10,
            )
            await admin_service.create_ratelimit("org-001", request)

        result = await admin_service.get_ratelimits_list("org-001")

        assert result.total == 3
        assert len(result.items) == 3

    @pytest.mark.asyncio
    async def test_get_ratelimits_list_filter_by_entity_type(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test filtering rate limit list by entity type."""
        from src.admin.schemas import RateLimitCreateRequest

        # Create rate limits for different entity types
        for entity_type, entity_id in [("team", "team-1"), ("user", "user-1"), ("team", "team-2")]:
            request = RateLimitCreateRequest(
                entity_type=entity_type,
                entity_id=entity_id,
                rpm=60,
            )
            await admin_service.create_ratelimit("org-001", request)

        # Filter by team
        result = await admin_service.get_ratelimits_list("org-001", entity_type="team")
        assert result.total == 2

        # Filter by user
        result = await admin_service.get_ratelimits_list("org-001", entity_type="user")
        assert result.total == 1

    @pytest.mark.asyncio
    async def test_delete_ratelimit(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test deleting a rate limit."""
        from src.admin.schemas import RateLimitCreateRequest

        request = RateLimitCreateRequest(
            entity_type="user",
            entity_id="user-123",
            rpm=60,
        )

        await admin_service.create_ratelimit("org-001", request)

        # Delete the rate limit
        result = await admin_service.delete_ratelimit("org-001", "user", "user-123")
        assert result is True

        # Verify it's gone
        ratelimits = await admin_service.get_ratelimits_list("org-001")
        assert ratelimits.total == 0

    @pytest.mark.asyncio
    async def test_delete_ratelimit_not_found(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test deleting non-existent rate limit fails."""
        with pytest.raises(ResourceNotFoundError):
            await admin_service.delete_ratelimit("org-001", "user", "non-existent")


# =============================================================================
# Issue #179: Usage Timeseries and My Chats Tests
# =============================================================================


class TestAdminServiceUsageTimeseries:
    """Tests for usage timeseries operations (Issue #179)."""

    @pytest.mark.asyncio
    async def test_get_usage_timeseries_empty(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test getting usage timeseries with no data."""
        result = await admin_service.get_usage_timeseries(
            org_id="org-001",
            period="daily",
            start_date="2026-02-19",
            end_date="2026-02-20",
        )

        # Should return empty list with dates filled in
        assert isinstance(result, list)
        assert len(result) == 2  # 2 days
        assert result[0]["date"] == "2026-02-19"
        assert result[0]["input_tokens"] == 0
        assert result[0]["request_count"] == 0

    @pytest.mark.asyncio
    async def test_get_usage_timeseries_with_data(self, admin_service: AdminService, sample_organizations: list[Organization], db_session):
        """Test getting usage timeseries with actual usage data."""
        from datetime import UTC, datetime
        from decimal import Decimal

        from src.shared.models.usage import UsageLog

        # Create some usage logs with timezone-aware timestamps
        log1 = UsageLog(
            id="log-1",
            org_id="org-001",
            user_id="user-1",
            department_id="dept-1",
            team_id="team-1",
            model="claude-3-opus",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=Decimal("0.10"),
            latency_ms=2000,
            status_code=200,
            timestamp=datetime(2026, 2, 19, 10, 0, 0, tzinfo=UTC),
        )
        log2 = UsageLog(
            id="log-2",
            org_id="org-001",
            user_id="user-1",
            department_id="dept-1",
            team_id="team-1",
            model="claude-3-opus",
            input_tokens=2000,
            output_tokens=1000,
            cost_usd=Decimal("0.20"),
            latency_ms=3000,
            status_code=200,
            timestamp=datetime(2026, 2, 19, 14, 0, 0, tzinfo=UTC),
        )
        db_session.add_all([log1, log2])
        await db_session.commit()

        result = await admin_service.get_usage_timeseries(
            org_id="org-001",
            period="daily",
            start_date="2026-02-19",
            end_date="2026-02-19",
        )

        assert len(result) == 1
        assert result[0]["date"] == "2026-02-19"
        assert result[0]["input_tokens"] == 3000  # 1000 + 2000
        assert result[0]["output_tokens"] == 1500  # 500 + 1000
        assert result[0]["request_count"] == 2


class TestAdminServiceMyChats:
    """Tests for my chats operations (Issue #179)."""

    @pytest.mark.asyncio
    async def test_get_user_chats_empty(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test getting chats with no data."""
        chats, total = await admin_service.get_user_chats(
            user_id="user-1",
            org_id="org-001",
        )

        assert len(chats) == 0
        assert total == 0

    @pytest.mark.asyncio
    async def test_get_user_chats_with_data(self, admin_service: AdminService, sample_organizations: list[Organization], db_session):
        """Test getting user chats with actual usage data."""
        from datetime import UTC, datetime
        from decimal import Decimal

        from src.shared.models.usage import UsageLog

        # Create some usage logs for the user
        log1 = UsageLog(
            id="log-1",
            request_id="req-1",
            org_id="org-001",
            user_id="user-1",
            department_id="dept-1",
            team_id="team-1",
            model="claude-3-opus",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=Decimal("0.10"),
            latency_ms=2000,
            status_code=200,
            timestamp=datetime(2026, 2, 19, 10, 0, 0, tzinfo=UTC),
        )
        log2 = UsageLog(
            id="log-2",
            request_id="req-2",
            org_id="org-001",
            user_id="user-1",
            department_id="dept-1",
            team_id="team-1",
            model="claude-3-sonnet",
            input_tokens=500,
            output_tokens=200,
            cost_usd=Decimal("0.05"),
            latency_ms=1500,
            status_code=200,
            timestamp=datetime(2026, 2, 20, 14, 0, 0, tzinfo=UTC),
        )
        db_session.add_all([log1, log2])
        await db_session.commit()

        chats, total = await admin_service.get_user_chats(
            user_id="user-1",
            org_id="org-001",
        )

        assert total == 2
        assert len(chats) == 2
        # Should be ordered by newest first
        assert chats[0]["request_id"] == "req-2"
        assert chats[1]["request_id"] == "req-1"

    @pytest.mark.asyncio
    async def test_get_user_chats_filters_by_user(self, admin_service: AdminService, sample_organizations: list[Organization], db_session):
        """Test that get_user_chats only returns the user's own chats."""
        from decimal import Decimal

        from src.shared.models.usage import UsageLog

        # Create logs for different users
        log1 = UsageLog(
            id="log-1",
            org_id="org-001",
            user_id="user-1",
            department_id="dept-1",
            team_id="team-1",
            model="claude-3-opus",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=Decimal("0.10"),
            latency_ms=2000,
            status_code=200,
        )
        log2 = UsageLog(
            id="log-2",
            org_id="org-001",
            user_id="user-2",  # Different user
            department_id="dept-1",
            team_id="team-1",
            model="claude-3-sonnet",
            input_tokens=500,
            output_tokens=200,
            cost_usd=Decimal("0.05"),
            latency_ms=1500,
            status_code=200,
        )
        db_session.add_all([log1, log2])
        await db_session.commit()

        chats, total = await admin_service.get_user_chats(
            user_id="user-1",
            org_id="org-001",
        )

        assert total == 1
        assert len(chats) == 1
        assert chats[0]["request_id"] == "log-1"

    @pytest.mark.asyncio
    async def test_get_user_chats_with_model_filter(self, admin_service: AdminService, sample_organizations: list[Organization], db_session):
        """Test filtering chats by model name."""
        from decimal import Decimal

        from src.shared.models.usage import UsageLog

        # Create logs with different models
        log1 = UsageLog(
            id="log-1",
            org_id="org-001",
            user_id="user-1",
            department_id="dept-1",
            team_id="team-1",
            model="claude-3-opus",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=Decimal("0.10"),
            latency_ms=2000,
            status_code=200,
        )
        log2 = UsageLog(
            id="log-2",
            org_id="org-001",
            user_id="user-1",
            department_id="dept-1",
            team_id="team-1",
            model="claude-3-sonnet",
            input_tokens=500,
            output_tokens=200,
            cost_usd=Decimal("0.05"),
            latency_ms=1500,
            status_code=200,
        )
        db_session.add_all([log1, log2])
        await db_session.commit()

        chats, total = await admin_service.get_user_chats(
            user_id="user-1",
            org_id="org-001",
            model_filter="opus",
        )

        assert total == 1
        assert chats[0]["model"] == "claude-3-opus"

    @pytest.mark.asyncio
    async def test_get_chat_detail(self, admin_service: AdminService, sample_organizations: list[Organization], db_session):
        """Test getting a specific chat detail."""
        from decimal import Decimal

        from src.shared.models.usage import UsageLog

        log = UsageLog(
            id="log-1",
            request_id="req-123",
            org_id="org-001",
            user_id="user-1",
            department_id="dept-1",
            team_id="team-1",
            model="claude-3-opus",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=Decimal("0.10"),
            latency_ms=2500,
            status_code=200,
        )
        db_session.add(log)
        await db_session.commit()

        result = await admin_service.get_chat_detail(
            user_id="user-1",
            org_id="org-001",
            request_id="req-123",
        )

        assert result is not None
        assert result["request_id"] == "req-123"
        assert result["model"] == "claude-3-opus"
        assert result["input_tokens"] == 1000
        assert result["latency_ms"] == 2500
        assert result["chat_logging_available"] is False

    @pytest.mark.asyncio
    async def test_get_chat_detail_not_found(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test getting non-existent chat detail returns None."""
        result = await admin_service.get_chat_detail(
            user_id="user-1",
            org_id="org-001",
            request_id="non-existent",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_get_chat_detail_wrong_user(self, admin_service: AdminService, sample_organizations: list[Organization], db_session):
        """Test that users cannot access other users' chat details."""
        from decimal import Decimal

        from src.shared.models.usage import UsageLog

        log = UsageLog(
            id="log-1",
            request_id="req-123",
            org_id="org-001",
            user_id="user-1",
            department_id="dept-1",
            team_id="team-1",
            model="claude-3-opus",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=Decimal("0.10"),
            latency_ms=2500,
            status_code=200,
        )
        db_session.add(log)
        await db_session.commit()

        # Try to access as a different user
        result = await admin_service.get_chat_detail(
            user_id="user-2",  # Different user
            org_id="org-001",
            request_id="req-123",
        )

        assert result is None  # Should not find it
