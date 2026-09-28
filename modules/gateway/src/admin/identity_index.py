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

# Identity rows are authoritative-from-Postgres projections; they live as long
# as the user is real. Offboarding deletes the row explicitly. We previously set
# a 7d TTL "backstop" assuming a reconcile job would refresh — that job doesn't
# run reliably, and rows were getting GC'd, breaking webhook routing for active
# users (#TBD bug). New writes do NOT set the ttl attribute.
DEFAULT_TTL_SECONDS = 0  # Sentinel: 0 = "do not write a ttl"

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
        member_org_ids: list[str] | None = None,
    ) -> bool:
        """Write an identity mapping to DynamoDB with retry.

        Args:
            extra_attrs: Optional dict of additional string attributes to store
                         (e.g. user_id, provider_username). None values are skipped.
            member_org_ids: Optional list of org_ids where the user has membership
                         (Issue #3134). Stored as a DDB List of Strings (SS).

        Returns True if write succeeded, False if all retries exhausted.
        """
        item = {
            "identity_type": {"S": identity_type},
            "identity_value": {"S": identity_value},
            "org_id": {"S": org_id},
            "updated_at": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        }
        if ttl_seconds > 0:
            item["ttl"] = {"N": str(int(time.time()) + ttl_seconds)}

        # Add extra attributes (skip None values)
        if extra_attrs:
            for key, value in extra_attrs.items():
                if value is not None:
                    item[key] = {"S": value}

        # Issue #3134: member_org_ids as a DDB List attribute
        if member_org_ids is not None:
            item["member_org_ids"] = {"L": [{"S": oid} for oid in member_org_ids]}

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

    async def update_installation_identity(
        self,
        identity_value: str,
        org_id: str,
        trigger_policy: str | None = None,
        min_author_association: str | None = None,
    ) -> bool:
        """Update an installation identity row using SET semantics (UpdateItem).

        Issue #3134 fix: Unlike put_identity (PutItem = full overwrite), this
        uses UpdateItem so that attrs not mentioned in the update expression
        (e.g. trigger_policy, min_author_association set by a prior admin action)
        are preserved. Callers that don't own the policy attrs should use this
        method instead of put_identity to avoid silently wiping policy.

        Always sets: org_id, updated_at.
        Conditionally sets: trigger_policy, min_author_association (only when provided).

        Returns True if update succeeded, False if all retries exhausted.
        """
        key = {
            "identity_type": {"S": "github_installation_id"},
            "identity_value": {"S": identity_value},
        }

        # Build dynamic update expression
        set_parts = ["org_id = :org", "updated_at = :now"]
        expression_values: dict = {
            ":org": {"S": org_id},
            ":now": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        }

        if trigger_policy is not None:
            set_parts.append("trigger_policy = :tp")
            expression_values[":tp"] = {"S": trigger_policy}
        if min_author_association is not None:
            set_parts.append("min_author_association = :ma")
            expression_values[":ma"] = {"S": min_author_association}

        update_expression = "SET " + ", ".join(set_parts)

        for attempt in range(MAX_RETRIES):
            try:
                await asyncio.to_thread(
                    self._client.update_item,
                    TableName=self._table_name,
                    Key=key,
                    UpdateExpression=update_expression,
                    ExpressionAttributeValues=expression_values,
                )
                return True
            except ClientError as e:
                wait = BASE_BACKOFF_SECONDS * (2**attempt)
                logger.warning(
                    "identity-index update_installation_identity failed (attempt %d/%d): %s. Retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    e.response["Error"]["Message"],
                    wait,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(wait)

        logger.error(
            "identity-index update_installation_identity exhausted retries: value=%s org=%s",
            identity_value,
            org_id,
        )
        return False

    async def update_membership_orgs(
        self,
        identity_type: "IdentityType | str",
        identity_value: str,
        member_org_ids: list[str],
    ) -> bool:
        """Update only the member_org_ids attribute on an existing identity row.

        Issue #3134: Targeted attribute update — uses UpdateItem to set
        member_org_ids without needing to know other attributes.

        Returns True if update succeeded, False if all retries exhausted.
        """
        key = {
            "identity_type": {"S": identity_type},
            "identity_value": {"S": identity_value},
        }
        expression_values = {
            ":orgs": {"L": [{"S": oid} for oid in member_org_ids]},
            ":now": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        }

        for attempt in range(MAX_RETRIES):
            try:
                await asyncio.to_thread(
                    self._client.update_item,
                    TableName=self._table_name,
                    Key=key,
                    UpdateExpression="SET member_org_ids = :orgs, updated_at = :now",
                    ExpressionAttributeValues=expression_values,
                )
                return True
            except ClientError as e:
                wait = BASE_BACKOFF_SECONDS * (2**attempt)
                logger.warning(
                    "identity-index update_membership_orgs failed (attempt %d/%d): %s. Retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    e.response["Error"]["Message"],
                    wait,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(wait)

        logger.error(
            "identity-index update_membership_orgs exhausted retries: type=%s value=%s",
            identity_type,
            identity_value,
        )
        return False

    async def update_user_identity_core(
        self,
        identity_value: str,
        user_id: str,
        org_id: str,
        provider_username: str | None = None,
    ) -> bool:
        """Update a github_user identity row using SET semantics (UpdateItem).

        Issue #3134 fix: Uses UpdateItem so that member_org_ids (set by
        membership write-through) is preserved when an unrelated identity
        operation re-writes the user row.

        Always sets: user_id, org_id, updated_at.
        Conditionally sets: provider_username (only when not None).

        Returns True if update succeeded, False if all retries exhausted.
        """
        key = {
            "identity_type": {"S": "github_user"},
            "identity_value": {"S": identity_value},
        }

        set_parts = ["user_id = :uid", "org_id = :org", "updated_at = :now"]
        expression_values: dict = {
            ":uid": {"S": user_id},
            ":org": {"S": org_id},
            ":now": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        }

        if provider_username is not None:
            set_parts.append("provider_username = :pun")
            expression_values[":pun"] = {"S": provider_username}

        update_expression = "SET " + ", ".join(set_parts)

        for attempt in range(MAX_RETRIES):
            try:
                await asyncio.to_thread(
                    self._client.update_item,
                    TableName=self._table_name,
                    Key=key,
                    UpdateExpression=update_expression,
                    ExpressionAttributeValues=expression_values,
                )
                return True
            except ClientError as e:
                wait = BASE_BACKOFF_SECONDS * (2**attempt)
                logger.warning(
                    "identity-index update_user_identity_core failed (attempt %d/%d): %s. Retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    e.response["Error"]["Message"],
                    wait,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(wait)

        logger.error(
            "identity-index update_user_identity_core exhausted retries: value=%s",
            identity_value,
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

        Issue #3134 fix: Uses update_installation_identity (UpdateItem) for
        github_installation_id rows so that trigger_policy/min_author_association
        attrs set by a prior admin action are NOT wiped by this sync.
        Cognito client IDs still use put_identity (no policy attrs on those rows).
        """
        old_github = set(old_github_installation_ids or [])
        old_cognito = set(old_cognito_client_ids or [])
        new_github = set(github_installation_ids)
        new_cognito = set(cognito_client_ids)

        tasks = []

        # Upsert new/changed GitHub installation IDs — UpdateItem preserves policy attrs
        for iid in new_github:
            tasks.append(self.update_installation_identity(identity_value=iid, org_id=org_id))

        # Upsert new/changed Cognito client IDs (no policy attrs to preserve)
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
