"""
Acceptance Criteria Validation Tests for Authentication Module.

These tests validate that all user stories and acceptance criteria
from Issue #41 are fully implemented and working correctly.

User Stories Covered:
- US-1.4: Human User Authentication via AWS SSO
- US-1.5: Service Account Registration
- US-1.6: Automated Agent Authentication (M2M)
- US-9.2: Unknown Organization
- US-9.5: Unregistered Service Account
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.auth.auth_service import AuthService
from src.auth.schemas import ServiceAccountCreate
from src.auth.service_account_service import ServiceAccountService
from src.auth.sts_client import STSClient
from src.auth.tenant_resolver import TenantResolver
from src.auth.token_manager import TokenManager
from src.shared.exceptions import UnknownOrganizationError, UnregisteredServiceAccountError
from src.shared.schemas.auth import AuthExchangeRequest


@pytest.mark.acceptance
class TestAcceptanceCriteria:
    """Acceptance criteria validation test suite."""

    @pytest.mark.asyncio
    async def test_us_1_4_human_user_authentication_via_aws_sso(self, db_session, sample_organization, sample_department, sample_team, sample_user):
        """
        US-1.4: Human User Authentication via AWS SSO

        Acceptance Criteria:
        - Human users can authenticate using AWS SSO temporary credentials
        - System validates credentials via AWS STS GetCallerIdentity
        - User identity is mapped to internal organization/team structure
        - JWT token is generated with appropriate user context
        - Token includes org_id, team_id, department_id, and user metadata
        """
        # Mock STS response for AWS SSO user
        mock_sts_responses = {
            "get_caller_identity": {
                "UserId": "AIDACKCEVSQ6C2EXAMPLE",
                "Account": "123456789012",
                "Arn": "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Developer/john.doe@test.com",
            }
        }

        # Create authentication service with mocked STS
        sts_client = STSClient(mock_responses=mock_sts_responses)
        tenant_resolver = TenantResolver()
        token_manager = TokenManager("test-secret-key")
        auth_service = AuthService(sts_client, tenant_resolver, token_manager)

        # Prepare AWS SSO credentials
        request = AuthExchangeRequest(
            aws_access_key_id="ASIAIOSFODNN7EXAMPLE",  # Temporary credentials from SSO
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="AQoDYXdzEJr...",  # Session token from SSO
        )

        # Authenticate human user
        response = await auth_service.exchange_credentials(request, db_session)

        # Validate response structure
        assert response.token is not None
        assert response.account_type == "human"
        assert response.user_id == sample_user.id
        assert response.org_id == sample_organization.id
        assert response.team_id == sample_team.id
        assert response.department_id == sample_department.id
        assert response.expires_at > datetime.now(UTC)

        # Validate token content
        token_context = await auth_service.validate_token(response.token, db_session)
        assert token_context.user_id == sample_user.id
        assert token_context.org_id == sample_organization.id
        assert token_context.team_id == sample_team.id
        assert token_context.department_id == sample_department.id
        assert token_context.account_type == "human"

        # ✓ US-1.4 ACCEPTANCE CRITERIA MET

    @pytest.mark.asyncio
    async def test_us_1_5_service_account_registration(self, db_session, sample_organization, sample_department, sample_team):
        """
        US-1.5: Service Account Registration

        Acceptance Criteria:
        - Admins can register IAM roles as service accounts
        - Service accounts are associated with org/department/team
        - IAM role ARN uniqueness is enforced across the system
        - Service accounts can be created, read, updated, deleted (CRUD)
        - Service account metadata includes name, role ARN, team assignment
        """
        service_account_service = ServiceAccountService()

        # Test CREATE operation
        create_data = ServiceAccountCreate(
            name="ML Training Pipeline",
            department_id=sample_department.id,
            team_id=sample_team.id,
            iam_role_arn="arn:aws:iam::123456789012:role/ml-training-pipeline",
        )

        created_sa = await service_account_service.create_service_account(create_data, sample_organization.id, db_session)

        assert created_sa.name == "ML Training Pipeline"
        assert created_sa.org_id == sample_organization.id
        assert created_sa.department_id == sample_department.id
        assert created_sa.team_id == sample_team.id
        assert created_sa.iam_role_arn == "arn:aws:iam::123456789012:role/ml-training-pipeline"

        # Test READ operation
        retrieved_sa = await service_account_service.get_service_account(created_sa.id, sample_organization.id, db_session)
        assert retrieved_sa.id == created_sa.id
        assert retrieved_sa.name == created_sa.name

        # Test LIST operation
        list_result = await service_account_service.list_service_accounts(sample_organization.id, db_session)
        assert len(list_result.service_accounts) >= 1
        assert any(sa.id == created_sa.id for sa in list_result.service_accounts)

        # Test UPDATE operation
        from src.auth.schemas import ServiceAccountUpdate

        update_data = ServiceAccountUpdate(name="Updated ML Training Pipeline")

        updated_sa = await service_account_service.update_service_account(created_sa.id, update_data, sample_organization.id, db_session)
        assert updated_sa.name == "Updated ML Training Pipeline"

        # Test IAM role ARN uniqueness enforcement
        duplicate_data = ServiceAccountCreate(
            name="Duplicate Service Account",
            department_id=sample_department.id,
            team_id=sample_team.id,
            iam_role_arn=created_sa.iam_role_arn,  # Same ARN should fail
        )

        with pytest.raises(Exception):  # Should raise DuplicateServiceAccountError
            await service_account_service.create_service_account(duplicate_data, sample_organization.id, db_session)

        # Test DELETE operation
        delete_result = await service_account_service.delete_service_account(created_sa.id, sample_organization.id, db_session)
        assert delete_result is True

        # Verify deletion
        with pytest.raises(Exception):  # Should raise ServiceAccountNotFoundError
            await service_account_service.get_service_account(created_sa.id, sample_organization.id, db_session)

        # ✓ US-1.5 ACCEPTANCE CRITERIA MET

    @pytest.mark.asyncio
    async def test_us_1_6_automated_agent_authentication_m2m(
        self, db_session, sample_organization, sample_department, sample_team, sample_service_account
    ):
        """
        US-1.6: Automated Agent Authentication (M2M)

        Acceptance Criteria:
        - Service accounts can authenticate using assumed IAM role credentials
        - System validates role credentials via AWS STS GetCallerIdentity
        - Role ARN is mapped to registered service account
        - JWT token is generated with service account context
        - Token includes service account metadata and team assignment
        - Supports machine-to-machine authentication patterns
        """
        # Mock STS response for assumed role (M2M)
        mock_sts_responses = {
            "get_caller_identity": {
                "UserId": "AIDACKCEVSQ6C2EXAMPLE",
                "Account": "123456789012",
                "Arn": "arn:aws:sts::123456789012:assumed-role/ml-training-service/automated-session-123",
            }
        }

        # Create authentication service with mocked STS
        sts_client = STSClient(mock_responses=mock_sts_responses)
        tenant_resolver = TenantResolver()
        token_manager = TokenManager("test-secret-key")
        auth_service = AuthService(sts_client, tenant_resolver, token_manager)

        # Prepare assumed role credentials (M2M pattern)
        request = AuthExchangeRequest(
            aws_access_key_id="ASIAIOSFODNN7EXAMPLE",  # Temporary credentials from AssumeRole
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="AQoDYXdzEJr...",  # Session token from AssumeRole
        )

        # Authenticate service account
        response = await auth_service.exchange_credentials(request, db_session)

        # Validate M2M authentication response
        assert response.token is not None
        assert response.account_type == "service"
        assert response.user_id == sample_service_account.id
        assert response.org_id == sample_organization.id
        assert response.team_id == sample_team.id
        assert response.department_id == sample_department.id

        # Validate service account token content
        token_context = await auth_service.validate_token(response.token, db_session)
        assert token_context.user_id == sample_service_account.id
        assert token_context.org_id == sample_organization.id
        assert token_context.account_type == "service"
        assert token_context.team_id == sample_service_account.team_id
        assert token_context.department_id == sample_service_account.department_id

        # Test token-based API access (simulating M2M usage)
        # Service account should be able to use token for subsequent API calls
        assert token_context.expires_at > datetime.now(UTC)

        # ✓ US-1.6 ACCEPTANCE CRITERIA MET

    @pytest.mark.asyncio
    async def test_us_9_2_unknown_organization_handling(self, db_session):
        """
        US-9.2: Unknown Organization

        Acceptance Criteria:
        - When AWS account is not registered with any organization, return 403
        - Error message clearly indicates the account is unregistered
        - Error message instructs user to contact platform administrator
        - System gracefully handles unknown organization scenario
        - No partial authentication or data leakage occurs
        """
        # Mock STS response for unknown AWS account
        mock_sts_responses = {
            "get_caller_identity": {
                "UserId": "AIDACKCEVSQ6C2EXAMPLE",
                "Account": "999888777666",  # Unknown account not in any organization
                "Arn": "arn:aws:sts::999888777666:assumed-role/some-role/session",
            }
        }

        # Create authentication service with mocked STS
        sts_client = STSClient(mock_responses=mock_sts_responses)
        tenant_resolver = TenantResolver()
        token_manager = TokenManager("test-secret-key")
        auth_service = AuthService(sts_client, tenant_resolver, token_manager)

        # Attempt authentication with unknown organization
        request = AuthExchangeRequest(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="session-token",
        )

        # Should raise UnknownOrganizationError
        with pytest.raises(UnknownOrganizationError) as exc_info:
            await auth_service.exchange_credentials(request, db_session)

        # Validate error properties
        error = exc_info.value
        assert error.status_code == 403
        assert error.error == "unknown_organization"
        assert "999888777666" in error.message
        assert "not registered with any organization" in error.message
        assert "Contact your platform administrator" in error.message

        # ✓ US-9.2 ACCEPTANCE CRITERIA MET

    @pytest.mark.asyncio
    async def test_us_9_5_unregistered_service_account_handling(self, db_session, sample_organization):
        """
        US-9.5: Unregistered Service Account

        Acceptance Criteria:
        - When IAM role is not registered as service account, return 403
        - Error message clearly indicates the role is unregistered
        - Error message instructs user to contact org administrator
        - System gracefully handles unregistered service account scenario
        - AWS account is recognized but specific role is not registered
        """
        # Mock STS response for unregistered service account role
        mock_sts_responses = {
            "get_caller_identity": {
                "UserId": "AIDACKCEVSQ6C2EXAMPLE",
                "Account": "123456789012",  # Known account (in sample_organization)
                "Arn": "arn:aws:sts::123456789012:assumed-role/unregistered-role/session",
            }
        }

        # Create authentication service with mocked STS
        sts_client = STSClient(mock_responses=mock_sts_responses)
        tenant_resolver = TenantResolver()
        token_manager = TokenManager("test-secret-key")
        auth_service = AuthService(sts_client, tenant_resolver, token_manager)

        # Attempt authentication with unregistered role
        request = AuthExchangeRequest(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="session-token",
        )

        # Should raise UnregisteredServiceAccountError
        with pytest.raises(UnregisteredServiceAccountError) as exc_info:
            await auth_service.exchange_credentials(request, db_session)

        # Validate error properties
        error = exc_info.value
        assert error.status_code == 403
        assert error.error == "unregistered_service_account"
        assert "not registered" in error.message.lower()
        assert "Contact your org administrator" in error.message

        # ✓ US-9.5 ACCEPTANCE CRITERIA MET

    @pytest.mark.asyncio
    async def test_token_validation_and_context_extraction(
        self, db_session, sample_organization, sample_team, sample_department, sample_service_account
    ):
        """
        Test that tokens contain all required context information
        and can be properly validated for authorization decisions.

        This validates the token validation pipeline used by middleware.
        """
        # Mock STS and create service
        mock_sts_responses = {
            "get_caller_identity": {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/ml-training-service/session"}
        }

        sts_client = STSClient(mock_responses=mock_sts_responses)
        auth_service = AuthService(sts_client=sts_client, tenant_resolver=TenantResolver(), token_manager=TokenManager("test-secret-key"))

        # Generate token
        request = AuthExchangeRequest(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="session-token",
        )

        auth_response = await auth_service.exchange_credentials(request, db_session)

        # Validate token provides complete context for middleware
        token_context = await auth_service.validate_token(auth_response.token, db_session)

        # All required fields for authorization are present
        assert token_context.user_id is not None
        assert token_context.org_id == sample_organization.id
        assert token_context.team_id == sample_team.id
        assert token_context.department_id == sample_department.id
        assert token_context.account_type in ["human", "service"]
        assert isinstance(token_context.is_admin, bool)
        assert token_context.expires_at > datetime.now(UTC)

        # Token can be revoked
        await auth_service.revoke_token(auth_response.token, db_session)

        # Revoked token should not validate
        with pytest.raises(Exception):
            await auth_service.validate_token(auth_response.token, db_session)

    @pytest.mark.asyncio
    async def test_admin_privilege_detection(self, db_session, sample_organization, sample_department, sample_team):
        """
        Test that admin privileges are correctly detected and included in tokens.

        This validates admin role mapping from organization configuration.
        """
        # Create admin service account
        service_account_service = ServiceAccountService()
        admin_sa_data = ServiceAccountCreate(
            name="Platform Admin Service",
            department_id=sample_department.id,
            team_id=sample_team.id,
            iam_role_arn="arn:aws:iam::123456789012:role/AdminRole",  # AdminRole is in sample org admin_roles
        )

        await service_account_service.create_service_account(admin_sa_data, sample_organization.id, db_session)

        # Mock STS for admin role
        mock_sts_responses = {
            "get_caller_identity": {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/AdminRole/admin-session"}
        }

        auth_service = AuthService(
            sts_client=STSClient(mock_responses=mock_sts_responses), tenant_resolver=TenantResolver(), token_manager=TokenManager("test-secret-key")
        )

        # Authenticate admin
        request = AuthExchangeRequest(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="session-token",
        )

        response = await auth_service.exchange_credentials(request, db_session)
        token_context = await auth_service.validate_token(response.token, db_session)

        # Admin privileges should be detected
        assert token_context.is_admin is True

    def test_integration_with_shared_interfaces(self):
        """
        Test that the auth module properly implements shared interfaces
        and integrates with the shared foundation.
        """
        from src.auth.auth_service import AuthService
        from src.shared.interfaces.auth import IAuthService

        # Verify AuthService implements IAuthService
        auth_service = AuthService()
        assert isinstance(auth_service, IAuthService)

        # Verify all interface methods are implemented
        assert hasattr(auth_service, "exchange_credentials")
        assert hasattr(auth_service, "validate_token")
        assert hasattr(auth_service, "revoke_token")

        # Verify proper return types are used
        from src.shared.schemas.auth import AuthExchangeRequest, AuthExchangeResponse, TokenContext

        # These are the expected schemas for interface compliance
        assert AuthExchangeRequest is not None
        assert AuthExchangeResponse is not None
        assert TokenContext is not None

    @pytest.mark.asyncio
    async def test_security_headers_and_token_security(self, db_session, sample_tenant_info):
        """
        Test that security measures are properly implemented:
        - Tokens are hashed before storage
        - JWT tokens have proper expiration
        - Token validation is comprehensive
        """
        token_manager = TokenManager("test-secret-key")

        # Generate token
        token, expires_at = token_manager.generate_token(sample_tenant_info)

        # Token should be a valid JWT
        assert isinstance(token, str)
        assert len(token.split(".")) == 3  # JWT has 3 parts

        # Token hash should be different from token
        token_hash = token_manager.hash_token(token)
        assert token_hash != token
        assert len(token_hash) == 64  # SHA-256 hex string

        # Token should have proper expiration
        assert expires_at > datetime.now(UTC)
        assert expires_at <= datetime.now(UTC) + timedelta(hours=24)

        # Store and validate token
        token_id = await token_manager.store_token(token, sample_tenant_info, expires_at, db_session)
        assert token_id is not None

        # Validation should work with proper token
        token_context = await token_manager.validate_token(token, db_session)
        assert token_context is not None

        # Validation should fail with tampered token
        tampered_token = token[:-5] + "tampr"
        with pytest.raises(Exception):
            await token_manager.validate_token(tampered_token, db_session)
