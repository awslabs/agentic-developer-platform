"""Tests for identity-index write-through client and service integration.

Issue #375: tenant-identity Phase A.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from src.admin.identity_index import IdentityIndexClient
from src.admin.schemas import OrganizationCreateRequest, OrganizationUpdateRequest
from src.admin.service import AdminService

# =============================================================================
# IdentityIndexClient unit tests
# =============================================================================


class TestIdentityIndexClient:
    """Unit tests for the IdentityIndexClient."""

    @pytest.fixture
    def mock_dynamodb(self):
        """Create a mock DynamoDB client."""
        client = MagicMock()
        client.put_item = MagicMock(return_value={})
        client.delete_item = MagicMock(return_value={})
        return client

    @pytest.fixture
    def index_client(self, mock_dynamodb):
        """Create an IdentityIndexClient with mocked DynamoDB."""
        return IdentityIndexClient(
            table_name="adp-dev-identity-index",
            dynamodb_client=mock_dynamodb,
        )

    @pytest.mark.asyncio
    async def test_put_identity_success(self, index_client, mock_dynamodb):
        """Test successful identity put."""
        result = await index_client.put_identity(
            identity_type="github_installation_id",
            identity_value="12345678",
            org_id="org-001",
        )
        assert result is True
        mock_dynamodb.put_item.assert_called_once()
        call_args = mock_dynamodb.put_item.call_args
        assert call_args[1]["TableName"] == "adp-dev-identity-index"
        item = call_args[1]["Item"]
        assert item["identity_type"]["S"] == "github_installation_id"
        assert item["identity_value"]["S"] == "12345678"
        assert item["org_id"]["S"] == "org-001"
        assert "ttl" in item

    @pytest.mark.asyncio
    async def test_put_identity_retry_on_failure(self, index_client, mock_dynamodb):
        """Test that put retries on transient DynamoDB errors."""
        error_response = {"Error": {"Code": "InternalServerError", "Message": "Service unavailable"}}
        mock_dynamodb.put_item.side_effect = [
            ClientError(error_response, "PutItem"),
            ClientError(error_response, "PutItem"),
            {},  # Success on 3rd attempt
        ]

        result = await index_client.put_identity(
            identity_type="cognito_client_id",
            identity_value="abc123",
            org_id="org-002",
        )
        assert result is True
        assert mock_dynamodb.put_item.call_count == 3

    @pytest.mark.asyncio
    async def test_put_identity_exhausts_retries(self, index_client, mock_dynamodb):
        """Test that put returns False after exhausting retries."""
        error_response = {"Error": {"Code": "InternalServerError", "Message": "Service unavailable"}}
        mock_dynamodb.put_item.side_effect = ClientError(error_response, "PutItem")

        result = await index_client.put_identity(
            identity_type="github_installation_id",
            identity_value="99999999",
            org_id="org-003",
        )
        assert result is False
        assert mock_dynamodb.put_item.call_count == 3

    @pytest.mark.asyncio
    async def test_delete_identity_success(self, index_client, mock_dynamodb):
        """Test successful identity deletion."""
        result = await index_client.delete_identity(
            identity_type="github_installation_id",
            identity_value="12345678",
        )
        assert result is True
        mock_dynamodb.delete_item.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_identity_retry_on_failure(self, index_client, mock_dynamodb):
        """Test that delete retries on transient errors."""
        error_response = {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Throttled"}}
        mock_dynamodb.delete_item.side_effect = [
            ClientError(error_response, "DeleteItem"),
            {},  # Success on 2nd attempt
        ]

        result = await index_client.delete_identity(
            identity_type="cognito_client_id",
            identity_value="xyz789",
        )
        assert result is True
        assert mock_dynamodb.delete_item.call_count == 2

    @pytest.mark.asyncio
    async def test_sync_identities_creates_and_deletes(self, index_client, mock_dynamodb):
        """Test sync handles additions and removals correctly."""
        await index_client.sync_identities_for_org(
            org_id="org-001",
            github_installation_ids=["new-1", "kept-1"],
            cognito_client_ids=["client-new"],
            old_github_installation_ids=["kept-1", "removed-1"],
            old_cognito_client_ids=["client-old"],
        )

        # Should have: put new-1, put kept-1, put client-new, delete removed-1, delete client-old
        assert mock_dynamodb.put_item.call_count == 3
        assert mock_dynamodb.delete_item.call_count == 2

    @pytest.mark.asyncio
    async def test_sync_identities_empty_lists(self, index_client, mock_dynamodb):
        """Test sync with empty lists does nothing."""
        await index_client.sync_identities_for_org(
            org_id="org-001",
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        mock_dynamodb.put_item.assert_not_called()
        mock_dynamodb.delete_item.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_all_for_org(self, index_client, mock_dynamodb):
        """Test cascade delete of all identities for an org."""
        await index_client.delete_all_for_org(
            github_installation_ids=["id1", "id2"],
            cognito_client_ids=["cid1"],
        )
        assert mock_dynamodb.delete_item.call_count == 3


# =============================================================================
# AdminService integration tests with identity-index
# =============================================================================


class TestAdminServiceIdentityWriteThrough:
    """Tests for AdminService org CRUD with identity-index write-through."""

    @pytest.fixture
    def mock_identity_index(self):
        """Create a mock IdentityIndexClient."""
        client = MagicMock(spec=IdentityIndexClient)
        client.sync_identities_for_org = AsyncMock()
        client.delete_all_for_org = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_create_org_triggers_write_through(self, db_session, mock_identity_index):
        """Test that creating an org with identities writes to the index."""
        service = AdminService(db=db_session, identity_index=mock_identity_index)

        request = OrganizationCreateRequest(
            name="Test Org With Identities",
            github_installation_ids=["inst-1", "inst-2"],
            cognito_client_ids=["client-a"],
        )

        result = await service.create_organization(request)

        assert result.github_installation_ids == ["inst-1", "inst-2"]
        assert result.cognito_client_ids == ["client-a"]

        mock_identity_index.sync_identities_for_org.assert_called_once_with(
            org_id=result.id,
            github_installation_ids=["inst-1", "inst-2"],
            cognito_client_ids=["client-a"],
        )

    @pytest.mark.asyncio
    async def test_create_org_without_identity_index(self, db_session):
        """Test that org creation works without identity_index (backward compat)."""
        service = AdminService(db=db_session)

        request = OrganizationCreateRequest(
            name="Test Org No Index",
            github_installation_ids=["inst-x"],
        )

        result = await service.create_organization(request)
        assert result.github_installation_ids == ["inst-x"]

    @pytest.mark.asyncio
    async def test_update_org_triggers_diff_sync(self, db_session, mock_identity_index):
        """Test that updating identity lists triggers diff-based sync."""
        service = AdminService(db=db_session, identity_index=mock_identity_index)

        # Create org first
        create_req = OrganizationCreateRequest(
            name="Update Test Org",
            github_installation_ids=["old-1", "kept-1"],
            cognito_client_ids=["c-old"],
        )
        created = await service.create_organization(create_req)
        mock_identity_index.sync_identities_for_org.reset_mock()

        # Update with changed identity lists
        update_req = OrganizationUpdateRequest(
            github_installation_ids=["kept-1", "new-1"],
            cognito_client_ids=["c-new"],
        )
        updated = await service.update_organization(created.id, update_req)

        assert updated.github_installation_ids == ["kept-1", "new-1"]
        assert updated.cognito_client_ids == ["c-new"]

        mock_identity_index.sync_identities_for_org.assert_called_once_with(
            org_id=created.id,
            github_installation_ids=["kept-1", "new-1"],
            cognito_client_ids=["c-new"],
            old_github_installation_ids=["old-1", "kept-1"],
            old_cognito_client_ids=["c-old"],
        )

    @pytest.mark.asyncio
    async def test_update_org_no_identity_change_skips_sync(self, db_session, mock_identity_index):
        """Test that updating non-identity fields skips index sync."""
        service = AdminService(db=db_session, identity_index=mock_identity_index)

        create_req = OrganizationCreateRequest(name="No Change Org")
        created = await service.create_organization(create_req)
        mock_identity_index.sync_identities_for_org.reset_mock()

        # Update only name — should NOT trigger sync
        update_req = OrganizationUpdateRequest(name="Renamed Org")
        await service.update_organization(created.id, update_req)

        mock_identity_index.sync_identities_for_org.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_org_triggers_index_cleanup(self, db_session, mock_identity_index):
        """Test that deleting an org removes its index entries."""
        service = AdminService(db=db_session, identity_index=mock_identity_index)

        create_req = OrganizationCreateRequest(
            name="Delete Test Org",
            github_installation_ids=["del-1"],
            cognito_client_ids=["del-c1", "del-c2"],
        )
        created = await service.create_organization(create_req)

        await service.delete_organization(created.id)

        mock_identity_index.delete_all_for_org.assert_called_once_with(["del-1"], ["del-c1", "del-c2"])

    @pytest.mark.asyncio
    async def test_create_org_ddb_failure_does_not_rollback_postgres(self, db_session, mock_identity_index):
        """Test that DDB write failure does not affect Postgres commit.

        Acceptance criteria: forced DDB write failure → org still exists in Postgres.
        """
        mock_identity_index.sync_identities_for_org = AsyncMock(side_effect=Exception("DDB unavailable"))

        service = AdminService(db=db_session, identity_index=mock_identity_index)

        request = OrganizationCreateRequest(
            name="Resilient Org",
            github_installation_ids=["fail-inst"],
        )

        # Should NOT raise — DDB failure is caught
        result = await service.create_organization(request)
        assert result.name == "Resilient Org"
        assert result.github_installation_ids == ["fail-inst"]

        # Verify org exists in DB
        from sqlalchemy import select

        from src.shared.models.organization import Organization

        db_result = await db_session.execute(select(Organization).where(Organization.id == result.id))
        org = db_result.scalar_one_or_none()
        assert org is not None
        assert org.name == "Resilient Org"
