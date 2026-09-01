"""Unit tests for AdminService."""

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.exceptions import PoolConfigurationError, ResourceConflictError, ResourceNotFoundError
from src.admin.schemas import (
    OrganizationCreateRequest,
    OrganizationUpdateRequest,
    PoolAccountCreateRequest,
    RateLimitConfigUpdateRequest,
)
from src.admin.service import AdminService
from src.shared.identity import UnresolvableUserEntityError
from src.shared.models.organization import Organization, User
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
    async def test_update_organization_member_approval_policy(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Test updating member_approval_policy.

        Issue #2984: Approval policy toggle persists correctly.
        """
        # Default is auto_approve_org_members
        org = await admin_service.get_organization("org-001")
        assert org.member_approval_policy == "auto_approve_org_members"

        # Toggle to require_admin_approval
        request = OrganizationUpdateRequest(member_approval_policy="require_admin_approval")
        result = await admin_service.update_organization("org-001", request)
        assert result.member_approval_policy == "require_admin_approval"

        # Toggle back to auto_approve_org_members
        request = OrganizationUpdateRequest(member_approval_policy="auto_approve_org_members")
        result = await admin_service.update_organization("org-001", request)
        assert result.member_approval_policy == "auto_approve_org_members"

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
    async def test_get_budgets_list_filter_by_entity_type(
        self, admin_service: AdminService, db_session: AsyncSession, sample_organizations: list[Organization]
    ):
        """Test filtering budget list by entity type."""
        from decimal import Decimal

        from src.admin.schemas import BudgetCreateRequest

        # Issue #4511: a `user` budget now requires a resolvable member, so this
        # needs a real users row rather than a fabricated id. The row's
        # cognito_sub is what gets persisted as the budget's entity_id.
        db_session.add(
            User(
                id="user-1",
                org_id="org-001",
                team_id="team-001",
                email="user1@test.com",
                cognito_sub="sub-user-1",
            )
        )
        await db_session.commit()

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


# Issue #4511: user-scoped budgets must be keyed on the Cognito sub, because that
# is the key enforcement and the /api/me/budget read path match on. A budget
# keyed on anything else is inert: visible in Budget Management, invisible to its
# owner, and never enforced.


class TestUserBudgetKeyResolution:
    """Tests that `user` budget keys are resolved to the Cognito sub (#4511)."""

    GITHUB_USER_ID = "20402445"
    COGNITO_SUB = "8a41f2c0-1b7d-4e5a-9c33-000000000001"
    CANONICAL_ID = "user-operator"

    @pytest.fixture
    async def github_member(self, db_session: AsyncSession, sample_organizations: list[Organization]) -> User:
        """A GitHub-onboarded member of org-001: sub set, no cognito_username.

        This is the shape that produced the incident — the broker mints a
        Cognito Username of `GitHub_<github_id>` and never populates
        `users.cognito_username`.
        """
        from src.shared.models.vault import UserIdentity

        user = User(
            id=self.CANONICAL_ID,
            org_id="org-001",
            team_id="team-001",
            email="operator@test.com",
            name="Operator",
            cognito_sub=self.COGNITO_SUB,
        )
        db_session.add(user)
        await db_session.flush()
        db_session.add(
            UserIdentity(
                id="identity-operator",
                org_id="org-001",
                team_id="team-001",
                user_id=self.CANONICAL_ID,
                provider="github",
                provider_user_id=self.GITHUB_USER_ID,
                provider_username="operator",
                verification_method="oauth",
            )
        )
        await db_session.commit()
        return user

    def _request(self, entity_id: str):
        from decimal import Decimal

        from src.admin.schemas import BudgetCreateRequest

        return BudgetCreateRequest(
            entity_type="user",
            entity_id=entity_id,
            period_type="monthly",
            budget_amount_usd=Decimal("100.00"),
            enforcement_mode="hard",
        )

    @pytest.mark.asyncio
    async def test_create_with_sub_persists_unchanged(self, admin_service: AdminService, github_member: User):
        """A sub is already the right key, so it is persisted as supplied."""
        result = await admin_service.create_budget("org-001", self._request(self.COGNITO_SUB))
        assert result.entity_id == self.COGNITO_SUB

    @pytest.mark.asyncio
    async def test_create_with_canonical_id_persists_sub(self, admin_service: AdminService, github_member: User):
        """A canonical users.id is resolved before persisting."""
        result = await admin_service.create_budget("org-001", self._request(self.CANONICAL_ID))
        assert result.entity_id == self.COGNITO_SUB

    @pytest.mark.asyncio
    async def test_create_with_cognito_username_persists_sub(self, admin_service: AdminService, github_member: User):
        """The exact incident input: `GitHub_<id>` must land as the sub.

        Before #4511 this persisted verbatim, producing a row that Budget
        Management displayed with a friendly name and that nothing enforced.
        """
        result = await admin_service.create_budget("org-001", self._request(f"GitHub_{self.GITHUB_USER_ID}"))
        assert result.entity_id == self.COGNITO_SUB

    @pytest.mark.asyncio
    async def test_create_with_lowercase_github_prefix_persists_sub(self, admin_service: AdminService, github_member: User):
        """Prefix matching is case-insensitive (the broker writes capital G)."""
        result = await admin_service.create_budget("org-001", self._request(f"github_{self.GITHUB_USER_ID}"))
        assert result.entity_id == self.COGNITO_SUB

    @pytest.mark.asyncio
    async def test_create_with_email_rejected_and_persists_nothing(self, admin_service: AdminService, github_member: User):
        """Email is not a key (no uniqueness constraint) — 422, nothing written."""
        with pytest.raises(UnresolvableUserEntityError) as exc:
            await admin_service.create_budget("org-001", self._request("operator@test.com"))
        assert exc.value.status_code == 422

        budgets = await admin_service.get_budgets_list("org-001")
        assert budgets.total == 0

    @pytest.mark.asyncio
    async def test_create_with_unmappable_id_rejected(self, admin_service: AdminService, github_member: User):
        """An id that matches nothing is refused rather than silently persisted."""
        with pytest.raises(UnresolvableUserEntityError):
            await admin_service.create_budget("org-001", self._request("user-123"))

        budgets = await admin_service.get_budgets_list("org-001")
        assert budgets.total == 0

    @pytest.mark.asyncio
    async def test_create_for_user_without_sub_rejected(
        self, admin_service: AdminService, db_session: AsyncSession, sample_organizations: list[Organization]
    ):
        """A member who has never signed in cannot be given an enforceable cap.

        This is the case that would recreate the bug: persisting here yields a
        budget nothing can ever match.
        """
        db_session.add(
            User(
                id="user-invited",
                org_id="org-001",
                team_id="team-001",
                email="invited@test.com",
                cognito_sub=None,
            )
        )
        await db_session.commit()

        with pytest.raises(UnresolvableUserEntityError):
            await admin_service.create_budget("org-001", self._request("user-invited"))

    @pytest.mark.asyncio
    async def test_create_does_not_resolve_across_tenants(self, admin_service: AdminService, github_member: User):
        """org-001's member must not resolve when creating a budget in org-002."""
        with pytest.raises(UnresolvableUserEntityError):
            await admin_service.create_budget("org-002", self._request(f"GitHub_{self.GITHUB_USER_ID}"))

    @pytest.mark.asyncio
    async def test_non_user_entity_types_are_untouched(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """Regression guard: org/team/department ids bypass resolution entirely."""
        from decimal import Decimal

        from src.admin.schemas import BudgetCreateRequest

        for entity_type, entity_id in (("org", "org-001"), ("team", "platform-team"), ("department", "engineering")):
            result = await admin_service.create_budget(
                "org-001",
                BudgetCreateRequest(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    period_type="monthly",
                    budget_amount_usd=Decimal("50.00"),
                    enforcement_mode="hard",
                ),
            )
            assert result.entity_id == entity_id

    @pytest.mark.asyncio
    async def test_username_and_sub_collide_as_duplicates(self, admin_service: AdminService, github_member: User):
        """Two spellings of one person are one budget, not two.

        Resolution happens before the conflict probe, so creating with the
        username after creating with the sub is a 409 — not a second row that
        would violate uq_budget_config.
        """
        await admin_service.create_budget("org-001", self._request(self.COGNITO_SUB))
        with pytest.raises(ResourceConflictError):
            await admin_service.create_budget("org-001", self._request(f"GitHub_{self.GITHUB_USER_ID}"))

    @pytest.mark.asyncio
    async def test_create_key_matches_enforcement_read_key(self, admin_service: AdminService, github_member: User):
        """Guard test (I6): the create key and the read key derive identically.

        `_read_cap` (src/budget/me_routes.py) is the query the owner's Budget &
        Spend page runs, keyed on `(EntityType.USER, context.user_id)` where
        `context.user_id` is the Cognito sub. This test drives the real read path
        against a budget created through the real create path: if a future change
        lets create persist a non-sub key, this fails rather than shipping
        another inert cap. That closes the class, not just the instance.
        """
        from src.budget.me_routes import _read_cap
        from src.shared.schemas.budget import EntityType, PeriodType

        # Create the way the UI does — using the Cognito username, the form that
        # caused the incident.
        await admin_service.create_budget("org-001", self._request(f"GitHub_{self.GITHUB_USER_ID}"))

        # Read the way the owner's own page does — keyed on their token's sub.
        cap = await _read_cap(
            admin_service.db,
            "org-001",
            EntityType.USER,
            self.COGNITO_SUB,
            PeriodType.MONTHLY,
        )

        assert cap is not None, "budget created via the admin path is invisible to the owner's read path"
        assert cap.entity_id == self.COGNITO_SUB


class TestUpdateBudgetConfigMiss:
    """Update-path fail-open fixes (#4511 I1): 404 on miss, same resolution."""

    @pytest.mark.asyncio
    async def test_update_nonexistent_budget_raises_404(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """A miss must be a 404, not HTTP 200 with a null body.

        Previously every miss fell through to `return None`, which the route
        rendered as a 200 — an operator's edit silently did nothing.
        """
        from decimal import Decimal

        from src.admin.schemas import BudgetConfigUpdateRequest

        with pytest.raises(ResourceNotFoundError):
            await admin_service.update_budget_config(
                "org-001",
                "team",
                "no-such-team",
                BudgetConfigUpdateRequest(budget_amount_usd=Decimal("10.00"), enforcement_mode="hard"),
            )

    @pytest.mark.asyncio
    async def test_update_unknown_entity_type_raises_404(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """An unparseable entity type is a miss, not a silent 200."""
        from decimal import Decimal

        from src.admin.schemas import BudgetConfigUpdateRequest

        with pytest.raises(ResourceNotFoundError):
            await admin_service.update_budget_config(
                "org-001",
                "not-an-entity-type",
                "whatever",
                BudgetConfigUpdateRequest(budget_amount_usd=Decimal("10.00"), enforcement_mode="hard"),
            )

    @pytest.mark.asyncio
    async def test_update_resolves_user_entity_id_from_path(
        self, admin_service: AdminService, db_session: AsyncSession, sample_organizations: list[Organization]
    ):
        """The path param gets the same resolution as the create body.

        Without this, editing a budget re-introduces a mis-keyed lookup: the
        operator's `GitHub_<id>` would miss the sub-keyed row they meant to edit.
        """
        from decimal import Decimal

        from src.admin.schemas import BudgetConfigUpdateRequest
        from src.shared.models.vault import UserIdentity

        sub = "8a41f2c0-1b7d-4e5a-9c33-000000000009"
        db_session.add(
            User(id="user-upd", org_id="org-001", team_id="team-001", email="upd@test.com", cognito_sub=sub),
        )
        await db_session.flush()
        db_session.add(
            UserIdentity(
                id="identity-upd",
                org_id="org-001",
                team_id="team-001",
                user_id="user-upd",
                provider="github",
                provider_user_id="777",
                provider_username="upd",
                verification_method="oauth",
            )
        )
        await db_session.commit()

        # The budget service is mocked in this fixture, so assert on the id the
        # service looked the budget up by — that is the behaviour under test.
        with pytest.raises(ResourceNotFoundError):
            await admin_service.update_budget_config(
                "org-001",
                "user",
                "GitHub_777",
                BudgetConfigUpdateRequest(budget_amount_usd=Decimal("10.00"), enforcement_mode="hard"),
            )

        lookup_entity_id = admin_service.budget_service.get_budgets_for_entity.await_args.args[1]
        assert lookup_entity_id == sub, "update looked the budget up by an id enforcement never uses"

    @pytest.mark.asyncio
    async def test_update_with_unresolvable_user_raises_422(self, admin_service: AdminService, sample_organizations: list[Organization]):
        """An unresolvable user id on the update path is a 422, not a 404."""
        from decimal import Decimal

        from src.admin.schemas import BudgetConfigUpdateRequest

        with pytest.raises(UnresolvableUserEntityError):
            await admin_service.update_budget_config(
                "org-001",
                "user",
                "someone@example.com",
                BudgetConfigUpdateRequest(budget_amount_usd=Decimal("10.00"), enforcement_mode="hard"),
            )


# Issue #4536: cloud-agent (`root_user`) budgets are keyed on the canonical
# `users.id` — what the usage tracker writes (#4300) and what the /api/me/budget
# read path derives. This is #4511's invariant one ledger over: the same accepted
# input forms, resolved to a DIFFERENT key, because the two person-scoped ledgers
# live in different id namespaces.


class TestRootUserBudgetKeyResolution:
    """Tests that `root_user` budget keys resolve to the canonical id (#4536)."""

    GITHUB_USER_ID = "31513556"
    COGNITO_SUB = "8a41f2c0-1b7d-4e5a-9c33-000000000042"
    CANONICAL_ID = "user-cloud-operator"

    @pytest.fixture
    async def github_member(self, db_session: AsyncSession, sample_organizations: list[Organization]) -> User:
        """A GitHub-onboarded member of org-001, reachable by all three forms."""
        from src.shared.models.vault import UserIdentity

        user = User(
            id=self.CANONICAL_ID,
            org_id="org-001",
            team_id="team-001",
            email="cloud@test.com",
            name="Cloud Operator",
            cognito_sub=self.COGNITO_SUB,
        )
        db_session.add(user)
        await db_session.flush()
        db_session.add(
            UserIdentity(
                id="identity-cloud-operator",
                org_id="org-001",
                team_id="team-001",
                user_id=self.CANONICAL_ID,
                provider="github",
                provider_user_id=self.GITHUB_USER_ID,
                provider_username="cloud-operator",
                verification_method="oauth",
            )
        )
        await db_session.commit()
        return user

    def _request(self, entity_id: str):
        from decimal import Decimal

        from src.admin.schemas import BudgetCreateRequest

        return BudgetCreateRequest(
            entity_type="root_user",
            entity_id=entity_id,
            period_type="monthly",
            budget_amount_usd=Decimal("250.00"),
            enforcement_mode="hard",
        )

    @pytest.mark.asyncio
    async def test_root_user_is_an_accepted_entity_type(self, admin_service: AdminService, github_member: User):
        """The gap the issue reports: the API refused this type outright.

        `BudgetCreateRequest.entity_type` was a Literal without `root_user`, so a
        cloud-agent cap could not be created at all — not even by API call.
        """
        result = await admin_service.create_budget("org-001", self._request(self.CANONICAL_ID))
        assert result.entity_type == "root_user"

    @pytest.mark.asyncio
    async def test_create_with_canonical_id_persists_unchanged(self, admin_service: AdminService, github_member: User):
        """The canonical id is already the key, so it round-trips untouched.

        This is what the person picker submits for a cloud-agent budget.
        """
        result = await admin_service.create_budget("org-001", self._request(self.CANONICAL_ID))
        assert result.entity_id == self.CANONICAL_ID

    @pytest.mark.asyncio
    async def test_create_with_sub_resolves_to_canonical_id(self, admin_service: AdminService, github_member: User):
        """A sub must NOT be persisted here — it is the other ledger's key.

        Persisting it would be #4511 recreated one ledger over: a cap that exists
        and matches nothing the tracker ever writes.
        """
        result = await admin_service.create_budget("org-001", self._request(self.COGNITO_SUB))
        assert result.entity_id == self.CANONICAL_ID

    @pytest.mark.asyncio
    async def test_create_with_cognito_username_resolves_to_canonical_id(self, admin_service: AdminService, github_member: User):
        """`GitHub_<id>` — the form the broker mints — resolves."""
        result = await admin_service.create_budget("org-001", self._request(f"GitHub_{self.GITHUB_USER_ID}"))
        assert result.entity_id == self.CANONICAL_ID

    @pytest.mark.asyncio
    async def test_create_with_lowercase_github_prefix_resolves(self, admin_service: AdminService, github_member: User):
        """Prefix matching is case-insensitive, as on the direct-use path."""
        result = await admin_service.create_budget("org-001", self._request(f"github_{self.GITHUB_USER_ID}"))
        assert result.entity_id == self.CANONICAL_ID

    @pytest.mark.asyncio
    async def test_create_for_member_without_sub_is_allowed(
        self, admin_service: AdminService, db_session: AsyncSession, sample_organizations: list[Organization]
    ):
        """The one asymmetry with direct-use: a never-signed-in member IS cappable.

        `user` budgets 422 for this person (no sub for enforcement to match), but
        their cloud spend is attributed from the run's lineage, so the canonical id
        is a real enforceable key. Refusing here would deny a working budget.
        """
        db_session.add(
            User(
                id="user-cloud-invited",
                org_id="org-001",
                team_id="team-001",
                email="cloud-invited@test.com",
                cognito_sub=None,
            )
        )
        await db_session.commit()

        result = await admin_service.create_budget("org-001", self._request("user-cloud-invited"))
        assert result.entity_id == "user-cloud-invited"

    @pytest.mark.asyncio
    async def test_create_with_service_principal_passes_through(self, admin_service: AdminService, github_member: User):
        """`service:` root principals are already canonical keys (#4344).

        Not offered in the form, but the API accepts them unmodified so an
        unattended CI/EventBridge trigger can still be given a ceiling.
        """
        result = await admin_service.create_budget("org-001", self._request("service:eventbridge:adp-dev-nightly"))
        assert result.entity_id == "service:eventbridge:adp-dev-nightly"

    @pytest.mark.asyncio
    async def test_create_with_email_rejected_and_persists_nothing(self, admin_service: AdminService, github_member: User):
        """Email is not a key (no uniqueness constraint) — 422, nothing written."""
        with pytest.raises(UnresolvableUserEntityError) as exc:
            await admin_service.create_budget("org-001", self._request("cloud@test.com"))
        assert exc.value.status_code == 422

        budgets = await admin_service.get_budgets_list("org-001")
        assert budgets.total == 0

    @pytest.mark.asyncio
    async def test_create_does_not_resolve_across_tenants(self, admin_service: AdminService, github_member: User):
        """org-001's member must not resolve when creating a budget in org-002.

        Cross-tenant keying is the blast radius the issue calls out by name.
        """
        with pytest.raises(UnresolvableUserEntityError):
            await admin_service.create_budget("org-002", self._request(f"GitHub_{self.GITHUB_USER_ID}"))

    @pytest.mark.asyncio
    async def test_sub_and_canonical_id_collide_as_duplicates(self, admin_service: AdminService, github_member: User):
        """Two spellings of one person are one budget, not two.

        Resolution runs before the conflict probe, so the second create is a 409
        rather than a second row violating uq_budget_config.
        """
        await admin_service.create_budget("org-001", self._request(self.CANONICAL_ID))
        with pytest.raises(ResourceConflictError):
            await admin_service.create_budget("org-001", self._request(self.COGNITO_SUB))

    @pytest.mark.asyncio
    async def test_direct_use_and_cloud_agent_budgets_coexist(self, admin_service: AdminService, github_member: User):
        """One person can hold both caps, on separate rows with separate keys.

        The whole point of the second entity type: capping direct use leaves cloud
        spend unbounded and vice versa, so both must be creatable for one person
        without colliding.
        """
        from decimal import Decimal

        from src.admin.schemas import BudgetCreateRequest

        cloud = await admin_service.create_budget("org-001", self._request(self.CANONICAL_ID))
        direct = await admin_service.create_budget(
            "org-001",
            BudgetCreateRequest(
                entity_type="user",
                entity_id=self.CANONICAL_ID,
                period_type="monthly",
                budget_amount_usd=Decimal("100.00"),
                enforcement_mode="hard",
            ),
        )

        assert cloud.entity_id == self.CANONICAL_ID
        assert direct.entity_id == self.COGNITO_SUB, "direct-use must still key on the sub (#4511)"
        assert (await admin_service.get_budgets_list("org-001")).total == 2

    @pytest.mark.asyncio
    async def test_update_path_applies_same_resolution(self, admin_service: AdminService, github_member: User):
        """The path param gets the same resolution as the create body.

        Without it, editing a cloud-agent budget by sub would miss the
        canonical-id-keyed row the operator meant to edit.
        """
        from decimal import Decimal

        from src.admin.schemas import BudgetConfigUpdateRequest

        # The budget service is mocked in this fixture, so assert on the id the
        # service looked the budget up by — that is the behaviour under test.
        with pytest.raises(ResourceNotFoundError):
            await admin_service.update_budget_config(
                "org-001",
                "root_user",
                self.COGNITO_SUB,
                BudgetConfigUpdateRequest(budget_amount_usd=Decimal("10.00"), enforcement_mode="hard"),
            )

        lookup_entity_id = admin_service.budget_service.get_budgets_for_entity.await_args.args[1]
        assert lookup_entity_id == self.CANONICAL_ID, "update looked the budget up by an id the cloud ledger never uses"

    @pytest.mark.asyncio
    async def test_update_with_unresolvable_id_raises_422(self, admin_service: AdminService, github_member: User):
        """An unresolvable id on the update path is a 422, not a 404."""
        from decimal import Decimal

        from src.admin.schemas import BudgetConfigUpdateRequest

        with pytest.raises(UnresolvableUserEntityError):
            await admin_service.update_budget_config(
                "org-001",
                "root_user",
                "cloud@test.com",
                BudgetConfigUpdateRequest(budget_amount_usd=Decimal("10.00"), enforcement_mode="hard"),
            )

    @pytest.mark.asyncio
    async def test_create_key_matches_dashboard_read_key(self, admin_service: AdminService, github_member: User):
        """Guard test (I6, one ledger over): create key == dashboard read key.

        The dashboard's cloud-agent line is read by resolving the caller's Cognito
        sub to their canonical id (`me_routes._resolve_root_principal`) and querying
        `root_user` on it (`_read_cap`). This drives BOTH real paths: create through
        the admin service using the incident-shaped input, then read through the
        owner's own derivation. If a future change lets create persist a non-canonical
        key, this fails rather than shipping another inert cap.
        """
        from src.budget.me_routes import _read_cap, _resolve_root_principal
        from src.shared.schemas.budget import EntityType, PeriodType

        # Create the way the UI does — via a form-supplied identity form.
        await admin_service.create_budget("org-001", self._request(f"GitHub_{self.GITHUB_USER_ID}"))

        # Derive the read key the way the owner's own page does: from their token's
        # Cognito sub, through the same resolver the read path uses.
        context = SimpleNamespace(
            account_type="user",
            user_id=self.COGNITO_SUB,
            org_id="org-001",
        )
        canonical_user_id, identity_status = await _resolve_root_principal(admin_service.db, context)

        assert identity_status == "resolved"
        cap = await _read_cap(
            admin_service.db,
            "org-001",
            EntityType.ROOT_USER,
            canonical_user_id,
            PeriodType.MONTHLY,
        )

        assert cap is not None, "cloud-agent budget created via the admin path is invisible to the owner's read path"
        assert cap.entity_id == self.CANONICAL_ID

    @pytest.mark.asyncio
    async def test_list_resolves_a_display_name_for_canonical_keys(self, admin_service: AdminService, github_member: User):
        """A cloud-agent row must render as a person, not a bare UUID.

        The Cognito batch lookup used for `user` rows is keyed by sub and cannot
        name these, so without a `users`-backed lookup every cloud-agent budget
        would list as an opaque id.
        """
        await admin_service.create_budget("org-001", self._request(self.CANONICAL_ID))

        listed = await admin_service.get_budgets_list("org-001")
        row = next(item for item in listed.items if item.entity_type == "root_user")
        assert row.entity_display_name == "Cloud Operator"

    @pytest.mark.asyncio
    async def test_list_leaves_service_principal_rows_unnamed(self, admin_service: AdminService, github_member: User):
        """A `service:` row has no person to name, so no name is invented."""
        await admin_service.create_budget("org-001", self._request("service:ci:nightly"))

        listed = await admin_service.get_budgets_list("org-001")
        row = next(item for item in listed.items if item.entity_type == "root_user")
        assert row.entity_display_name is None


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
