"""
Unit tests for the Tenant Resolver module.

These tests cover AWS identity mapping to internal tenant structures,
including organization resolution, service account handling, and user resolution.
"""

from unittest.mock import patch

import pytest

from src.auth.exceptions import TenantResolutionError
from src.auth.schemas import AWSCallerIdentity, TenantInfo
from src.auth.tenant_resolver import TenantResolver
from src.shared.exceptions import UnknownOrganizationError, UnregisteredServiceAccountError
from src.shared.models.organization import Organization, User


@pytest.mark.unit
class TestTenantResolver:
    """Test suite for TenantResolver."""

    def test_init(self):
        """Test TenantResolver initialization."""
        resolver = TenantResolver()
        assert resolver is not None

    @pytest.mark.asyncio
    async def test_resolve_tenant_service_account_success(
        self, db_session, tenant_resolver: TenantResolver, sample_organization: Organization, sample_service_account, sample_department, sample_team
    ):
        """Test successful service account tenant resolution."""
        caller_identity = AWSCallerIdentity(
            user_id="AIDACKCEVSQ6C2EXAMPLE", account="123456789012", arn="arn:aws:sts::123456789012:assumed-role/ml-training-service/test-session"
        )

        tenant_info = await tenant_resolver.resolve_tenant(caller_identity, db_session)

        assert isinstance(tenant_info, TenantInfo)
        assert tenant_info.org_id == sample_organization.id
        assert tenant_info.org_name == sample_organization.name
        assert tenant_info.account_type == "service"
        assert tenant_info.entity_id == sample_service_account.id
        assert tenant_info.department_id == sample_service_account.department_id
        assert tenant_info.team_id == sample_service_account.team_id

    @pytest.mark.asyncio
    async def test_resolve_tenant_unknown_organization(self, db_session, tenant_resolver: TenantResolver):
        """Test tenant resolution with unknown organization."""
        # Use an account ID that doesn't exist in any organization
        caller_identity = AWSCallerIdentity(
            user_id="AIDACKCEVSQ6C2EXAMPLE",
            account="999999999999",  # Unknown account
            arn="arn:aws:sts::999999999999:assumed-role/some-role/session",
        )

        with pytest.raises(UnknownOrganizationError) as exc_info:
            await tenant_resolver.resolve_tenant(caller_identity, db_session)

        assert "999999999999" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_resolve_tenant_unregistered_service_account(self, db_session, tenant_resolver: TenantResolver, sample_organization: Organization):
        """Test tenant resolution with unregistered service account."""
        caller_identity = AWSCallerIdentity(
            user_id="AIDACKCEVSQ6C2EXAMPLE", account="123456789012", arn="arn:aws:sts::123456789012:assumed-role/unregistered-role/session"
        )

        with pytest.raises(UnregisteredServiceAccountError) as exc_info:
            await tenant_resolver.resolve_tenant(caller_identity, db_session)

        assert "unregistered-role" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_resolve_tenant_human_user_success(
        self, db_session, tenant_resolver: TenantResolver, sample_organization: Organization, sample_user: User, sample_department, sample_team
    ):
        """Test successful human user tenant resolution."""
        caller_identity = AWSCallerIdentity(
            user_id="AIDACKCEVSQ6C2EXAMPLE",
            account="123456789012",
            arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Developer/john.doe@test.com",
        )

        tenant_info = await tenant_resolver.resolve_tenant(caller_identity, db_session)

        assert isinstance(tenant_info, TenantInfo)
        assert tenant_info.org_id == sample_organization.id
        assert tenant_info.account_type == "human"
        assert tenant_info.entity_id == sample_user.id

    @pytest.mark.asyncio
    async def test_resolve_tenant_human_user_not_found(
        self, db_session, tenant_resolver: TenantResolver, sample_organization: Organization, sample_department, sample_team
    ):
        """Test human user resolution when user not found in database."""
        caller_identity = AWSCallerIdentity(
            user_id="AIDACKCEVSQ6C2EXAMPLE",
            account="123456789012",
            arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Developer/unknown.user@test.com",
        )

        # Mock the _get_or_create_default_team to avoid complexity
        with patch.object(tenant_resolver, "_get_or_create_default_team", side_effect=TenantResolutionError("No default team")):
            with pytest.raises(TenantResolutionError) as exc_info:
                await tenant_resolver.resolve_tenant(caller_identity, db_session)

            assert "No default team" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_resolve_tenant_database_error(self, db_session, tenant_resolver: TenantResolver):
        """Test tenant resolution with database error."""
        caller_identity = AWSCallerIdentity(
            user_id="AIDACKCEVSQ6C2EXAMPLE", account="123456789012", arn="arn:aws:sts::123456789012:assumed-role/test-role/session"
        )

        # Mock database error
        with patch.object(db_session, "execute", side_effect=Exception("Database error")):
            with pytest.raises(TenantResolutionError) as exc_info:
                await tenant_resolver.resolve_tenant(caller_identity, db_session)

            assert "Tenant resolution failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_find_organization_by_account_success(self, db_session, tenant_resolver: TenantResolver, sample_organization: Organization):
        """Test finding organization by AWS account ID."""
        org = await tenant_resolver._find_organization_by_account("123456789012", db_session)

        assert org is not None
        assert org.id == sample_organization.id
        assert org.name == sample_organization.name

    @pytest.mark.asyncio
    async def test_find_organization_by_account_not_found(self, db_session, tenant_resolver: TenantResolver):
        """Test finding organization with unknown account ID."""
        org = await tenant_resolver._find_organization_by_account("999999999999", db_session)

        assert org is None

    def test_is_service_account_role(self, tenant_resolver: TenantResolver):
        """Test identifying service account ARNs."""
        # Service account ARNs (roles)
        assert tenant_resolver._is_service_account("arn:aws:sts::123456789012:assumed-role/service-role/session") is True
        assert tenant_resolver._is_service_account("arn:aws:iam::123456789012:role/service-role") is True

        # Human user ARNs
        assert tenant_resolver._is_service_account("arn:aws:iam::123456789012:user/username") is False
        assert tenant_resolver._is_service_account("arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Developer/user") is False

    def test_extract_role_arn_from_assumed_role(self, tenant_resolver: TenantResolver):
        """Test extracting role ARN from assumed role ARN."""
        assumed_role_arn = "arn:aws:sts::123456789012:assumed-role/TestRole/session-name"
        expected_role_arn = "arn:aws:iam::123456789012:role/TestRole"

        result = tenant_resolver._extract_role_arn(assumed_role_arn)

        assert result == expected_role_arn

    def test_extract_role_arn_from_iam_role(self, tenant_resolver: TenantResolver):
        """Test extracting role ARN from IAM role ARN (should return as-is)."""
        iam_role_arn = "arn:aws:iam::123456789012:role/TestRole"

        result = tenant_resolver._extract_role_arn(iam_role_arn)

        assert result == iam_role_arn

    def test_extract_role_arn_invalid(self, tenant_resolver: TenantResolver):
        """Test extracting role ARN from invalid ARN."""
        result = tenant_resolver._extract_role_arn("invalid-arn")

        assert result is None

    def test_extract_user_identifier_from_user_arn(self, tenant_resolver: TenantResolver):
        """Test extracting user identifier from user ARN."""
        user_arn = "arn:aws:iam::123456789012:user/john.doe"

        result = tenant_resolver._extract_user_identifier(user_arn)

        assert result == "john.doe"

    def test_extract_user_identifier_from_sso_arn(self, tenant_resolver: TenantResolver):
        """Test extracting user identifier from SSO ARN."""
        sso_arn = "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Developer/john.doe@test.com"

        result = tenant_resolver._extract_user_identifier(sso_arn)

        assert result == "john.doe@test.com"

    def test_extract_user_identifier_invalid(self, tenant_resolver: TenantResolver):
        """Test extracting user identifier from invalid ARN."""
        result = tenant_resolver._extract_user_identifier("invalid-arn")

        assert result is None

    def test_check_admin_privileges_with_admin_role(self, tenant_resolver: TenantResolver, sample_organization: Organization):
        """Test admin privilege checking with admin role."""
        # The sample organization has "AdminRole" in admin_roles
        role_arn = "arn:aws:iam::123456789012:role/AdminRole"

        result = tenant_resolver._check_admin_privileges(sample_organization, role_arn)

        assert result is True

    def test_check_admin_privileges_without_admin_role(self, tenant_resolver: TenantResolver, sample_organization: Organization):
        """Test admin privilege checking without admin role."""
        role_arn = "arn:aws:iam::123456789012:role/RegularRole"

        result = tenant_resolver._check_admin_privileges(sample_organization, role_arn)

        assert result is False

    def test_check_user_admin_privileges_with_admin_group(self, tenant_resolver: TenantResolver, sample_organization: Organization):
        """Test user admin privilege checking with admin group."""
        # The sample organization has "AdminGroup" in admin_groups
        user_arn = "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_AdminGroup/user"

        result = tenant_resolver._check_user_admin_privileges(sample_organization, user_arn)

        assert result is True

    def test_check_user_admin_privileges_without_admin_group(self, tenant_resolver: TenantResolver, sample_organization: Organization):
        """Test user admin privilege checking without admin group."""
        user_arn = "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Developer/user"

        result = tenant_resolver._check_user_admin_privileges(sample_organization, user_arn)

        assert result is False

    def test_extract_role_name(self, tenant_resolver: TenantResolver):
        """Test extracting role name from role ARN."""
        role_arn = "arn:aws:iam::123456789012:role/TestRole"

        result = tenant_resolver._extract_role_name(role_arn)

        assert result == "TestRole"

    def test_extract_role_name_invalid(self, tenant_resolver: TenantResolver):
        """Test extracting role name from invalid ARN."""
        result = tenant_resolver._extract_role_name("invalid")

        assert result is None
