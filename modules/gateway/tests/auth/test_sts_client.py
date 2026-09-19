"""
Unit tests for the STS Client module.

These tests cover AWS STS integration with proper mocking to avoid
actual AWS API calls during testing.
"""

import logging
from typing import Any
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


@pytest.mark.unit
class TestBrokeredWorkspaceAssume:
    """Issue #5051 (U16a): the brokered workspace assume sends a per-tenant ExternalId.

    The ExternalId is the shared secret in the tenant's role trust policy. Assuming
    without it succeeds against any role whose policy omits the condition, which is the
    confused-deputy exposure these tests exist to keep closed — so "the parameter was
    sent" and "no value means refusal" are both asserted, not just the happy path.

    These tests prove the parameter reaches ``AssumeRole``. They do NOT prove AWS
    enforces the condition; that is deferred live criterion U16a-L1.
    """

    @staticmethod
    def _client_with_stub() -> tuple[STSClient, MagicMock]:
        """An STSClient whose boto3 STS client is a stub we can inspect."""
        stub = MagicMock()
        stub.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "ASIAIOSFODNN7EXAMPLE",
                "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "SessionToken": "AQoDYXdzEJr...",
                "Expiration": "2024-02-12T19:00:00Z",
            }
        }
        with patch("boto3.client", return_value=stub):
            client = STSClient()
        return client, stub

    @staticmethod
    def _stored_account(external_id: str | None = "11111111-1111-1111-1111-111111111111") -> dict[str, Any]:
        """The record shape ``connect_start`` writes to Secrets Manager."""
        record: dict[str, Any] = {
            "role_arn": "arn:aws:iam::123456789012:role/ADP-Agent-tenant-a",
            "account_id": "123456789012",
            "default_region": "us-east-1",
        }
        if external_id is not None:
            record["external_id"] = external_id
        return record

    @pytest.mark.asyncio
    async def test_external_id_from_stored_metadata_reaches_assume_role(self):
        """The stored ExternalId is sent as the ExternalId parameter, verbatim."""
        client, stub = self._client_with_stub()
        stored = self._stored_account()

        credentials = await client.assume_workspace_role(stored, "adp-broker-session", user_id="user-a", agent_id="developer", task_id="task-a")

        stub.assume_role.assert_called_once()
        params = stub.assume_role.call_args.kwargs
        assert params["ExternalId"] == stored["external_id"]
        assert params["RoleArn"] == stored["role_arn"]
        assert params["RoleSessionName"] == "adp-broker-session"
        assert credentials["AccessKeyId"] == "ASIAIOSFODNN7EXAMPLE"

    @pytest.mark.asyncio
    async def test_two_tenants_produce_two_different_external_ids(self):
        """Per-tenant, not a shared constant: two records send two distinct values."""
        client, stub = self._client_with_stub()
        tenant_a = self._stored_account("11111111-1111-1111-1111-111111111111")
        tenant_b = self._stored_account("22222222-2222-2222-2222-222222222222")
        tenant_b["account_id"] = "210987654321"
        tenant_b["role_arn"] = "arn:aws:iam::210987654321:role/ADP-Agent-tenant-b"

        await client.assume_workspace_role(tenant_a, "session-a", user_id="user-a", agent_id="developer", task_id="task-a")
        await client.assume_workspace_role(tenant_b, "session-b", user_id="user-a", agent_id="developer", task_id="task-a")

        sent = [call.kwargs["ExternalId"] for call in stub.assume_role.call_args_list]
        assert sent == [tenant_a["external_id"], tenant_b["external_id"]]
        assert sent[0] != sent[1]

    @pytest.mark.asyncio
    async def test_missing_external_id_is_refused_not_assumed_without_it(self):
        """No stored value → refuse. AssumeRole must not be called at all."""
        client, stub = self._client_with_stub()

        with pytest.raises(STSClientError) as exc_info:
            await client.assume_workspace_role(
                self._stored_account(external_id=None), "adp-broker-session", user_id="user-a", agent_id="developer", task_id="task-a"
            )

        assert exc_info.value.details["error_type"] == "missing_external_id"
        stub.assume_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_blank_external_id_is_refused(self):
        """An empty stored value is absence, not a valid condition to send."""
        client, stub = self._client_with_stub()

        with pytest.raises(STSClientError) as exc_info:
            await client.assume_workspace_role(
                self._stored_account(external_id="   "), "adp-broker-session", user_id="user-a", agent_id="developer", task_id="task-a"
            )

        assert exc_info.value.details["error_type"] == "missing_external_id"
        stub.assume_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_role_arn_is_refused(self):
        """A record with no target ARN is a bookkeeping gap, reported as one."""
        client, stub = self._client_with_stub()
        stored = self._stored_account()
        del stored["role_arn"]

        with pytest.raises(STSClientError) as exc_info:
            await client.assume_workspace_role(stored, "adp-broker-session", user_id="user-a", agent_id="developer", task_id="task-a")

        assert exc_info.value.details["error_type"] == "missing_role_arn"
        stub.assume_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_only_credentials_not_the_role_arn(self):
        """The brokered caller gets a session, not a role ARN it could re-assume."""
        client, _ = self._client_with_stub()

        credentials = await client.assume_workspace_role(
            self._stored_account(), "adp-broker-session", user_id="user-a", agent_id="developer", task_id="task-a"
        )

        assert set(credentials) == {"AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration"}
        assert "role_arn" not in credentials
        assert "RoleArn" not in credentials

    @pytest.mark.asyncio
    async def test_assume_returning_no_credentials_raises(self):
        """A response without Credentials must not be handed back as a session."""
        client, stub = self._client_with_stub()
        stub.assume_role.return_value = {"AssumedRoleUser": {"Arn": "arn:aws:sts::123456789012:assumed-role/r/s"}}

        with pytest.raises(STSClientError) as exc_info:
            await client.assume_workspace_role(self._stored_account(), "adp-broker-session", user_id="user-a", agent_id="developer", task_id="task-a")

        assert exc_info.value.details["error_type"] == "no_credentials_returned"

    @pytest.mark.asyncio
    async def test_access_denied_is_surfaced_with_the_role_arn(self):
        """A trust-policy rejection reports the role, so an operator can act on it."""
        client, stub = self._client_with_stub()
        stub.assume_role.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Not authorized to perform sts:AssumeRole"}}, "AssumeRole"
        )
        stored = self._stored_account()

        with pytest.raises(STSClientError) as exc_info:
            await client.assume_workspace_role(stored, "adp-broker-session", user_id="user-a", agent_id="developer", task_id="task-a")

        assert exc_info.value.details["error_type"] == "assume_role_failed"
        assert exc_info.value.details["role_arn"] == stored["role_arn"]

    @pytest.mark.asyncio
    async def test_external_id_is_never_logged(self, caplog):
        """The refusal path names the role; it must not echo the secret it lacks."""
        client, stub = self._client_with_stub()
        stub.assume_role.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "AssumeRole")
        stored = self._stored_account("s3cr3t-external-id-value")

        with caplog.at_level(logging.DEBUG), pytest.raises(STSClientError):
            await client.assume_workspace_role(stored, "adp-broker-session", user_id="user-a", agent_id="developer", task_id="task-a")

        assert "s3cr3t-external-id-value" not in caplog.text


