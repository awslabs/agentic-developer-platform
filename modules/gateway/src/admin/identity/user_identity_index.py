"""DynamoDB client for the user-identity-index table.

Issue #537: Identity projection redesign — new per-user lookup table.
PK = provider (S), SK = provider_user_id (S).
Attrs: user_id, org_id, provider_username, updated_at, ttl.

This is a thin write/read client. The IdentityIndexWriter orchestrates
dual-write logic (old table + this table) with feature-flag gating.
"""

import asyncio
import logging
import os
import time
from typing import TypedDict

import boto3
from botocore.exceptions import ClientError

from src.shared.identity.providers import SUPPORTED_PROVIDERS

logger = logging.getLogger(__name__)

# 30-day TTL (refreshed by 7d reconcile job — 23d headroom on failures)
DEFAULT_TTL_SECONDS = 30 * 86400

# Retry config (matches existing identity_index.py pattern)
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 0.5


class UserIdentityItem(TypedDict, total=False):
    """Shape of a user-identity-index DDB item."""

    provider: str
    provider_user_id: str
    user_id: str
    org_id: str
    provider_username: str | None
    updated_at: str
    ttl: int


def _get_table_name() -> str:
    """Get the user-identity-index table name from environment."""
    return os.environ.get("USER_IDENTITY_INDEX_TABLE", "adp-dev-user-identity-index")


class UserIdentityIndexClient:
    """DynamoDB client for the user-identity-index table.

    Write-through pattern: called after Postgres commits succeed.
    Failures are logged and retried but do not roll back Postgres.
    """

    def __init__(self, table_name: str | None = None, dynamodb_client=None):
        self._table_name = table_name or _get_table_name()
        self._client = dynamodb_client or boto3.client(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )

    @property
    def table_name(self) -> str:
        return self._table_name

    async def put_user_identity(
        self,
        provider: str,
        provider_user_id: str,
        user_id: str,
        org_id: str,
        provider_username: str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> bool:
        """Write a user identity to the new DDB table.

        PK=provider, SK=provider_user_id. Upsert semantics (PutItem overwrites).
        Returns True if write succeeded, False if all retries exhausted.
        """
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported provider: {provider!r}")

        item: dict = {
            "provider": {"S": provider},
            "provider_user_id": {"S": provider_user_id},
            "user_id": {"S": user_id},
            "org_id": {"S": org_id},
            "updated_at": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            "ttl": {"N": str(int(time.time()) + ttl_seconds)},
        }

        if provider_username is not None:
            item["provider_username"] = {"S": provider_username}

        for attempt in range(MAX_RETRIES):
            try:
                await asyncio.to_thread(
                    self._client.put_item,
                    TableName=self._table_name,
                    Item=item,
                )
                return True
            except ClientError as e:
                wait = BASE_BACKOFF_SECONDS * (2**attempt)
                logger.warning(
                    "user-identity-index put_item failed (attempt %d/%d): %s. Retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    e.response["Error"]["Message"],
                    wait,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(wait)

        logger.error(
            "user-identity-index put_item exhausted retries: provider=%s provider_user_id=%s",
            provider,
            provider_user_id,
        )
        return False

    async def get_user_identity(
        self,
        provider: str,
        provider_user_id: str,
    ) -> UserIdentityItem | None:
        """Read a user identity from the new DDB table.

        Returns the item dict or None if not found.
        """
        try:
            response = await asyncio.to_thread(
                self._client.get_item,
                TableName=self._table_name,
                Key={
                    "provider": {"S": provider},
                    "provider_user_id": {"S": provider_user_id},
                },
            )
            item = response.get("Item")
            if not item:
                return None
            return UserIdentityItem(
                provider=item["provider"]["S"],
                provider_user_id=item["provider_user_id"]["S"],
                user_id=item["user_id"]["S"],
                org_id=item["org_id"]["S"],
                provider_username=item.get("provider_username", {}).get("S"),
                updated_at=item.get("updated_at", {}).get("S", ""),
                ttl=int(item.get("ttl", {}).get("N", "0")),
            )
        except ClientError as e:
            logger.error(
                "user-identity-index get_item failed: provider=%s provider_user_id=%s: %s",
                provider,
                provider_user_id,
                e.response["Error"]["Message"],
            )
            return None

    async def delete_user_identity(
        self,
        provider: str,
        provider_user_id: str,
    ) -> bool:
        """Delete a user identity from the new DDB table.

        Returns True if delete succeeded, False if all retries exhausted.
        """
        for attempt in range(MAX_RETRIES):
            try:
                await asyncio.to_thread(
                    self._client.delete_item,
                    TableName=self._table_name,
                    Key={
                        "provider": {"S": provider},
                        "provider_user_id": {"S": provider_user_id},
                    },
                )
                return True
            except ClientError as e:
                wait = BASE_BACKOFF_SECONDS * (2**attempt)
                logger.warning(
                    "user-identity-index delete_item failed (attempt %d/%d): %s. Retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    e.response["Error"]["Message"],
                    wait,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(wait)

        logger.error(
            "user-identity-index delete_item exhausted retries: provider=%s provider_user_id=%s",
            provider,
            provider_user_id,
        )
        return False
