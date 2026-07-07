#!/usr/bin/env python3
"""Backfill member_org_ids to DDB identity rows for all users with TenantMembership.

Issue #3134: One-off script to run after deploying the cross-tenant trigger
policy enforcement. For every user who has TenantMembership rows, writes their
member_org_ids list to their DDB identity rows (both old table and v2 table).

Run this BEFORE any tenant flips trigger_policy to "home_tenant_only".

Usage:
    # Dry-run (shows what would be updated):
    python backfill_member_org_ids.py --dry-run

    # Execute backfill:
    python backfill_member_org_ids.py

    # Against a specific environment:
    IDENTITY_INDEX_TABLE=adp-prod-identity-index \
    USER_IDENTITY_INDEX_TABLE=adp-prod-user-identity-index \
    DATABASE_URL=postgresql+asyncpg://... \
    python backfill_member_org_ids.py

Environment variables:
    DATABASE_URL: Postgres connection string (required)
    IDENTITY_INDEX_TABLE: DDB table name (default: adp-dev-identity-index)
    USER_IDENTITY_INDEX_TABLE: DDB v2 table name (default: adp-dev-user-identity-index)
    AWS_REGION: AWS region (default: us-east-1)
"""

import argparse
import asyncio
import logging
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Configuration from environment
IDENTITY_INDEX_TABLE = os.environ.get("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
USER_IDENTITY_INDEX_TABLE = os.environ.get("USER_IDENTITY_INDEX_TABLE", "adp-dev-user-identity-index")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DATABASE_URL = os.environ.get("DATABASE_URL", "")


async def get_users_with_memberships(db_url: str) -> list[dict]:
    """Query Postgres for all users with their membership org_ids and GitHub identity."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(db_url)

    query = text("""
        SELECT
            ui.provider_user_id,
            ui.provider,
            array_agg(DISTINCT tm.tenant_id) as member_org_ids
        FROM user_identities ui
        JOIN tenant_memberships tm ON tm.user_id = ui.user_id
        WHERE ui.provider = 'github'
        GROUP BY ui.provider_user_id, ui.provider
        HAVING count(DISTINCT tm.tenant_id) > 0
    """)

    async with engine.connect() as conn:
        result = await conn.execute(query)
        rows = result.fetchall()

    await engine.dispose()

    return [
        {
            "provider_user_id": row[0],
            "provider": row[1],
            "member_org_ids": list(row[2]),
        }
        for row in rows
    ]


def update_old_table(client, provider_user_id: str, member_org_ids: list[str], dry_run: bool) -> bool:
    """Update member_org_ids on the old identity-index table."""
    key = {
        "identity_type": {"S": "github_user"},
        "identity_value": {"S": provider_user_id},
    }
    expression_values = {
        ":orgs": {"L": [{"S": oid} for oid in member_org_ids]},
        ":now": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    }

    if dry_run:
        logger.info("[DRY-RUN] Would update old table: github_user|%s → member_org_ids=%s", provider_user_id, member_org_ids)
        return True

    try:
        client.update_item(
            TableName=IDENTITY_INDEX_TABLE,
            Key=key,
            UpdateExpression="SET member_org_ids = :orgs, updated_at = :now",
            ExpressionAttributeValues=expression_values,
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ValidationException":
            # Row doesn't exist — skip (user may not have old-table row)
            logger.warning("Old table row not found for github_user|%s — skipping", provider_user_id)
            return False
        raise


def update_new_table(client, provider_user_id: str, member_org_ids: list[str], dry_run: bool) -> bool:
    """Update member_org_ids on the v2 user-identity-index table."""
    key = {
        "provider": {"S": "github"},
        "provider_user_id": {"S": provider_user_id},
    }
    expression_values = {
        ":orgs": {"L": [{"S": oid} for oid in member_org_ids]},
        ":now": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    }

    if dry_run:
        logger.info("[DRY-RUN] Would update v2 table: github|%s → member_org_ids=%s", provider_user_id, member_org_ids)
        return True

    try:
        client.update_item(
            TableName=USER_IDENTITY_INDEX_TABLE,
            Key=key,
            UpdateExpression="SET member_org_ids = :orgs, updated_at = :now",
            ExpressionAttributeValues=expression_values,
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ValidationException":
            logger.warning("V2 table row not found for github|%s — skipping", provider_user_id)
            return False
        raise


async def main():
    parser = argparse.ArgumentParser(description="Backfill member_org_ids to DDB identity rows")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be updated without making changes")
    args = parser.parse_args()

    if not DATABASE_URL:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    logger.info("Starting member_org_ids backfill (dry_run=%s)", args.dry_run)
    logger.info("  Old table: %s", IDENTITY_INDEX_TABLE)
    logger.info("  V2 table:  %s", USER_IDENTITY_INDEX_TABLE)
    logger.info("  Region:    %s", AWS_REGION)

    # Fetch all users with memberships from Postgres
    users = await get_users_with_memberships(DATABASE_URL)
    logger.info("Found %d users with TenantMembership rows", len(users))

    if not users:
        logger.info("Nothing to backfill — exiting")
        return

    # Create DDB client
    ddb_client = boto3.client("dynamodb", region_name=AWS_REGION)

    # Process each user
    success_count = 0
    error_count = 0

    for user in users:
        provider_user_id = user["provider_user_id"]
        member_org_ids = user["member_org_ids"]

        try:
            update_old_table(ddb_client, provider_user_id, member_org_ids, args.dry_run)
            update_new_table(ddb_client, provider_user_id, member_org_ids, args.dry_run)
            success_count += 1
        except Exception:
            logger.exception("Failed to backfill user %s", provider_user_id)
            error_count += 1

        # Throttle to avoid DDB throughput issues
        if not args.dry_run and success_count % 25 == 0:
            await asyncio.sleep(0.1)

    logger.info(
        "Backfill complete: %d succeeded, %d failed, %d total",
        success_count,
        error_count,
        len(users),
    )


if __name__ == "__main__":
    asyncio.run(main())
