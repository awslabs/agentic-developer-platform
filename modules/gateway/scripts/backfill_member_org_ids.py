#!/usr/bin/env python3
"""Rebuild member_org_ids on DDB identity rows from Postgres truth.

Issue #3134 created this to backfill every user before the cross-tenant trigger
policy went live. Issue #4849 makes it the standing **reconciliation path** for
the projection: Postgres `tenant_memberships` is the source of truth, the DDB
`member_org_ids` attribute is a read-optimized copy, and the write-through in
`src/admin/memberships.py::project_member_org_ids` is deliberately best-effort
(it never raises, so a DDB fault cannot fail a membership mutation that already
committed). That design choice is only safe because this script exists: it is
how a projection that drifted — a failed write-through, a row created before
#3134, a direct SQL edit — gets repaired.

Reconciliation is idempotent and safe to re-run at any time. Both flows below
recompute the full org list from Postgres and overwrite; neither merges, so a
membership deleted in Postgres is also removed from the projection.

Usage:
    # Dry-run (shows what would be updated):
    python backfill_member_org_ids.py --dry-run

    # Reconcile every user (post-incident sweep, pre-cutover backfill):
    python backfill_member_org_ids.py

    # Reconcile ONE user — the targeted repair. Either key works:
    python backfill_member_org_ids.py --provider-user-id 1234567
    python backfill_member_org_ids.py --user-id 3f8c...-uuid

    # Against a specific environment:
    IDENTITY_INDEX_TABLE=adp-prod-identity-index \
    USER_IDENTITY_INDEX_TABLE=adp-prod-user-identity-index \
    DATABASE_URL=postgresql+asyncpg://... \
    python backfill_member_org_ids.py

Memberships count only through proven GitHub identity bindings, matching
project_member_org_ids. All identity keys remain repair targets, including
unproven or membershipless accounts: they receive an empty list so stale access
is removed. If every identity row was deleted, --provider-user-id explicitly
targets that orphaned projection for clearing; a sweep cannot discover a key
that no longer exists in Postgres.

Only existing DDB rows are updated. An absent row already satisfies an empty
clear; a nonempty projection with no identity row is incomplete and needs the
identity-owning provisioning path. Validation and operational failures exit
nonzero, including partial writes. Both tables are attempted and reported so
rerunning safely completes a partially successful clear.

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
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.shared.identity.verification import PROVEN_METHODS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Configuration from environment
IDENTITY_INDEX_TABLE = os.environ.get("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
USER_IDENTITY_INDEX_TABLE = os.environ.get("USER_IDENTITY_INDEX_TABLE", "adp-dev-user-identity-index")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DATABASE_URL = os.environ.get("DATABASE_URL", "")


async def get_users_with_memberships(
    db_url: str,
    *,
    provider_user_id: str | None = None,
    user_id: str | None = None,
) -> list[dict]:
    """Query Postgres for users with their membership org_ids and GitHub identity.

    With no filter, returns every GitHub identity key, even when no proven
    membership remains. `provider_user_id` / `user_id` select keys to repair.

    The grouping is by provider_user_id (not by the user_identities row) on
    purpose: `user_identities` is unique per (provider, provider_user_id, org_id),
    so one GitHub account legitimately has N rows — one per org. Grouping by the
    row would emit N partial org lists for the same DDB key and the last write
    would win with only one org in it. Grouping by the identity value aggregates
    every membership held through a proven identity into the list the key expects.
    (The aggregation happens in Python rather than via `array_agg` so the query
    is portable to the SQLite test suite; the emitted rows are identical.)

    The `user_id` filter is a semi-join rather than an extra WHERE on the joined
    rows for the same reason: it selects *which account* to rebuild, and the
    rebuild must still see that account's full membership set.
    """
    from sqlalchemy import bindparam, text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(db_url)

    params = {"proven_methods": sorted(PROVEN_METHODS)}
    filter_sql = ""
    if provider_user_id:
        filter_sql = "AND ui.provider_user_id = :provider_user_id"
        params["provider_user_id"] = provider_user_id
    elif user_id:
        filter_sql = """AND ui.provider_user_id IN (
                SELECT ui2.provider_user_id
                FROM user_identities ui2
                WHERE ui2.provider = 'github' AND ui2.user_id = :user_id
            )"""
        params["user_id"] = user_id

    query = text(f"""
        SELECT
            ui.provider_user_id,
            ui.provider,
            tm.tenant_id
        FROM user_identities ui
        LEFT JOIN tenant_memberships tm
          ON tm.user_id = ui.user_id AND ui.verification_method IN :proven_methods
        WHERE ui.provider = 'github'
        {filter_sql}
    """).bindparams(bindparam("proven_methods", expanding=True))

    async with engine.connect() as conn:
        result = await conn.execute(query, params)
        rows = result.fetchall()

    await engine.dispose()

    # Aggregate to one entry per (provider, provider_user_id) — the DDB key.
    # The outer join keeps empty/unproven keys in the repair set. Putting the
    # proof filter in WHERE instead would silently skip the stale lists that
    # most need clearing. An explicit account target also survives full removal.
    grouped: dict[tuple[str, str], set[str]] = {}
    if provider_user_id:
        grouped[(provider_user_id, "github")] = set()
    for row in rows:
        if not row[0]:
            continue
        org_ids = grouped.setdefault((row[0], row[1]), set())
        if row[2]:
            org_ids.add(row[2])

    return [
        {
            "provider_user_id": pid,
            "provider": provider,
            "member_org_ids": sorted(org_ids),
        }
        for (pid, provider), org_ids in grouped.items()
    ]


def update_old_table(client, provider_user_id: str, member_org_ids: list[str], dry_run: bool) -> bool:
    """Update an existing row; return False only when it is already absent."""
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
            ConditionExpression="attribute_exists(#pk) AND attribute_exists(#sk)",
            ExpressionAttributeNames={"#pk": "identity_type", "#sk": "identity_value"},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # The condition tests existence only. A schema/validation rejection
            # is a failed write, not evidence that the stale row is absent.
            return False
        raise


def update_new_table(client, provider_user_id: str, member_org_ids: list[str], dry_run: bool) -> bool:
    """Update an existing row; return False only when it is already absent."""
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
            ConditionExpression="attribute_exists(#pk) AND attribute_exists(#sk)",
            ExpressionAttributeNames={"#pk": "provider", "#sk": "provider_user_id"},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def main():
    parser = argparse.ArgumentParser(description="Rebuild member_org_ids on DDB identity rows from Postgres truth")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be updated without making changes")
    # Issue #4849: single-user reconciliation. Mutually exclusive because they
    # are two spellings of the same target, and accepting both would silently
    # honour one and ignore the other.
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--provider-user-id",
        help="Reconcile only this GitHub numeric user id (the projection's DDB key)",
    )
    target.add_argument(
        "--user-id",
        help="Reconcile only this ADP users.id UUID (resolved to its GitHub identities)",
    )
    args = parser.parse_args()

    if not DATABASE_URL:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    single_user = args.provider_user_id or args.user_id
    scope = f"provider_user_id={args.provider_user_id}" if args.provider_user_id else (f"user_id={args.user_id}" if args.user_id else "all users")
    logger.info("Starting member_org_ids reconciliation (dry_run=%s, scope=%s)", args.dry_run, scope)
    logger.info("  Old table: %s", IDENTITY_INDEX_TABLE)
    logger.info("  V2 table:  %s", USER_IDENTITY_INDEX_TABLE)
    logger.info("  Region:    %s", AWS_REGION)

    # Fetch the target users (and their full membership sets) from Postgres
    users = await get_users_with_memberships(
        DATABASE_URL,
        provider_user_id=args.provider_user_id,
        user_id=args.user_id,
    )
    logger.info("Found %d GitHub account projections to reconcile", len(users))

    if not users:
        if single_user:
            # Exit non-zero: an operator repairing one user's projection needs to
            # know the target was not found rather than read "complete" and
            # assume the row is now correct.
            logger.error(
                "No GitHub identity matched %s — nothing written. "
                "For a fully deleted user, use --provider-user-id to clear the account's projection.",
                scope,
            )
            sys.exit(1)
        logger.info("Nothing to reconcile — exiting")
        return

    # Create DDB client
    ddb_client = boto3.client("dynamodb", region_name=AWS_REGION)

    # Process each user
    success_count = 0
    error_count = 0
    partial_count = 0
    absent_count = 0

    for processed, user in enumerate(users, start=1):
        provider_user_id = user["provider_user_id"]
        member_org_ids = user["member_org_ids"]

        completed = []
        for table_name, update_table in [(IDENTITY_INDEX_TABLE, update_old_table), (USER_IDENTITY_INDEX_TABLE, update_new_table)]:
            try:
                updated = update_table(ddb_client, provider_user_id, member_org_ids, args.dry_run)
                if updated:
                    outcome = "dry_run" if args.dry_run else "updated"
                    complete = True
                else:
                    absent_count += 1
                    # No row is already the desired revoked state. A nonempty
                    # projection needs an existing identity row from its owner;
                    # this membership-only repair must not invent a bare one.
                    complete = not member_org_ids
                    outcome = "already_absent" if complete else "missing"
                completed.append(complete)
                log = logger.info if complete else logger.error
                log("Membership reconciliation account=%s table=%s outcome=%s", provider_user_id, table_name, outcome)
            except Exception:
                completed.append(False)
                logger.exception("Membership reconciliation account=%s table=%s outcome=error", provider_user_id, table_name)
                # Still attempt the other table: a partial clear is safe, but
                # must exit nonzero so a retry repairs the incomplete copy.

        if all(completed):
            success_count += 1
        else:
            error_count += 1
            if any(completed):
                partial_count += 1

        # Throttle to avoid DDB throughput issues
        if not args.dry_run and processed % 25 == 0:
            await asyncio.sleep(0.1)

    logger.info(
        "Reconciliation complete: %d succeeded, %d failed (%d partial), %d absent table rows, %d total",
        success_count,
        error_count,
        partial_count,
        absent_count,
        len(users),
    )
    # Non-zero on any failure so a CI/runbook caller can't treat a partial
    # reconciliation as a repaired projection.
    if error_count:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
