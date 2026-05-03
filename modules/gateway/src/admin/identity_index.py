"""Identity Index DynamoDB client for tenant-identity Phase A.

Issue #375: Write-through client that maintains the identity-index DynamoDB table
alongside Postgres. Each identity (installation_id or client_id) maps to an org_id,
enabling O(1) lookups from webhook-ingress and token-generation Lambdas.

Pattern: best-effort write-through with exponential backoff retry + TTL backstop.
Postgres remains the authoritative source; DDB is a read-optimized projection.
"""

import asyncio
import logging
import os
import time
from typing import Literal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

IdentityType = Literal["github_installation_id", "cognito_client_id", "github_user"]

# Default TTL: 7 days (reconcile job refreshes before expiry)
DEFAULT_TTL_SECONDS = 7 * 86400

# Retry config
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 0.5


def _get_table_name() -> str:
    """Get the identity-index table name from environment."""
    return os.environ.get("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")


class IdentityIndexClient:
    """DynamoDB client for the identity-index table.

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

    async def put_identity(
        self,
        identity_type: "IdentityType | str",
        identity_value: str,
        org_id: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        extra_attrs: dict[str, str | None] | None = None,
    ) -> bool:
        """Write an identity mapping to DynamoDB with retry.

        Args:
            extra_attrs: Optional dict of additional string attributes to store
                         (e.g. user_id, provider_username). None values are skipped.

        Returns True if write succeeded, False if all retries exhausted.
        """
        item = {
            "identity_type": {"S": identity_type},
            "identity_value": {"S": identity_value},
            "org_id": {"S": org_id},
            "ttl": {"N": str(int(time.time()) + ttl_seconds)},
            "updated_at": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        }

        # Add extra attributes (skip None values)
        if extra_attrs:
            for key, value in extra_attrs.items():
                if value is not None:
                    item[key] = {"S": value}

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
                    "identity-index put_item failed (attempt %d/%d): %s. Retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    e.response["Error"]["Message"],
                    wait,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(wait)

        logger.error(
            "identity-index put_item exhausted retries: type=%s value=%s org=%s",
            identity_type,
            identity_value,
            org_id,
        )
        return False

    async def delete_identity(
        self,
        identity_type: IdentityType,
        identity_value: str,
    ) -> bool:
        """Delete an identity mapping from DynamoDB with retry.

        Returns True if delete succeeded, False if all retries exhausted.
        """
        key = {
            "identity_type": {"S": identity_type},
            "identity_value": {"S": identity_value},
        }

        for attempt in range(MAX_RETRIES):
            try:
                await asyncio.to_thread(
                    self._client.delete_item,
                    TableName=self._table_name,
                    Key=key,
                )
                return True
            except ClientError as e:
                wait = BASE_BACKOFF_SECONDS * (2**attempt)
                logger.warning(
                    "identity-index delete_item failed (attempt %d/%d): %s. Retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    e.response["Error"]["Message"],
                    wait,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(wait)

        logger.error(
            "identity-index delete_item exhausted retries: type=%s value=%s",
            identity_type,
            identity_value,
        )
        return False

    async def sync_identities_for_org(
        self,
        org_id: str,
        github_installation_ids: list[str],
        cognito_client_ids: list[str],
        old_github_installation_ids: list[str] | None = None,
        old_cognito_client_ids: list[str] | None = None,
    ) -> None:
        """Sync all identities for an org: upsert new, delete removed.

        Used by create (old lists empty) and update (diff against old lists).
        Best-effort — failures are logged but don't propagate.
        """
        old_github = set(old_github_installation_ids or [])
        old_cognito = set(old_cognito_client_ids or [])
        new_github = set(github_installation_ids)
        new_cognito = set(cognito_client_ids)

        tasks = []

        # Upsert new/changed GitHub installation IDs
        for iid in new_github:
            tasks.append(self.put_identity("github_installation_id", iid, org_id))

        # Upsert new/changed Cognito client IDs
        for cid in new_cognito:
            tasks.append(self.put_identity("cognito_client_id", cid, org_id))

        # Delete removed GitHub installation IDs
        for iid in old_github - new_github:
            tasks.append(self.delete_identity("github_installation_id", iid))

        # Delete removed Cognito client IDs
        for cid in old_cognito - new_cognito:
            tasks.append(self.delete_identity("cognito_client_id", cid))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failures = sum(1 for r in results if r is False or isinstance(r, Exception))
            if failures:
                logger.warning(
                    "identity-index sync for org %s: %d/%d operations failed",
                    org_id,
                    failures,
                    len(tasks),
                )

    async def delete_all_for_org(
        self,
        github_installation_ids: list[str],
        cognito_client_ids: list[str],
    ) -> None:
        """Delete all identity entries for an org (used on org deletion)."""
        tasks = []
        for iid in github_installation_ids:
            tasks.append(self.delete_identity("github_installation_id", iid))
        for cid in cognito_client_ids:
            tasks.append(self.delete_identity("cognito_client_id", cid))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
