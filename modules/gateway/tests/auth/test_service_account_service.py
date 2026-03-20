"""
Unit tests for the Service Account Service module.

These tests cover CRUD operations for service accounts with proper
validation, error handling, and database integration.
"""

from unittest.mock import patch

import pytest

from src.auth.exceptions import DuplicateServiceAccountError, ServiceAccountNotFoundError, TenantResolutionError
from src.auth.schemas import ServiceAccountCreate, ServiceAccountResponse, ServiceAccountUpdate
from src.auth.service_account_service import ServiceAccountService


@pytest.mark.unit
class TestServiceAccountService:
    """Test suite for ServiceAccountService."""

    def test_init(self):
        """Test ServiceAccountService initialization."""
        service = ServiceAccountService()
        assert service is not None

    @pytest.mark.asyncio
    async def test_create_service_account_success(
        self, db_session, service_account_service: ServiceAccountService, sample_organization, sample_department, sample_team
    ):
        """Test successful service account creation."""
        create_data = ServiceAccountCreate(
            name="Test Service Account",
            department_id=sample_department.id,
            team_id=sample_team.id,
            iam_role_arn="arn:aws:iam::123456789012:role/test-service-account",
        )

        result = await service_account_service.create_service_account(create_data, sample_organization.id, db_session)

        assert isinstance(result, ServiceAccountResponse)
        assert result.name == create_data.name
        assert result.org_id == sample_organization.id
        assert result.department_id == create_data.department_id
        assert result.team_id == create_data.team_id
        assert result.iam_role_arn == create_data.iam_role_arn

    @pytest.mark.asyncio
    async def test_create_service_account_invalid_department(
        self, db_session, service_account_service: ServiceAccountService, sample_organization, sample_team
    ):
        """Test service account creation with invalid department."""
        create_data = ServiceAccountCreate(
            name="Test Service Account",
            department_id="invalid-department",
            team_id=sample_team.id,
            iam_role_arn="arn:aws:iam::123456789012:role/test-service-account",
        )

        with pytest.raises(TenantResolutionError) as exc_info:
            await service_account_service.create_service_account(create_data, sample_organization.id, db_session)

        assert "Department invalid-department not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_service_account_invalid_team(
        self, db_session, service_account_service: ServiceAccountService, sample_organization, sample_department
    ):
        """Test service account creation with invalid team."""
        create_data = ServiceAccountCreate(
            name="Test Service Account",
            department_id=sample_department.id,
            team_id="invalid-team",
            iam_role_arn="arn:aws:iam::123456789012:role/test-service-account",
        )

        with pytest.raises(TenantResolutionError) as exc_info:
            await service_account_service.create_service_account(create_data, sample_organization.id, db_session)

        assert "Team invalid-team not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_service_account_duplicate_arn(
        self, db_session, service_account_service: ServiceAccountService, sample_organization, sample_department, sample_team, sample_service_account
    ):
        """Test service account creation with duplicate IAM role ARN."""
        create_data = ServiceAccountCreate(
            name="Duplicate Service Account",
            department_id=sample_department.id,
            team_id=sample_team.id,
            iam_role_arn=sample_service_account.iam_role_arn,  # Duplicate ARN
        )

        with pytest.raises(DuplicateServiceAccountError) as exc_info:
            await service_account_service.create_service_account(create_data, sample_organization.id, db_session)

        assert sample_service_account.iam_role_arn in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_service_account_database_error(
        self, db_session, service_account_service: ServiceAccountService, sample_organization, sample_department, sample_team
    ):
        """Test service account creation with database error."""
        create_data = ServiceAccountCreate(
            name="Test Service Account",
            department_id=sample_department.id,
            team_id=sample_team.id,
            iam_role_arn="arn:aws:iam::123456789012:role/test-service-account",
        )

        # Mock database commit to raise an error
        with patch.object(db_session, "commit", side_effect=Exception("Database error")):
            with pytest.raises(TenantResolutionError) as exc_info:
                await service_account_service.create_service_account(create_data, sample_organization.id, db_session)

            assert "Service account creation failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_service_account_success(
        self, db_session, service_account_service: ServiceAccountService, sample_organization, sample_service_account
    ):
        """Test successful service account retrieval."""
        result = await service_account_service.get_service_account(sample_service_account.id, sample_organization.id, db_session)

        assert isinstance(result, ServiceAccountResponse)
        assert result.id == sample_service_account.id
        assert result.name == sample_service_account.name
        assert result.org_id == sample_organization.id

    @pytest.mark.asyncio
    async def test_get_service_account_not_found(self, db_session, service_account_service: ServiceAccountService, sample_organization):
        """Test service account retrieval when not found."""
        with pytest.raises(ServiceAccountNotFoundError) as exc_info:
            await service_account_service.get_service_account("non-existent-id", sample_organization.id, db_session)

        assert "non-existent-id" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_service_account_wrong_org(self, db_session, service_account_service: ServiceAccountService, sample_service_account):
        """Test service account retrieval with wrong organization."""
        with pytest.raises(ServiceAccountNotFoundError):
            await service_account_service.get_service_account(sample_service_account.id, "wrong-org-id", db_session)

    @pytest.mark.asyncio
    async def test_update_service_account_success(
        self, db_session, service_account_service: ServiceAccountService, sample_organization, sample_service_account
    ):
        """Test successful service account update."""
        update_data = ServiceAccountUpdate(name="Updated Service Account Name")

        result = await service_account_service.update_service_account(sample_service_account.id, update_data, sample_organization.id, db_session)

        assert isinstance(result, ServiceAccountResponse)
        assert result.name == "Updated Service Account Name"
        assert result.id == sample_service_account.id

    @pytest.mark.asyncio
    async def test_update_service_account_no_changes(
        self, db_session, service_account_service: ServiceAccountService, sample_organization, sample_service_account
    ):
        """Test service account update with no changes."""
        update_data = ServiceAccountUpdate()

        result = await service_account_service.update_service_account(sample_service_account.id, update_data, sample_organization.id, db_session)

        # Should return existing service account unchanged
        assert isinstance(result, ServiceAccountResponse)
        assert result.name == sample_service_account.name

    @pytest.mark.asyncio
    async def test_update_service_account_not_found(self, db_session, service_account_service: ServiceAccountService, sample_organization):
        """Test updating non-existent service account."""
        update_data = ServiceAccountUpdate(name="New Name")

        with pytest.raises(ServiceAccountNotFoundError):
            await service_account_service.update_service_account("non-existent-id", update_data, sample_organization.id, db_session)

    @pytest.mark.asyncio
    async def test_delete_service_account_success(
        self, db_session, service_account_service: ServiceAccountService, sample_organization, sample_service_account
    ):
        """Test successful service account deletion."""
        result = await service_account_service.delete_service_account(sample_service_account.id, sample_organization.id, db_session)

        assert result is True

    @pytest.mark.asyncio
    async def test_delete_service_account_not_found(self, db_session, service_account_service: ServiceAccountService, sample_organization):
        """Test deleting non-existent service account."""
        with pytest.raises(ServiceAccountNotFoundError):
            await service_account_service.delete_service_account("non-existent-id", sample_organization.id, db_session)

    @pytest.mark.asyncio
    async def test_list_service_accounts_success(
        self, db_session, service_account_service: ServiceAccountService, sample_organization, sample_service_account
    ):
        """Test successful service account listing."""
        result = await service_account_service.list_service_accounts(sample_organization.id, db_session)

        assert result.total_count >= 1
        assert len(result.service_accounts) >= 1
        assert result.page == 1
        assert result.page_size == 50

        # Check that our sample service account is in the list
        service_account_ids = [sa.id for sa in result.service_accounts]
        assert sample_service_account.id in service_account_ids

    @pytest.mark.asyncio
    async def test_list_service_accounts_with_filters(
        self, db_session, service_account_service: ServiceAccountService, sample_organization, sample_service_account, sample_department, sample_team
    ):
        """Test service account listing with department and team filters."""
        result = await service_account_service.list_service_accounts(
            sample_organization.id, db_session, department_id=sample_department.id, team_id=sample_team.id
        )

        assert result.total_count >= 1
        assert len(result.service_accounts) >= 1

        # All returned service accounts should match the filters
        for sa in result.service_accounts:
            assert sa.department_id == sample_department.id
            assert sa.team_id == sample_team.id

    @pytest.mark.asyncio
    async def test_list_service_accounts_pagination(
        self, db_session, service_account_service: ServiceAccountService, sample_organization, sample_service_account
    ):
        """Test service account listing with pagination."""
        result = await service_account_service.list_service_accounts(sample_organization.id, db_session, page=1, page_size=10)

        assert result.page == 1
        assert result.page_size == 10
        assert len(result.service_accounts) <= 10

    @pytest.mark.asyncio
    async def test_find_service_account_by_role_arn_success(self, db_session, service_account_service: ServiceAccountService, sample_service_account):
        """Test finding service account by role ARN."""
        result = await service_account_service.find_service_account_by_role_arn(sample_service_account.iam_role_arn, db_session)

        assert result is not None
        assert isinstance(result, ServiceAccountResponse)
        assert result.id == sample_service_account.id
        assert result.iam_role_arn == sample_service_account.iam_role_arn

    @pytest.mark.asyncio
    async def test_find_service_account_by_role_arn_not_found(self, db_session, service_account_service: ServiceAccountService):
        """Test finding service account by non-existent role ARN."""
        result = await service_account_service.find_service_account_by_role_arn("arn:aws:iam::123456789012:role/non-existent-role", db_session)

        assert result is None

    @pytest.mark.asyncio
    async def test_find_service_account_by_role_arn_database_error(self, db_session, service_account_service: ServiceAccountService):
        """Test finding service account by role ARN with database error."""
        with patch.object(db_session, "execute", side_effect=Exception("Database error")):
            result = await service_account_service.find_service_account_by_role_arn("arn:aws:iam::123456789012:role/test-role", db_session)

            assert result is None  # Should handle error gracefully

    @pytest.mark.asyncio
    async def test_validate_department_and_team_success(
        self, db_session, service_account_service: ServiceAccountService, sample_organization, sample_department, sample_team
    ):
        """Test successful department and team validation."""
        # This should not raise any exception
        await service_account_service._validate_department_and_team(sample_department.id, sample_team.id, sample_organization.id, db_session)

    @pytest.mark.asyncio
    async def test_validate_department_and_team_invalid_department(
        self, db_session, service_account_service: ServiceAccountService, sample_organization, sample_team
    ):
        """Test department and team validation with invalid department."""
        with pytest.raises(TenantResolutionError) as exc_info:
            await service_account_service._validate_department_and_team("invalid-department", sample_team.id, sample_organization.id, db_session)

        assert "Department invalid-department not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_validate_department_and_team_invalid_team(
        self, db_session, service_account_service: ServiceAccountService, sample_organization, sample_department
    ):
        """Test department and team validation with invalid team."""
        with pytest.raises(TenantResolutionError) as exc_info:
            await service_account_service._validate_department_and_team(sample_department.id, "invalid-team", sample_organization.id, db_session)

        assert "Team invalid-team not found" in str(exc_info.value)
