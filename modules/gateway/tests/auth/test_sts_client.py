"""
Unit tests for the STS Client module.

These tests cover AWS STS integration with proper mocking to avoid
actual AWS API calls during testing.
"""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from src.auth.exceptions import STSClientError
from src.auth.schemas import AWSCallerIdentity
from src.auth.sts_client import STSClient


@pytest.mark.unit
class TestSTSClient:
    """Test suite for STSClient."""

    def test_init_with_mock_responses(self):
        """Test STS client initialization with mock responses."""
        mock_responses = {"get_caller_identity": {"Account": "123456789012"}}
        client = STSClient(mock_responses=mock_responses)

        assert client.mock_responses == mock_responses
        assert client._client is None  # No real client when mocking

    def test_init_without_mocks(self):
        """Test STS client initialization without mocks."""
        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.return_value = MagicMock()
            client = STSClient()

            assert client.mock_responses == {}
            mock_boto_client.assert_called_once_with("sts", config=client.config)

    @pytest.mark.asyncio
    async def test_get_caller_identity_with_mock(self):
        """Test get_caller_identity with mock responses."""
        mock_responses = {
            "get_caller_identity": {"UserId": "AIDACKCEVSQ6C2EXAMPLE", "Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/test-user"}
        }
        client = STSClient(mock_responses=mock_responses)

        result = await client.get_caller_identity(aws_access_key_id="test-key", aws_secret_access_key="test-secret")

        assert isinstance(result, AWSCallerIdentity)
        assert result.user_id == "AIDACKCEVSQ6C2EXAMPLE"
        assert result.account == "123456789012"
        assert result.arn == "arn:aws:iam::123456789012:user/test-user"

    @pytest.mark.asyncio
    async def test_get_caller_identity_with_session_token(self):
        """Test get_caller_identity with session token."""
        mock_responses = {"get_caller_identity": {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/test-role/session"}}
        client = STSClient(mock_responses=mock_responses)

        result = await client.get_caller_identity(
            aws_access_key_id="test-key", aws_secret_access_key="test-secret", aws_session_token="test-session-token"
        )

        assert isinstance(result, AWSCallerIdentity)
        assert result.account == "123456789012"
        assert result.user_id is None  # No UserId in assumed role response

    @pytest.mark.asyncio
    async def test_get_caller_identity_no_credentials_error(self):
        """Test get_caller_identity with no credentials error."""
        client = STSClient()  # No mock responses

        with patch("boto3.client") as mock_boto_client:
            mock_client = MagicMock()
            mock_client.get_caller_identity.side_effect = NoCredentialsError()
            mock_boto_client.return_value = mock_client

            with pytest.raises(STSClientError) as exc_info:
                await client.get_caller_identity("", "")

            assert "Invalid or missing AWS credentials" in str(exc_info.value)
            assert exc_info.value.details["error_type"] == "no_credentials"

    @pytest.mark.asyncio
    async def test_get_caller_identity_access_denied(self):
        """Test get_caller_identity with access denied error."""
        client = STSClient()

        error_response = {"Error": {"Code": "AccessDenied", "Message": "User is not authorized to perform: sts:GetCallerIdentity"}}

        with patch("boto3.client") as mock_boto_client:
            mock_client = MagicMock()
            mock_client.get_caller_identity.side_effect = ClientError(error_response, "GetCallerIdentity")
            mock_boto_client.return_value = mock_client

            with pytest.raises(STSClientError) as exc_info:
                await client.get_caller_identity("invalid", "invalid")

            assert "Invalid AWS credentials or insufficient permissions" in str(exc_info.value)
            assert exc_info.value.details["error_type"] == "access_denied"

    @pytest.mark.asyncio
    async def test_get_caller_identity_token_expired(self):
        """Test get_caller_identity with token expired error."""
        client = STSClient()

        error_response = {"Error": {"Code": "TokenRefreshRequired", "Message": "The provided token is expired"}}

        with patch("boto3.client") as mock_boto_client:
            mock_client = MagicMock()
            mock_client.get_caller_identity.side_effect = ClientError(error_response, "GetCallerIdentity")
            mock_boto_client.return_value = mock_client

            with pytest.raises(STSClientError) as exc_info:
                await client.get_caller_identity("key", "secret", "expired-token")

            assert "AWS session token has expired" in str(exc_info.value)
            assert exc_info.value.details["error_type"] == "token_expired"

    @pytest.mark.asyncio
    async def test_get_caller_identity_generic_client_error(self):
        """Test get_caller_identity with generic client error."""
        client = STSClient()

        error_response = {"Error": {"Code": "InternalError", "Message": "An internal error occurred"}}

        with patch("boto3.client") as mock_boto_client:
            mock_client = MagicMock()
            mock_client.get_caller_identity.side_effect = ClientError(error_response, "GetCallerIdentity")
            mock_boto_client.return_value = mock_client

            with pytest.raises(STSClientError) as exc_info:
                await client.get_caller_identity("key", "secret")

            assert "AWS STS operation failed" in str(exc_info.value)
            assert exc_info.value.details["error_type"] == "client_error"

    @pytest.mark.asyncio
    async def test_get_caller_identity_botocore_error(self):
        """Test get_caller_identity with botocore error."""
        client = STSClient()

        with patch("boto3.client") as mock_boto_client:
            mock_client = MagicMock()
            mock_client.get_caller_identity.side_effect = BotoCoreError()
            mock_boto_client.return_value = mock_client

            with pytest.raises(STSClientError) as exc_info:
                await client.get_caller_identity("key", "secret")

            assert "AWS SDK error" in str(exc_info.value)
            assert exc_info.value.details["error_type"] == "sdk_error"

    @pytest.mark.asyncio
    async def test_get_caller_identity_unexpected_error(self):
        """Test get_caller_identity with unexpected error."""
        client = STSClient()

        with patch("boto3.client") as mock_boto_client:
            mock_client = MagicMock()
            mock_client.get_caller_identity.side_effect = Exception("Unexpected error")
            mock_boto_client.return_value = mock_client

            with pytest.raises(STSClientError) as exc_info:
                await client.get_caller_identity("key", "secret")

            assert "Unexpected error during STS operation" in str(exc_info.value)
            assert exc_info.value.details["error_type"] == "unexpected_error"

    @pytest.mark.asyncio
    async def test_assume_role_with_mock(self):
        """Test assume_role with mock responses."""
        mock_responses = {
            "assume_role": {
                "Credentials": {
                    "AccessKeyId": "ASIAIOSFODNN7EXAMPLE",
                    "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                    "SessionToken": "AQoDYXdzEJr...",
                    "Expiration": "2024-02-12T19:00:00Z",
                }
            }
        }
        client = STSClient(mock_responses=mock_responses)

        result = await client.assume_role("arn:aws:iam::123456789012:role/test-role", "test-session")

        assert result == mock_responses["assume_role"]

    @pytest.mark.asyncio
    async def test_assume_role_not_initialized(self):
        """Test assume_role when client is not initialized."""
        client = STSClient(mock_responses={})  # Mock responses but no actual client

        with pytest.raises(STSClientError) as exc_info:
            await client.assume_role("arn:aws:iam::123456789012:role/test-role", "test-session")

        assert "STS client not initialized" in str(exc_info.value)

    def test_get_account_id_from_arn(self):
        """Test extracting account ID from ARN."""
        client = STSClient()

        # Test valid ARNs
        assert client.get_account_id_from_arn("arn:aws:iam::123456789012:role/test-role") == "123456789012"
        assert client.get_account_id_from_arn("arn:aws:sts::987654321098:assumed-role/role/session") == "987654321098"

    def test_get_account_id_from_invalid_arn(self):
        """Test extracting account ID from invalid ARN."""
        client = STSClient()

        with pytest.raises(STSClientError) as exc_info:
            client.get_account_id_from_arn("invalid-arn")

        assert "Invalid ARN format" in str(exc_info.value)
        assert exc_info.value.details["error_type"] == "invalid_arn"

    def test_is_service_role_arn(self):
        """Test checking if ARN is a service role."""
        client = STSClient()

        # Service role ARNs
        assert client.is_service_role_arn("arn:aws:iam::123456789012:role/service-role") is True
        assert client.is_service_role_arn("arn:aws:sts::123456789012:assumed-role/role/session") is True

        # User ARN
        assert client.is_service_role_arn("arn:aws:iam::123456789012:user/username") is False

    def test_extract_role_name_from_arn(self):
        """Test extracting role name from ARN."""
        client = STSClient()

        # Test assumed role ARN
        assumed_role_arn = "arn:aws:sts::123456789012:assumed-role/TestRole/session-name"
        assert client.extract_role_name_from_arn(assumed_role_arn) == "TestRole"

        # Test IAM role ARN
        iam_role_arn = "arn:aws:iam::123456789012:role/TestRole"
        assert client.extract_role_name_from_arn(iam_role_arn) == "TestRole"

        # Test invalid ARN
        assert client.extract_role_name_from_arn("invalid-arn") is None

    def test_extract_role_name_from_user_arn(self):
        """Test extracting role name from user ARN (should return None)."""
        client = STSClient()

        user_arn = "arn:aws:iam::123456789012:user/username"
        assert client.extract_role_name_from_arn(user_arn) is None
