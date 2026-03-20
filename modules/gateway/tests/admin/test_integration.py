"""Integration tests for Admin module."""

import pytest

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.admin.schemas import (
    OrganizationCreateRequest,
    OrganizationUpdateRequest,
    PoolAccountCreateRequest,
    RateLimitConfigUpdateRequest,
)
from src.admin.service import AdminService
from src.shared.schemas.auth import TokenContext


class TestOrganizationLifecycle:
    """Integration tests for organization lifecycle."""

    @pytest.mark.asyncio
    async def test_create_and_get_organization(self, admin_service: AdminService):
        """Test creating and retrieving an organization."""
        # Create
        request = OrganizationCreateRequest(
            name="Integration Test Org",
            aws_accounts=["123456789012"],
            settings={"test": True},
        )
        created = await admin_service.create_organization(request)

        # Get
        retrieved = await admin_service.get_organization(created.id)

        assert retrieved.name == "Integration Test Org"
        assert retrieved.aws_accounts == ["123456789012"]
        assert retrieved.settings == {"test": True}

    @pytest.mark.asyncio
    async def test_create_update_delete_organization(self, admin_service: AdminService):
        """Test full organization lifecycle."""
        # Create
        create_request = OrganizationCreateRequest(name="Lifecycle Test Org")
        created = await admin_service.create_organization(create_request)

        # Update
        update_request = OrganizationUpdateRequest(
            name="Updated Lifecycle Test Org",
            settings={"updated": True},
        )
        updated = await admin_service.update_organization(created.id, update_request)

        assert updated.name == "Updated Lifecycle Test Org"
        assert updated.settings == {"updated": True}

        # Delete
        result = await admin_service.delete_organization(created.id)
        assert result is True

        # Verify deleted
        from src.admin.exceptions import ResourceNotFoundError

        with pytest.raises(ResourceNotFoundError):
            await admin_service.get_organization(created.id)

    @pytest.mark.asyncio
    async def test_list_organizations_pagination(self, admin_service: AdminService):
        """Test organization listing with pagination."""
        # Create multiple orgs
        for i in range(5):
            await admin_service.create_organization(OrganizationCreateRequest(name=f"Pagination Test Org {i}"))

        # Test pagination
        page1, total = await admin_service.list_organizations(page=1, page_size=2)
        page2, _ = await admin_service.list_organizations(page=2, page_size=2)
        page3, _ = await admin_service.list_organizations(page=3, page_size=2)

        assert total >= 5
        assert len(page1) == 2
        assert len(page2) == 2
        assert len(page3) >= 1

        # Verify no duplicates
        all_ids = {org.id for org in page1 + page2 + page3}
        assert len(all_ids) == len(page1) + len(page2) + len(page3)


class TestPoolManagementLifecycle:
    """Integration tests for pool management lifecycle."""

    @pytest.mark.asyncio
    async def test_add_and_remove_pool_account(self, admin_service: AdminService):
        """Test adding and removing pool accounts."""
        # Add
        request = PoolAccountCreateRequest(
            account_id="999999999999",
            role_arn="arn:aws:iam::999999999999:role/IntegrationTestRole",
            region="us-east-1",
        )
        added = await admin_service.add_pool_account(request)

        # Verify in pool status
        status = await admin_service.get_pool_status()
        assert any(acc.id == added.id for acc in status.accounts)

        # Remove
        await admin_service.remove_pool_account(added.id)

        # Verify removed
        status_after = await admin_service.get_pool_status()
        assert not any(acc.id == added.id for acc in status_after.accounts)

    @pytest.mark.asyncio
    async def test_pool_status_health_tracking(self, admin_service: AdminService):
        """Test pool status health tracking."""
        # Add multiple accounts
        accounts = []
        for i in range(3):
            request = PoolAccountCreateRequest(
                account_id=f"10000000000{i}",
                role_arn=f"arn:aws:iam::10000000000{i}:role/TestRole",
                region="us-east-1",
            )
            accounts.append(await admin_service.add_pool_account(request))

        # Check status
        status = await admin_service.get_pool_status()

        assert status.total_accounts >= 3
        assert status.healthy_accounts >= 3  # New accounts are healthy

        # Cleanup
        for acc in accounts:
            await admin_service.remove_pool_account(acc.id)


