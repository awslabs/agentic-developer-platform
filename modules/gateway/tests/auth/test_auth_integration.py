"""
Integration tests for the Authentication module.

These tests verify end-to-end authentication flows with real database
interactions and component integration (using mocked AWS STS).
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.auth.auth_service import AuthService
from src.auth.schemas import ServiceAccountCreate, TenantInfo
from src.auth.service_account_service import ServiceAccountService
from src.auth.sts_client import STSClient
from src.auth.tenant_resolver import TenantResolver
from src.auth.token_manager import TokenManager
from src.shared.exceptions import UnknownOrganizationError, UnregisteredServiceAccountError
from src.shared.schemas.auth import AuthExchangeRequest, TokenContext

from .conftest import create_sample_token


@pytest.mark.integration
class TestAuthIntegration:
    """Integration test suite for authentication flows."""

    @pytest.mark.asyncio
    async def test_complete_service_account_authentication_flow(
        self, db_session, sample_organization, sample_department, sample_team, sample_service_account
    ):
        """Test complete service account authentication flow end-to-end."""
        # Mock STS responses for service account
        mock_sts_responses = {
            "get_caller_identity": {
                "UserId": "AIDACKCEVSQ6C2EXAMPLE",
                "Account": "123456789012",
                "Arn": "arn:aws:sts::123456789012:assumed-role/ml-training-service/session",
            }
        }

        # Create services with mocked STS
        sts_client = STSClient(mock_responses=mock_sts_responses)
        tenant_resolver = TenantResolver()
        token_manager = TokenManager("test-secret-key")
        auth_service = AuthService(sts_client, tenant_resolver, token_manager)

        # Step 1: Exchange AWS credentials for gateway token
        request = AuthExchangeRequest(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="session-token",
        )

        response = await auth_service.exchange_credentials(request, db_session)

        assert response.token is not None
        assert response.account_type == "service"
        assert response.user_id == sample_service_account.id
        assert response.org_id == sample_organization.id
        assert response.expires_at > datetime.now(UTC)

        # Step 2: Validate the returned token
        token_context = await auth_service.validate_token(response.token, db_session)

        assert isinstance(token_context, TokenContext)
        assert token_context.user_id == sample_service_account.id
        assert token_context.org_id == sample_organization.id
        assert token_context.account_type == "service"
        assert token_context.team_id == sample_team.id
        assert token_context.department_id == sample_department.id

        # Step 3: Revoke the token
        await auth_service.revoke_token(response.token, db_session)

        # Step 4: Verify token is no longer valid
        with pytest.raises(Exception):  # Should raise TokenValidationError
            await auth_service.validate_token(response.token, db_session)

    @pytest.mark.asyncio
    async def test_complete_human_user_authentication_flow(self, db_session, sample_organization, sample_department, sample_team, sample_user):
        """Test complete human user authentication flow end-to-end."""
        # Mock STS responses for human user
        mock_sts_responses = {
            "get_caller_identity": {
                "UserId": "AIDACKCEVSQ6C2EXAMPLE",
                "Account": "123456789012",
                "Arn": "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Developer/john.doe@test.com",
            }
        }

        # Create services with mocked STS
        sts_client = STSClient(mock_responses=mock_sts_responses)
        tenant_resolver = TenantResolver()
        token_manager = TokenManager("test-secret-key")
        auth_service = AuthService(sts_client, tenant_resolver, token_manager)

        # Exchange credentials
        request = AuthExchangeRequest(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="session-token",
        )

        response = await auth_service.exchange_credentials(request, db_session)

        assert response.token is not None
        assert response.account_type == "human"
        assert response.user_id == sample_user.id
        assert response.org_id == sample_organization.id

        # Validate token
        token_context = await auth_service.validate_token(response.token, db_session)
        assert token_context.user_id == sample_user.id
        assert token_context.account_type == "human"

    @pytest.mark.asyncio
    async def test_unknown_organization_error_flow(self, db_session):
        """Test authentication flow with unknown organization (US-9.2)."""
        # Mock STS responses with unknown account
        mock_sts_responses = {
            "get_caller_identity": {
                "UserId": "AIDACKCEVSQ6C2EXAMPLE",
                "Account": "999888777666",  # Unknown account
                "Arn": "arn:aws:sts::999888777666:assumed-role/some-role/session",
            }
        }

        sts_client = STSClient(mock_responses=mock_sts_responses)
        tenant_resolver = TenantResolver()
        token_manager = TokenManager("test-secret-key")
        auth_service = AuthService(sts_client, tenant_resolver, token_manager)

        request = AuthExchangeRequest(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="session-token",
        )

        with pytest.raises(UnknownOrganizationError) as exc_info:
            await auth_service.exchange_credentials(request, db_session)

        assert "999888777666" in str(exc_info.value)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_unregistered_service_account_error_flow(self, db_session, sample_organization):
        """Test authentication flow with unregistered service account (US-9.5)."""
        # Mock STS responses with unregistered role
        mock_sts_responses = {
            "get_caller_identity": {
                "UserId": "AIDACKCEVSQ6C2EXAMPLE",
                "Account": "123456789012",
                "Arn": "arn:aws:sts::123456789012:assumed-role/unregistered-role/session",
            }
        }

        sts_client = STSClient(mock_responses=mock_sts_responses)
        tenant_resolver = TenantResolver()
        token_manager = TokenManager("test-secret-key")
        auth_service = AuthService(sts_client, tenant_resolver, token_manager)

        request = AuthExchangeRequest(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="session-token",
        )

        with pytest.raises(UnregisteredServiceAccountError) as exc_info:
            await auth_service.exchange_credentials(request, db_session)

        assert "arn:aws:iam::123456789012:role/unregistered-role" in str(exc_info.value)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_service_account_crud_integration(self, db_session, sample_organization, sample_department, sample_team):
        """Test complete service account CRUD operations integration."""
        service_account_service = ServiceAccountService()

        # Create service account
        create_data = ServiceAccountCreate(
            name="Integration Test Service Account",
            department_id=sample_department.id,
            team_id=sample_team.id,
            iam_role_arn="arn:aws:iam::123456789012:role/integration-test-service",
        )

        created_sa = await service_account_service.create_service_account(create_data, sample_organization.id, db_session)

        assert created_sa.name == "Integration Test Service Account"
        assert created_sa.org_id == sample_organization.id

        # Read service account
        retrieved_sa = await service_account_service.get_service_account(created_sa.id, sample_organization.id, db_session)
        assert retrieved_sa.id == created_sa.id

        # Update service account
        from src.auth.schemas import ServiceAccountUpdate

        update_data = ServiceAccountUpdate(name="Updated Integration Test Service Account")

        updated_sa = await service_account_service.update_service_account(created_sa.id, update_data, sample_organization.id, db_session)
        assert updated_sa.name == "Updated Integration Test Service Account"

        # List service accounts
        list_result = await service_account_service.list_service_accounts(sample_organization.id, db_session)
        assert len(list_result.service_accounts) >= 1

        # Find by role ARN
        found_sa = await service_account_service.find_service_account_by_role_arn(created_sa.iam_role_arn, db_session)
        assert found_sa is not None
        assert found_sa.id == created_sa.id

        # Delete service account
        delete_result = await service_account_service.delete_service_account(created_sa.id, sample_organization.id, db_session)
        assert delete_result is True

        # Verify deletion
        with pytest.raises(Exception):  # Should raise ServiceAccountNotFoundError
            await service_account_service.get_service_account(created_sa.id, sample_organization.id, db_session)

    @pytest.mark.asyncio
    async def test_token_lifecycle_integration(self, db_session, sample_tenant_info: TenantInfo):
        """Test complete token lifecycle integration."""
        token_manager = TokenManager("test-secret-key")

        # Generate token
        token, expires_at = token_manager.generate_token(sample_tenant_info)
        assert token is not None
        assert expires_at > datetime.now(UTC)

        # Store token
        token_id = await token_manager.store_token(token, sample_tenant_info, expires_at, db_session)
        assert token_id is not None

        # Validate token
        token_context = await token_manager.validate_token(token, db_session)
        assert token_context.user_id == sample_tenant_info.entity_id
        assert token_context.org_id == sample_tenant_info.org_id

        # Create additional tokens for the same user
        token2, expires_at2 = token_manager.generate_token(sample_tenant_info)
        await token_manager.store_token(token2, sample_tenant_info, expires_at2, db_session)

        token3, expires_at3 = token_manager.generate_token(sample_tenant_info)
        await token_manager.store_token(token3, sample_tenant_info, expires_at3, db_session)

        # Revoke all user tokens
        count = await token_manager.revoke_all_user_tokens(sample_tenant_info.entity_id, sample_tenant_info.org_id, db_session)
        assert count >= 3

        # Verify all tokens are revoked
        with pytest.raises(Exception):
            await token_manager.validate_token(token, db_session)
        with pytest.raises(Exception):
            await token_manager.validate_token(token2, db_session)
        with pytest.raises(Exception):
            await token_manager.validate_token(token3, db_session)

    @pytest.mark.asyncio
    async def test_expired_token_cleanup_integration(self, db_session, sample_tenant_info: TenantInfo):
        """Test expired token cleanup integration."""
        token_manager = TokenManager("test-secret-key")

        # Create old expired token (more than 24 hours ago)
        old_expiry = datetime.now(UTC) - timedelta(hours=25)
        await create_sample_token(db_session, sample_tenant_info, token_hash="old-expired-token-integration", expires_at=old_expiry)

        # Create recently expired token (less than 24 hours ago)
        recent_expiry = datetime.now(UTC) - timedelta(hours=1)
        await create_sample_token(db_session, sample_tenant_info, token_hash="recent-expired-token-integration", expires_at=recent_expiry)

        # Create valid token
        valid_expiry = datetime.now(UTC) + timedelta(hours=1)
        await create_sample_token(db_session, sample_tenant_info, token_hash="valid-token-integration", expires_at=valid_expiry)

        # Clean up expired tokens
        count = await token_manager.cleanup_expired_tokens(db_session)

        # Should clean up only the old expired token
        assert count == 1

    @pytest.mark.asyncio
    async def test_admin_privilege_integration(self, db_session, sample_organization, sample_department, sample_team):
        """Test admin privilege handling integration."""
        # Mock STS responses for admin role
        mock_sts_responses = {
            "get_caller_identity": {
                "UserId": "AIDACKCEVSQ6C2EXAMPLE",
                "Account": "123456789012",
                "Arn": "arn:aws:sts::123456789012:assumed-role/AdminRole/admin-session",
            }
        }

        sts_client = STSClient(mock_responses=mock_sts_responses)
        tenant_resolver = TenantResolver()
        token_manager = TokenManager("test-secret-key")
        auth_service = AuthService(sts_client, tenant_resolver, token_manager)

        # Create an admin service account
        service_account_service = ServiceAccountService()
        admin_sa_data = ServiceAccountCreate(
            name="Admin Service Account",
            department_id=sample_department.id,
            team_id=sample_team.id,
            iam_role_arn="arn:aws:iam::123456789012:role/AdminRole",
        )

        await service_account_service.create_service_account(admin_sa_data, sample_organization.id, db_session)

        # Exchange credentials - should get admin privileges
        request = AuthExchangeRequest(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY",
            aws_session_token="session-token",
        )

        response = await auth_service.exchange_credentials(request, db_session)

        # Validate token and check admin privileges
        token_context = await auth_service.validate_token(response.token, db_session)

        # Admin role should be detected based on organization role mappings
        # Note: This depends on the sample_organization fixture having "AdminRole" in admin_roles
        assert token_context.is_admin is True

    @pytest.mark.asyncio
    async def test_multi_tenant_isolation(self, db_session, sample_organization, sample_department, sample_team):
        """Test that tenant isolation works correctly."""
        from src.shared.models.organization import Department, Organization, Team

        # Create a second organization
        org2 = Organization(id="org-test-456", name="Test Organization 2", aws_accounts=["210987654321"], role_mappings={}, settings={})
        db_session.add(org2)

        dept2 = Department(id="dept-engineering-2", org_id=org2.id, name="Engineering 2")
        db_session.add(dept2)

        team2 = Team(id="team-ml-2", org_id=org2.id, department_id=dept2.id, name="Machine Learning 2")
        db_session.add(team2)

        await db_session.commit()

        # Create service account in org2
        service_account_service = ServiceAccountService()
        sa_data = ServiceAccountCreate(
            name="Org2 Service Account", department_id=dept2.id, team_id=team2.id, iam_role_arn="arn:aws:iam::210987654321:role/org2-service"
        )

        org2_sa = await service_account_service.create_service_account(sa_data, org2.id, db_session)

        # Try to access org2's service account from org1 context (should fail)
        with pytest.raises(Exception):  # Should raise ServiceAccountNotFoundError
            await service_account_service.get_service_account(
                org2_sa.id,
                sample_organization.id,
                db_session,  # Wrong org_id
            )

        # Access from correct org should work
        retrieved_sa = await service_account_service.get_service_account(org2_sa.id, org2.id, db_session)
        assert retrieved_sa.id == org2_sa.id
