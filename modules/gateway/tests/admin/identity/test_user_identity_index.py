"""Tests for UserIdentityIndexClient (new DDB table).

Issue #537: Identity projection redesign — user-identity-index DDB client.
"""

import time
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from src.admin.identity.user_identity_index import UserIdentityIndexClient


class TestUserIdentityIndexClient:
    """Unit tests for the UserIdentityIndexClient."""

    @pytest.fixture
    def mock_dynamodb(self):
        """Create a mock DynamoDB client."""
        client = MagicMock()
        client.put_item = MagicMock(return_value={})
        client.get_item = MagicMock(return_value={})
        client.delete_item = MagicMock(return_value={})
        return client

    @pytest.fixture
    def index_client(self, mock_dynamodb):
        """Create a UserIdentityIndexClient with mocked DynamoDB."""
        return UserIdentityIndexClient(
            table_name="adp-dev-user-identity-index",
            dynamodb_client=mock_dynamodb,
        )

    @pytest.mark.asyncio
    async def test_put_user_identity_success(self, index_client, mock_dynamodb):
        """Test successful put with correct item shape."""
        result = await index_client.put_user_identity(
            provider="github",
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
            provider_username="testuser",
        )
        assert result is True
        mock_dynamodb.put_item.assert_called_once()
        call_args = mock_dynamodb.put_item.call_args
        assert call_args[1]["TableName"] == "adp-dev-user-identity-index"
        item = call_args[1]["Item"]
        assert item["provider"]["S"] == "github"
        assert item["provider_user_id"]["S"] == "12345"
        assert item["user_id"]["S"] == "user-001"
        assert item["org_id"]["S"] == "org-001"
        assert item["provider_username"]["S"] == "testuser"
        # Identity rows are authoritative; default no TTL.
        # Offboarding deletes rows explicitly. Caller can opt in via ttl_seconds > 0.
        assert "ttl" not in item
        assert "updated_at" in item

    @pytest.mark.asyncio
    async def test_put_user_identity_ttl_opt_in(self, index_client, mock_dynamodb):
        """TTL is set when caller explicitly opts in with ttl_seconds > 0."""
        await index_client.put_user_identity(
            provider="github",
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
            ttl_seconds=3600,  # 1 hour, opt-in
        )
        item = mock_dynamodb.put_item.call_args[1]["Item"]
        ttl_value = int(item["ttl"]["N"])
        expected_min = int(time.time()) + 3600 - 5
        expected_max = int(time.time()) + 3600 + 5
        assert expected_min <= ttl_value <= expected_max

    @pytest.mark.asyncio
    async def test_put_user_identity_no_username(self, index_client, mock_dynamodb):
        """Provider username is omitted when None."""
        await index_client.put_user_identity(
            provider="slack",
            provider_user_id="U012345",
            user_id="user-002",
            org_id="org-001",
        )
        item = mock_dynamodb.put_item.call_args[1]["Item"]
        assert "provider_username" not in item

    @pytest.mark.asyncio
    async def test_put_rejects_invalid_provider(self, index_client):
        """Put raises ValueError for unsupported provider."""
        with pytest.raises(ValueError, match="Unsupported provider"):
            await index_client.put_user_identity(
                provider="telegram",
                provider_user_id="999",
                user_id="user-001",
                org_id="org-001",
            )

    @pytest.mark.asyncio
    async def test_put_retries_on_failure(self, index_client, mock_dynamodb):
        """Put retries on transient DDB errors."""
        error_response = {"Error": {"Code": "InternalServerError", "Message": "Service unavailable"}}
        mock_dynamodb.put_item.side_effect = [
            ClientError(error_response, "PutItem"),
            {},  # Success on 2nd attempt
        ]
        result = await index_client.put_user_identity(
            provider="github",
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
        )
        assert result is True
        assert mock_dynamodb.put_item.call_count == 2

    @pytest.mark.asyncio
    async def test_put_exhausts_retries(self, index_client, mock_dynamodb):
        """Put returns False after exhausting retries."""
        error_response = {"Error": {"Code": "InternalServerError", "Message": "Service unavailable"}}
        mock_dynamodb.put_item.side_effect = ClientError(error_response, "PutItem")
        result = await index_client.put_user_identity(
            provider="github",
            provider_user_id="12345",
            user_id="user-001",
            org_id="org-001",
        )
        assert result is False
        assert mock_dynamodb.put_item.call_count == 3

    @pytest.mark.asyncio
    async def test_get_user_identity_found(self, index_client, mock_dynamodb):
        """Get returns item dict when found."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "provider": {"S": "github"},
                "provider_user_id": {"S": "12345"},
                "user_id": {"S": "user-001"},
                "org_id": {"S": "org-001"},
                "provider_username": {"S": "testuser"},
                "updated_at": {"S": "2026-05-08T00:00:00Z"},
                "ttl": {"N": "1000000"},
            }
        }
        result = await index_client.get_user_identity(provider="github", provider_user_id="12345")
        assert result is not None
        assert result["user_id"] == "user-001"
        assert result["org_id"] == "org-001"
        assert result["provider_username"] == "testuser"

    @pytest.mark.asyncio
    async def test_get_user_identity_not_found(self, index_client, mock_dynamodb):
        """Get returns None when item not found."""
        mock_dynamodb.get_item.return_value = {}
        result = await index_client.get_user_identity(provider="github", provider_user_id="99999")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_user_identity_error(self, index_client, mock_dynamodb):
        """Get returns None on DDB error."""
        error_response = {"Error": {"Code": "InternalServerError", "Message": "fail"}}
        mock_dynamodb.get_item.side_effect = ClientError(error_response, "GetItem")
        result = await index_client.get_user_identity(provider="github", provider_user_id="12345")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_user_identity_success(self, index_client, mock_dynamodb):
        """Delete returns True on success."""
        result = await index_client.delete_user_identity(provider="github", provider_user_id="12345")
        assert result is True
        mock_dynamodb.delete_item.assert_called_once()
        key = mock_dynamodb.delete_item.call_args[1]["Key"]
        assert key["provider"]["S"] == "github"
        assert key["provider_user_id"]["S"] == "12345"

    @pytest.mark.asyncio
    async def test_delete_retries_on_failure(self, index_client, mock_dynamodb):
        """Delete retries on transient errors."""
        error_response = {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Throttled"}}
        mock_dynamodb.delete_item.side_effect = [
            ClientError(error_response, "DeleteItem"),
            {},  # Success on 2nd attempt
        ]
        result = await index_client.delete_user_identity(provider="slack", provider_user_id="U999")
        assert result is True
        assert mock_dynamodb.delete_item.call_count == 2
