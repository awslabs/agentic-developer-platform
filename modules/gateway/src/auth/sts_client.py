"""
AWS STS Client integration for the Bedrock Gateway Authentication module.

This module provides a secure wrapper around boto3 STS operations with proper
error handling, retry logic, and mocking support for testing.
"""

import asyncio
import logging
import re
from collections.abc import Mapping
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from src.shared.config import get_settings

from .exceptions import STSClientError
from .schemas import AWSCallerIdentity

logger = logging.getLogger(__name__)


class STSClient:
    """
    AWS STS client with error handling, retries, and async support.

    Supports:
    - GetCallerIdentity operations
    - Proper error handling and retry logic
    - Configurable timeout and retry settings
    - Async wrapper around synchronous boto3 operations
    - Mocking support for testing
    """

    def __init__(self, mock_responses: dict[str, Any] | None = None):
        """
        Initialize the STS client.

        Args:
            mock_responses: Optional mock responses for testing. Format:
                {
                    "get_caller_identity": {
                        "UserId": "AIDACKCEVSQ6C2EXAMPLE",
                        "Account": "123456789012",
                        "Arn": "arn:aws:iam::123456789012:user/john.doe"
                    }
                }
        """
        self.settings = get_settings()
        self.mock_responses = mock_responses if mock_responses is not None else {}

        # Configure boto3 with retries and timeouts
        self.config = Config(
            region_name=self.settings.aws_region, retries={"max_attempts": 3, "mode": "adaptive"}, read_timeout=30, connect_timeout=10
        )

        # Initialize the STS client only if mock_responses was not provided
        # (None means not mocking, {} means mocking with empty responses)
        self._client = None
        if mock_responses is None:
            try:
                self._client = boto3.client("sts", config=self.config)
                logger.info("STS client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize STS client: {e}")
                raise STSClientError(f"Failed to initialize STS client: {str(e)}")

    async def get_caller_identity(
        self, aws_access_key_id: str, aws_secret_access_key: str, aws_session_token: str | None = None
    ) -> AWSCallerIdentity:
        """
        Get caller identity using provided AWS credentials.

        Args:
            aws_access_key_id: AWS access key ID
            aws_secret_access_key: AWS secret access key
            aws_session_token: Optional AWS session token for temporary credentials

        Returns:
            AWSCallerIdentity: Parsed caller identity information

        Raises:
            STSClientError: If the operation fails
        """
        # Use mock response if available
        if self.mock_responses and "get_caller_identity" in self.mock_responses:
            mock_response = self.mock_responses["get_caller_identity"]
            logger.debug("Using mock response for get_caller_identity")
            return AWSCallerIdentity(user_id=mock_response.get("UserId"), account=mock_response["Account"], arn=mock_response["Arn"])

        # Create a temporary STS client with the provided credentials
        try:
            temp_client = boto3.client(
                "sts",
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                aws_session_token=aws_session_token,
                config=self.config,
            )

            # Execute the operation in a thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, temp_client.get_caller_identity)

            logger.debug(f"GetCallerIdentity successful for account: {response.get('Account')}")

            return AWSCallerIdentity(user_id=response.get("UserId"), account=response["Account"], arn=response["Arn"])

        except NoCredentialsError as e:
            logger.warning(f"No credentials provided: {e}")
            raise STSClientError("Invalid or missing AWS credentials", details={"error_type": "no_credentials"})

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]

            logger.warning(f"STS ClientError: {error_code} - {error_message}")

            # Map specific AWS errors to appropriate exceptions
            if error_code in ("InvalidUserID.NotFound", "AccessDenied"):
                raise STSClientError(
                    "Invalid AWS credentials or insufficient permissions",
                    details={"error_code": error_code, "error_message": error_message, "error_type": "access_denied"},
                )
            elif error_code == "TokenRefreshRequired":
                raise STSClientError(
                    "AWS session token has expired", details={"error_code": error_code, "error_message": error_message, "error_type": "token_expired"}
                )
            else:
                raise STSClientError(
                    f"AWS STS operation failed: {error_message}",
                    details={"error_code": error_code, "error_message": error_message, "error_type": "client_error"},
                )

        except BotoCoreError as e:
            logger.error(f"STS BotoCoreError: {e}")
            raise STSClientError(f"AWS SDK error: {str(e)}", details={"error_type": "sdk_error"})

        except Exception as e:
            logger.error(f"Unexpected error in get_caller_identity: {e}")
            raise STSClientError(f"Unexpected error during STS operation: {str(e)}", details={"error_type": "unexpected_error"})

    async def assume_role(
        self,
        role_arn: str,
        role_session_name: str,
        duration_seconds: int = 3600,
        external_id: str | None = None,
        *,
        tags: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """
        Assume an AWS IAM role.

        Args:
            role_arn: ARN of the role to assume
            role_session_name: Session name for the assumed role
            duration_seconds: Duration of the session in seconds (default: 1 hour)
            external_id: Optional ExternalId for confused-deputy protection. When
                supplied it is sent as the ``ExternalId`` parameter, which a role
                whose trust policy carries an ``sts:ExternalId`` condition requires.
                Keyword-optional so the existing callers that assume roles trusting
                ADP unconditionally keep working unchanged; brokered workspace
                assumes must go through :meth:`assume_workspace_role`, which makes
                the value mandatory.

        Returns:
            Dict containing the assumed role credentials

        Raises:
            STSClientError: If the operation fails
        """
        # Use mock response if available
        if self.mock_responses and "assume_role" in self.mock_responses:
            logger.debug("Using mock response for assume_role")
            return self.mock_responses["assume_role"]

        if not self._client:
            raise STSClientError("STS client not initialized")

        params: dict[str, Any] = {
            "RoleArn": role_arn,
            "RoleSessionName": role_session_name,
            "DurationSeconds": duration_seconds,
        }
        if external_id:
            params["ExternalId"] = external_id
        if tags is not None:
            params["Tags"] = tags

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: self._client.assume_role(**params))

            logger.debug(f"AssumeRole successful for role: {role_arn}")
            return response

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            logger.warning("AssumeRole ClientError: %s", error_code)

            raise STSClientError(
                f"Failed to assume role {role_arn}: {error_code}",
                details={"error_code": error_code, "role_arn": role_arn, "error_type": "assume_role_failed"},
            )

        except Exception as e:
            logger.error("Unexpected error in assume_role (%s)", type(e).__name__)
            raise STSClientError("Unexpected error during assume role operation", details={"error_type": "unexpected_error", "role_arn": role_arn})

    async def assume_workspace_role(
        self,
        stored_account: Mapping[str, Any],
        role_session_name: str,
        duration_seconds: int = 3600,
        *,
        user_id: str,
        agent_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        """Broker an ADP-created workspace role using authorized stored metadata.

        Called after the credential endpoint resolves the principal and selects
        their credential. ExternalId and target account come only from that
        stored record. Session tags preserve the v1 role's required user binding.
        Imported and legacy generic roles retain their separate existing path.
        Returns only temporary credentials; no role or ExternalId is delivered.
        """
        role_arn = stored_account.get("role_arn")
        if not isinstance(role_arn, str) or not role_arn.strip():
            raise STSClientError("Stored cloud-account record has no role ARN", details={"error_type": "missing_role_arn"})
        account_id = stored_account.get("account_id")
        match = re.fullmatch(r"arn:aws(?:-us-gov|-cn)?:iam::([0-9]{12}):role/[^\s]+", role_arn)
        if not isinstance(account_id, str) or match is None or match.group(1) != account_id:
            raise STSClientError("Stored cloud-account role and account disagree", details={"error_type": "invalid_account_role"})
        external_id = stored_account.get("external_id")
        if not isinstance(external_id, str) or not external_id.strip():
            raise STSClientError("Stored cloud-account record has no ExternalId", details={"error_type": "missing_external_id"})
        if re.fullmatch(r"[A-Za-z0-9_+=,.@:/-]{2,1224}", external_id) is None:
            raise STSClientError("Stored ExternalId is malformed", details={"error_type": "invalid_external_id"})
        if any(not isinstance(value, str) or not value.strip() for value in (user_id, agent_id, task_id)):
            raise STSClientError("Broker requires resolved identity context", details={"error_type": "missing_identity"})
        response = await self.assume_role(
            role_arn=role_arn,
            role_session_name=role_session_name,
            duration_seconds=duration_seconds,
            external_id=external_id,
            tags=[
                {"Key": "adp:user_id", "Value": user_id},
                {"Key": "adp:agent_id", "Value": agent_id},
                {"Key": "adp:task_id", "Value": task_id},
                {"Key": "adp:persona", "Value": agent_id},
            ],
        )
        credentials = response.get("Credentials")
        required = ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")
        if not isinstance(credentials, dict) or any(not credentials.get(key) for key in required):
            raise STSClientError("AssumeRole returned incomplete credentials", details={"error_type": "no_credentials_returned"})
        return {key: credentials[key] for key in required}

    def get_account_id_from_arn(self, arn: str) -> str:
        """
        Extract AWS account ID from an ARN.

        Args:
            arn: AWS resource ARN

        Returns:
            str: AWS account ID

        Raises:
            STSClientError: If ARN format is invalid
        """
        try:
            # ARN format: arn:partition:service:region:account-id:resource
            arn_parts = arn.split(":")
            if len(arn_parts) < 5:
                raise ValueError("Invalid ARN format")

            account_id = arn_parts[4]
            if not account_id or not account_id.isdigit():
                raise ValueError("Invalid account ID in ARN")

            return account_id

        except (ValueError, IndexError) as e:
            logger.error(f"Failed to parse account ID from ARN '{arn}': {e}")
            raise STSClientError(f"Invalid ARN format: {arn}", details={"arn": arn, "error_type": "invalid_arn"})

    def is_service_role_arn(self, arn: str) -> bool:
        """
        Check if an ARN represents an IAM role (vs user).

        Args:
            arn: AWS resource ARN

        Returns:
            bool: True if the ARN is for an IAM role
        """
        try:
            # Service roles have ARN format: arn:aws:sts::account:assumed-role/RoleName/SessionName
            # IAM roles have ARN format: arn:aws:iam::account:role/RoleName
            return ":role/" in arn or ":assumed-role/" in arn
        except Exception:
            return False

    def extract_role_name_from_arn(self, arn: str) -> str | None:
        """
        Extract role name from an ARN.

        Args:
            arn: AWS resource ARN

        Returns:
            Optional[str]: Role name if extractable, None otherwise
        """
        try:
            if ":assumed-role/" in arn:
                # Format: arn:aws:sts::account:assumed-role/RoleName/SessionName
                parts = arn.split("/")
                if len(parts) >= 2:
                    return parts[-2]  # Second to last part is the role name
            elif ":role/" in arn:
                # Format: arn:aws:iam::account:role/RoleName
                parts = arn.split("/")
                if len(parts) >= 2:
                    return parts[-1]  # Last part is the role name

            return None

        except Exception:
            return None