@pytest.mark.unit
class TestAssumeRoleExternalIdRegression:
    """The added parameter must not change behaviour for callers that pass none."""

    @pytest.mark.asyncio
    async def test_no_external_id_sends_no_external_id_parameter(self):
        """Roles trusting ADP unconditionally keep working: no key, not an empty one.

        An empty ``ExternalId`` is not equivalent to omitting it — AWS rejects the
        empty string — so a caller that passes nothing must produce a call with no
        such key.
        """
        stub = MagicMock()
        stub.assume_role.return_value = {"Credentials": {"AccessKeyId": "AKIA"}}
        with patch("boto3.client", return_value=stub):
            client = STSClient()

        await client.assume_role("arn:aws:iam::123456789012:role/legacy", "legacy-session")

        params = stub.assume_role.call_args.kwargs
        assert "ExternalId" not in params
        assert params == {
            "RoleArn": "arn:aws:iam::123456789012:role/legacy",
            "RoleSessionName": "legacy-session",
            "DurationSeconds": 3600,
        }

    @pytest.mark.asyncio
    async def test_positional_callers_are_unaffected(self):
        """The signature stayed backward-compatible for positional call sites."""
        stub = MagicMock()
        stub.assume_role.return_value = {"Credentials": {"AccessKeyId": "AKIA"}}
        with patch("boto3.client", return_value=stub):
            client = STSClient()

        await client.assume_role("arn:aws:iam::123456789012:role/legacy", "legacy-session", 900)

        assert stub.assume_role.call_args.kwargs["DurationSeconds"] == 900
        assert "ExternalId" not in stub.assume_role.call_args.kwargs

    @pytest.mark.asyncio
    async def test_explicit_external_id_is_sent(self):
        """The low-level path accepts an ExternalId directly for non-brokered uses."""
        stub = MagicMock()
        stub.assume_role.return_value = {"Credentials": {"AccessKeyId": "AKIA"}}
        with patch("boto3.client", return_value=stub):
            client = STSClient()

        await client.assume_role("arn:aws:iam::123456789012:role/r", "s", external_id="ext-123")

        assert stub.assume_role.call_args.kwargs["ExternalId"] == "ext-123"