class TestRateLimitConfiguration:
    """Integration tests for rate limit configuration."""

    @pytest.mark.asyncio
    async def test_create_and_update_ratelimit(self, admin_service: AdminService, sample_organizations):
        """Test rate limit configuration lifecycle."""
        org_id = sample_organizations[0].id

        # Create config
        create_request = RateLimitConfigUpdateRequest(
            rpm=100,
            tpm=10000,
            concurrent_requests=10,
        )
        created = await admin_service.update_ratelimit_config(org_id, "org", org_id, create_request)

        assert created.rpm == 100
        assert created.tpm == 10000

        # Update config
        update_request = RateLimitConfigUpdateRequest(rpm=200)
        updated = await admin_service.update_ratelimit_config(org_id, "org", org_id, update_request)

        assert updated.rpm == 200
        assert updated.tpm == 10000  # Unchanged

        # Get config
        retrieved = await admin_service.get_ratelimit_config(org_id, "org", org_id)

        assert retrieved.rpm == 200


class TestAccessControlIntegration:
    """Integration tests for access control."""

    @pytest.mark.asyncio
    async def test_platform_admin_full_access(self, access_control: AccessControl, platform_admin_context: TokenContext):
        """Test platform admin has full access."""
        # Should pass all permission checks
        await access_control.check_permission(platform_admin_context, Permission.ORG_CREATE)
        await access_control.check_permission(platform_admin_context, Permission.ORG_DELETE)
        await access_control.check_permission(platform_admin_context, Permission.POOL_MANAGE)
        await access_control.check_permission(platform_admin_context, Permission.METRICS_READ)

        # Should be able to access any org
        result = await access_control.validate_resource_access(platform_admin_context, "any-org-id")
        assert result is True

    @pytest.mark.asyncio
    async def test_org_admin_scoped_access(self, access_control: AccessControl, org_admin_context: TokenContext):
        """Test org admin has scoped access."""
        from src.admin.exceptions import AccessDeniedError, InvalidScopeError

        # Should have org-level permissions
        await access_control.check_permission(org_admin_context, Permission.ORG_READ)
        await access_control.check_permission(org_admin_context, Permission.BUDGET_UPDATE)

        # Should not have platform-level permissions
        with pytest.raises(AccessDeniedError):
            await access_control.check_permission(org_admin_context, Permission.ORG_CREATE)

        with pytest.raises(AccessDeniedError):
            await access_control.check_permission(org_admin_context, Permission.POOL_MANAGE)

        # Should not access other orgs
        with pytest.raises(InvalidScopeError):
            await access_control.check_permission(org_admin_context, Permission.ORG_READ, target_org_id="other-org")

    @pytest.mark.asyncio
    async def test_accessible_organizations_filtering(
        self,
        access_control: AccessControl,
        platform_admin_context: TokenContext,
        org_admin_context: TokenContext,
    ):
        """Test accessible organizations filtering."""
        # Platform admin - all orgs
        platform_orgs = await access_control.get_accessible_organizations(platform_admin_context)
        assert platform_orgs is None  # None means all

        # Org admin - only their org
        org_orgs = await access_control.get_accessible_organizations(org_admin_context)
        assert org_orgs == [org_admin_context.org_id]


class TestEndToEndAdminWorkflow:
    """End-to-end integration tests for admin workflows."""

    @pytest.mark.asyncio
    async def test_onboard_organization_workflow(
        self,
        admin_service: AdminService,
        access_control: AccessControl,
        platform_admin_context: TokenContext,
    ):
        """Test complete organization onboarding workflow."""
        # 1. Check permission to create org
        await access_control.check_permission(platform_admin_context, Permission.ORG_CREATE)

        # 2. Create organization
        org = await admin_service.create_organization(
            OrganizationCreateRequest(
                name="New Client Org",
                aws_accounts=["111111111111"],
                settings={"plan": "enterprise"},
            )
        )

        # 3. Configure rate limits
        await admin_service.update_ratelimit_config(
            org.id,
            "org",
            org.id,
            RateLimitConfigUpdateRequest(rpm=1000, tpm=100000, concurrent_requests=50),
        )

        # 4. Verify configuration
        config = await admin_service.get_ratelimit_config(org.id, "org", org.id)
        assert config.rpm == 1000

        # 5. Verify org in list
        orgs, _ = await admin_service.list_organizations()
        assert any(o.id == org.id for o in orgs)

        # Cleanup
        await admin_service.delete_organization(org.id)
