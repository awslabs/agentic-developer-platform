"""Unit tests for Cognito tenant resolution in TenantResolver."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.auth.schemas import AWSCallerIdentity
from src.auth.tenant_resolver import CognitoTenantInfo, TenantResolver
from src.shared.exceptions import UnknownOrganizationError
from src.shared.models.organization import Organization, User


@pytest.fixture
def tenant_resolver():
    """Create a TenantResolver instance."""
    return TenantResolver()


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    return AsyncMock()


class TestIsCognitoUser:
    """Tests for is_cognito_user method."""

    def test_gateway_caller_role_returns_true(self, tenant_resolver):
        """Test that gateway-caller role ARN returns True."""
        role_arn = "arn:aws:sts::123456789012:assumed-role/gateway-caller/session-name"
        assert tenant_resolver.is_cognito_user(role_arn) is True

    def test_cognito_identity_role_returns_true(self, tenant_resolver):
        """Test that cognito-identity role ARN returns True."""
        role_arn = "arn:aws:sts::123456789012:assumed-role/cognito-identity-pool-role/session"
        assert tenant_resolver.is_cognito_user(role_arn) is True

    def test_regular_assumed_role_returns_false(self, tenant_resolver):
        """Test that regular assumed role ARN returns False."""
        role_arn = "arn:aws:sts::123456789012:assumed-role/ServiceRole/session-name"
        assert tenant_resolver.is_cognito_user(role_arn) is False

    def test_sso_role_returns_false(self, tenant_resolver):
        """Test that AWS SSO role ARN returns False."""
        role_arn = "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Developer/john@test.com"
        assert tenant_resolver.is_cognito_user(role_arn) is False

    def test_iam_user_returns_false(self, tenant_resolver):
        """Test that IAM user ARN returns False."""
        user_arn = "arn:aws:iam::123456789012:user/testuser"
        assert tenant_resolver.is_cognito_user(user_arn) is False

    def test_empty_arn_returns_false(self, tenant_resolver):
        """Test that empty ARN returns False."""
        assert tenant_resolver.is_cognito_user("") is False

    def test_none_arn_returns_false(self, tenant_resolver):
        """Test that None ARN returns False."""
        assert tenant_resolver.is_cognito_user(None) is False


class TestExtractCognitoTenantInfo:
    """Tests for extract_cognito_tenant_info method."""

    def test_extracts_full_tenant_info(self, tenant_resolver):
        """Test extracting full tenant info from session name."""
        caller_identity = AWSCallerIdentity(
            user_id="AROA1234567890EXAMPLE",
            account="123456789012",
            arn="arn:aws:sts::123456789012:assumed-role/gateway-caller/org-test-org_dept-engineering_team-backend_role-admin",
        )

        info = tenant_resolver.extract_cognito_tenant_info(caller_identity)

        assert info.org_id == "test-org"
        assert info.department_id == "engineering"
        assert info.team_id == "backend"
        assert info.role == "admin"

    def test_extracts_partial_tenant_info(self, tenant_resolver):
        """Test extracting partial tenant info from session name."""
        caller_identity = AWSCallerIdentity(
            user_id="AROA1234567890EXAMPLE",
            account="123456789012",
            arn="arn:aws:sts::123456789012:assumed-role/gateway-caller/org-test-org_dept-sales",
        )

        info = tenant_resolver.extract_cognito_tenant_info(caller_identity)

        assert info.org_id == "test-org"
        assert info.department_id == "sales"
        assert info.team_id is None
        assert info.role is None

    def test_extracts_only_org_id(self, tenant_resolver):
        """Test extracting only org_id from session name."""
        caller_identity = AWSCallerIdentity(
            user_id="AROA1234567890EXAMPLE",
            account="123456789012",
            arn="arn:aws:sts::123456789012:assumed-role/gateway-caller/org-my-company",
        )

        info = tenant_resolver.extract_cognito_tenant_info(caller_identity)

        assert info.org_id == "my-company"
        assert info.department_id is None
        assert info.team_id is None

    def test_handles_unstructured_session_name(self, tenant_resolver):
        """Test handling session name without tenant prefixes."""
        caller_identity = AWSCallerIdentity(
            user_id="AROA1234567890EXAMPLE",
            account="123456789012",
            arn="arn:aws:sts::123456789012:assumed-role/gateway-caller/random-session-name",
        )

        info = tenant_resolver.extract_cognito_tenant_info(caller_identity)

        assert info.org_id is None
        assert info.department_id is None
        assert info.team_id is None
        assert info.role is None

    def test_handles_iam_role_arn(self, tenant_resolver):
        """Test handling IAM role ARN without assumed-role."""
        caller_identity = AWSCallerIdentity(
            user_id="AROA1234567890EXAMPLE",
            account="123456789012",
            arn="arn:aws:iam::123456789012:role/gateway-caller",
        )

        info = tenant_resolver.extract_cognito_tenant_info(caller_identity)

        # Should return empty info since no assumed-role with session name
        assert isinstance(info, CognitoTenantInfo)

    def test_handles_empty_arn(self, tenant_resolver):
        """Test handling empty ARN."""
        caller_identity = AWSCallerIdentity(
            user_id="AROA1234567890EXAMPLE",
            account="123456789012",
            arn="",
        )

        info = tenant_resolver.extract_cognito_tenant_info(caller_identity)

        assert info.org_id is None
        assert info.department_id is None


class TestResolveCognitoUser:
    """Tests for _resolve_cognito_user method."""

    @pytest.mark.asyncio
    async def test_resolves_cognito_user_with_org_from_session(self, tenant_resolver, mock_db_session):
        """Test resolving Cognito user with org_id from session name."""
        caller_identity = AWSCallerIdentity(
            user_id="cognito-sub-12345",
            account="123456789012",
            arn="arn:aws:sts::123456789012:assumed-role/gateway-caller/org-test-org_dept-eng_team-backend_role-admin",
        )

        # Mock organization lookup
        mock_org = MagicMock(spec=Organization)
        mock_org.id = "test-org"
        mock_org.name = "Test Organization"

        # First call for org lookup, second for user lookup (returns None)
        call_count = [0]

        def mock_execute_side_effect(*args, **kwargs):
            result = MagicMock()
            if call_count[0] == 0:  # Organization lookup
                result.scalar_one_or_none.return_value = mock_org
            else:  # User lookup
                result.scalar_one_or_none.return_value = None
            call_count[0] += 1
            return result

        mock_db_session.execute = AsyncMock(side_effect=mock_execute_side_effect)

        result = await tenant_resolver._resolve_cognito_user(caller_identity, mock_db_session)

        assert result.org_id == "test-org"
        assert result.org_name == "Test Organization"
        assert result.account_type == "cognito"
        assert result.is_admin is True

    @pytest.mark.asyncio
    async def test_resolves_cognito_user_with_db_user(self, tenant_resolver, mock_db_session):
        """Test resolving Cognito user who exists in database."""
        caller_identity = AWSCallerIdentity(
            user_id="cognito-sub-12345",
            account="123456789012",
            arn="arn:aws:sts::123456789012:assumed-role/gateway-caller/org-test-org",
        )

        # Mock organization
        mock_org = MagicMock(spec=Organization)
        mock_org.id = "test-org"
        mock_org.name = "Test Organization"

        # Mock user
        mock_user = MagicMock(spec=User)
        mock_user.id = "user-123"
        mock_user.team_id = "team-backend"
        mock_user.cognito_sub = "cognito-sub-12345"

        # Mock team
        mock_team = MagicMock()
        mock_team.department_id = "dept-eng"

        # Setup mock responses
        call_count = [0]

        def mock_execute_side_effect(*args, **kwargs):
            result = MagicMock()
            if call_count[0] == 0:  # Organization lookup
                result.scalar_one_or_none.return_value = mock_org
            elif call_count[0] == 1:  # User lookup
                result.scalar_one_or_none.return_value = mock_user
            elif call_count[0] == 2:  # Team lookup
                result.scalar_one_or_none.return_value = mock_team
            call_count[0] += 1
            return result

        mock_db_session.execute = AsyncMock(side_effect=mock_execute_side_effect)

        result = await tenant_resolver._resolve_cognito_user(caller_identity, mock_db_session)

        assert result.org_id == "test-org"
        assert result.entity_id == "user-123"
        assert result.account_type == "cognito"

    @pytest.mark.asyncio
    async def test_raises_unknown_org_error_when_org_not_found(self, tenant_resolver, mock_db_session):
        """Test that UnknownOrganizationError is raised when org not found."""
        caller_identity = AWSCallerIdentity(
            user_id="cognito-sub-12345",
            account="123456789012",
            arn="arn:aws:sts::123456789012:assumed-role/gateway-caller/org-nonexistent",
        )

        # Mock organization not found
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db_session.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(UnknownOrganizationError):
            await tenant_resolver._resolve_cognito_user(caller_identity, mock_db_session)

    @pytest.mark.asyncio
    async def test_falls_back_to_account_lookup_without_org_in_session(self, tenant_resolver, mock_db_session):
        """Test fallback to AWS account lookup when org_id not in session."""
        caller_identity = AWSCallerIdentity(
            user_id="cognito-sub-12345",
            account="123456789012",
            arn="arn:aws:sts::123456789012:assumed-role/gateway-caller/random-session",
        )

        # Mock organization with account lookup
        mock_org = MagicMock(spec=Organization)
        mock_org.id = "found-org"
        mock_org.name = "Found Organization"
        mock_org.aws_accounts = ["123456789012"]

        # First call returns all orgs (for account lookup), second call returns None (no user)
        call_count = [0]

        def mock_execute_side_effect(*args, **kwargs):
            result = MagicMock()
            if call_count[0] == 0:  # Organization list lookup
                result.scalars.return_value.all.return_value = [mock_org]
            else:  # User lookup
                result.scalar_one_or_none.return_value = None
            call_count[0] += 1
            return result

        mock_db_session.execute = AsyncMock(side_effect=mock_execute_side_effect)

        result = await tenant_resolver._resolve_cognito_user(caller_identity, mock_db_session)

        assert result.org_id == "found-org"
        assert result.account_type == "cognito"


class TestResolveTenantCognitoIntegration:
    """Integration tests for resolve_tenant with Cognito users."""

    @pytest.mark.asyncio
    async def test_resolve_tenant_routes_cognito_user_correctly(self, tenant_resolver, mock_db_session):
        """Test that resolve_tenant routes Cognito users to _resolve_cognito_user."""
        caller_identity = AWSCallerIdentity(
            user_id="cognito-sub-12345",
            account="123456789012",
            arn="arn:aws:sts::123456789012:assumed-role/gateway-caller/org-test-org_role-user",
        )

        # Mock organization
        mock_org = MagicMock(spec=Organization)
        mock_org.id = "test-org"
        mock_org.name = "Test Organization"

        # First call for org lookup, second for user lookup (returns None)
        call_count = [0]

        def mock_execute_side_effect(*args, **kwargs):
            result = MagicMock()
            if call_count[0] == 0:  # Organization lookup
                result.scalar_one_or_none.return_value = mock_org
            else:  # User lookup
                result.scalar_one_or_none.return_value = None
            call_count[0] += 1
            return result

        mock_db_session.execute = AsyncMock(side_effect=mock_execute_side_effect)

        result = await tenant_resolver.resolve_tenant(caller_identity, mock_db_session)

        assert result.account_type == "cognito"
        assert result.org_id == "test-org"

    @pytest.mark.asyncio
    async def test_resolve_tenant_routes_non_cognito_to_standard_flow(self, tenant_resolver, mock_db_session):
        """Test that resolve_tenant routes non-Cognito users to standard flow."""
        caller_identity = AWSCallerIdentity(
            user_id="AROA1234567890EXAMPLE",
            account="123456789012",
            arn="arn:aws:sts::123456789012:assumed-role/ServiceRole/session",
        )

        # Mock organization with AWS account
        mock_org = MagicMock(spec=Organization)
        mock_org.id = "test-org"
        mock_org.name = "Test Organization"
        mock_org.aws_accounts = ["123456789012"]
        mock_org.role_mappings = {}

        # Mock service account
        mock_sa = MagicMock()
        mock_sa.id = "sa-123"
        mock_sa.department_id = "default"
        mock_sa.team_id = "default"
        mock_sa.name = "ServiceRole"

        # Mock department and team
        mock_dept = MagicMock()
        mock_dept.id = "default"
        mock_team = MagicMock()
        mock_team.id = "default"
        mock_team.department_id = "default"

        call_count = [0]

        def mock_execute_side_effect(*args, **kwargs):
            result = MagicMock()
            if call_count[0] == 0:  # Organization list
                result.scalars.return_value.all.return_value = [mock_org]
            elif call_count[0] == 1:  # Service account lookup
                result.scalar_one_or_none.return_value = mock_sa
            elif call_count[0] == 2:  # Department lookup
                result.scalar_one_or_none.return_value = mock_dept
            elif call_count[0] == 3:  # Team lookup
                result.scalar_one_or_none.return_value = mock_team
            call_count[0] += 1
            return result

        mock_db_session.execute = AsyncMock(side_effect=mock_execute_side_effect)

        result = await tenant_resolver.resolve_tenant(caller_identity, mock_db_session)

        assert result.account_type == "service"
        assert result.org_id == "test-org"


class TestCognitoTenantInfoDataclass:
    """Tests for CognitoTenantInfo dataclass."""

    def test_default_values_are_none(self):
        """Test that default values are None."""
        info = CognitoTenantInfo()

        assert info.org_id is None
        assert info.department_id is None
        assert info.team_id is None
        assert info.role is None

    def test_can_set_all_values(self):
        """Test that all values can be set."""
        info = CognitoTenantInfo(
            org_id="org-123",
            department_id="dept-456",
            team_id="team-789",
            role="admin",
        )

        assert info.org_id == "org-123"
        assert info.department_id == "dept-456"
        assert info.team_id == "team-789"
        assert info.role == "admin"
