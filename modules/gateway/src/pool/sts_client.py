"""STS client wrapper for cross-account role assumption."""

import logging
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src.pool.config import BedrockAccountConfig, PoolSettings
from src.pool.exceptions import CredentialExpiredError, RoleAssumptionError
from src.pool.models import AssumedRoleCredentials

logger = logging.getLogger(__name__)


class STSClient:
    """Wrapper for AWS STS operations with credential caching."""

    def __init__(
        self,
        settings: PoolSettings | None = None,
        sts_client: Any | None = None,
    ):
        """Initialize the STS client.

        Args:
            settings: Pool settings for session configuration
            sts_client: Optional pre-configured STS client (for testing)
        """
        self._settings = settings or PoolSettings()
        self._sts_client = sts_client or boto3.client("sts")
        self._credential_cache: dict[str, AssumedRoleCredentials] = {}

    async def assume_role(
        self,
        account_config: BedrockAccountConfig,
        force_refresh: bool = False,
    ) -> AssumedRoleCredentials:
        """Assume a cross-account IAM role and return credentials.

        Args:
            account_config: Configuration for the account to assume role in
            force_refresh: If True, bypass the cache and fetch new credentials

        Returns:
            AssumedRoleCredentials with temporary credentials

        Raises:
            RoleAssumptionError: If the role assumption fails
        """
        cache_key = account_config.role_arn

        # Check cache first (unless force refresh)
        if not force_refresh and cache_key in self._credential_cache:
            cached = self._credential_cache[cache_key]
            if not cached.is_expired(margin_seconds=self._settings.credential_refresh_margin_seconds):
                logger.debug(f"Using cached credentials for {account_config.role_arn}")
                return cached

        # Need to fetch new credentials
        logger.info(f"Assuming role {account_config.role_arn} for account {account_config.account_id}")

        try:
            params = {
                "RoleArn": account_config.role_arn,
                "RoleSessionName": self._settings.sts_session_name,
                "DurationSeconds": self._settings.sts_session_duration_seconds,
            }

            if account_config.external_id:
                params["ExternalId"] = account_config.external_id

            response = self._sts_client.assume_role(**params)

            credentials = AssumedRoleCredentials(
                access_key_id=response["Credentials"]["AccessKeyId"],
                secret_access_key=response["Credentials"]["SecretAccessKey"],
                session_token=response["Credentials"]["SessionToken"],
                expiration=response["Credentials"]["Expiration"],
            )

            # Update cache
            self._credential_cache[cache_key] = credentials
            logger.debug(f"Successfully assumed role {account_config.role_arn}")

            return credentials

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            error_message = e.response.get("Error", {}).get("Message", str(e))
            logger.error(f"Failed to assume role {account_config.role_arn}: {error_code} - {error_message}")
            raise RoleAssumptionError(account_config.role_arn, f"{error_code}: {error_message}")
        except BotoCoreError as e:
            logger.error(f"BotoCore error assuming role {account_config.role_arn}: {e}")
            raise RoleAssumptionError(account_config.role_arn, str(e))

    async def get_cached_credentials(self, role_arn: str) -> AssumedRoleCredentials | None:
        """Get cached credentials for a role ARN if available and valid.

        Args:
            role_arn: The role ARN to look up

        Returns:
            Cached credentials if available and not expired, None otherwise
        """
        if role_arn not in self._credential_cache:
            return None

        cached = self._credential_cache[role_arn]
        if cached.is_expired(margin_seconds=self._settings.credential_refresh_margin_seconds):
            return None

        return cached

    async def refresh_credentials(
        self,
        account_config: BedrockAccountConfig,
    ) -> AssumedRoleCredentials:
        """Refresh credentials for an account, handling failures.

        Args:
            account_config: Configuration for the account

        Returns:
            Fresh credentials

        Raises:
            CredentialExpiredError: If credentials cannot be refreshed
        """
        try:
            return await self.assume_role(account_config, force_refresh=True)
        except RoleAssumptionError as e:
            logger.error(f"Failed to refresh credentials for {account_config.account_id}: {e}")
            raise CredentialExpiredError(account_config.account_id) from e

    def clear_cache(self, role_arn: str | None = None) -> None:
        """Clear cached credentials.

        Args:
            role_arn: Specific role ARN to clear, or None to clear all
        """
        if role_arn:
            self._credential_cache.pop(role_arn, None)
            logger.debug(f"Cleared credential cache for {role_arn}")
        else:
            self._credential_cache.clear()
            logger.debug("Cleared all credential cache")

    def get_cache_status(self) -> dict[str, dict[str, Any]]:
        """Get status of cached credentials.

        Returns:
            Dictionary mapping role ARNs to their credential status
        """
        result = {}
        now = datetime.now(UTC)

        for role_arn, creds in self._credential_cache.items():
            remaining_seconds = (creds.expiration - now).total_seconds()
            result[role_arn] = {
                "expires_at": creds.expiration.isoformat(),
                "remaining_seconds": max(0, int(remaining_seconds)),
                "is_valid": not creds.is_expired(margin_seconds=self._settings.credential_refresh_margin_seconds),
            }

        return result
