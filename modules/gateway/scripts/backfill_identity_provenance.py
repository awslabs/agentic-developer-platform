#!/usr/bin/env python3
"""Project verification_method onto DDB identity rows from Postgres truth.

Issue #5664 (A10). `user_identities.verification_method` records HOW a link
between a platform user and an external account was established, and the webhook
path now refuses to mint human dispatch authority from a link that is not proven
(`common/agent_authority.py`). Both DynamoDB identity tables are read-optimized
projections of Postgres, and until this issue neither carried the attribute — so
every resolution off the hot path came out with no provenance at all.

The gateway writers now set the attribute on every new or updated row. This
script is what fixes rows that already exist: without it, enforcing the authority
gate would refuse every sender whose row predates the writers, which is an outage
rather than a fix. Run it BEFORE republishing the Lambda. See
`docs/runbooks/identity-provenance-rollout.md` for the ordered procedure.

It is also the standing reconciliation path, for the same reason
`backfill_member_org_ids.py` is: the write-through in
`src/admin/identity/identity_index_writer.py` is deliberately best-effort (it
never raises, so a DDB fault cannot fail a Postgres mutation that already
committed), and that is only safe because drift can be repaired.

Nothing here invents evidence
-----------------------------
This script only COPIES what Postgres already recorded. It never upgrades a
value: a `magic_link` row (pre-#5664, ambiguous — the platform genuinely cannot
tell whether the link was delivered out-of-band or handed back to the requester)
stays `magic_link`, which `is_proven()` treats as unproven. Rewriting those to a
proven value would be manufacturing proof that was never collected, which is the
finding this issue exists to close.

The multi-row reduction rule
----------------------------
`user_identities` is unique per (provider, provider_user_id, org_id), so ONE
external account legitimately holds N rows — one per tenant it is linked in — and
those rows may carry DIFFERENT provenance (OAuth-confirmed in org A, merely
self-asserted in org B). The DDB key is (provider, provider_user_id) with no
org component, so a single value has to stand in for all N.

The reduction is fail-closed: a proven method is projected only when EVERY row
for the account agrees on it. Any disagreement projects "" (unknown), because the
DDB row cannot say which tenant's link a given resolution is about, and
projecting the most permissive of the N would let provenance earned in org A mint
authority in org B. "" is not a denial of service: the resolver's canonical
lookup is tenant-scoped (it passes the installation's org_id), so it answers the
question precisely and overwrites the projected value — the projection is a cache,
and the safe cache miss is "unknown".

Usage:
    # Dry-run — prints the reduction for every account, writes nothing:
    python backfill_identity_provenance.py --dry-run

    # Backfill every account (the pre-cutover run):
    python backfill_identity_provenance.py

    # Reconcile ONE account (targeted repair after a failed write-through):
    python backfill_identity_provenance.py --provider-user-id 1234567
    python backfill_identity_provenance.py --user-id 3f8c...-uuid

    # Against a specific environment:
    IDENTITY_INDEX_TABLE=adp-prod-identity-index \
    USER_IDENTITY_INDEX_TABLE=adp-prod-user-identity-index \
    DATABASE_URL=postgresql+asyncpg://... \
    python backfill_identity_provenance.py

Idempotent and safe to re-run: each account's value is recomputed from Postgres
and overwritten. Re-running after a Postgres change is how the projection is
brought back into agreement.

Environment variables:
    DATABASE_URL: Postgres connection string (required)
    IDENTITY_INDEX_TABLE: legacy DDB table (default: adp-dev-identity-index)
    USER_IDENTITY_INDEX_TABLE: v2 DDB table (default: adp-dev-user-identity-index)
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

IDENTITY_INDEX_TABLE = os.environ.get("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
USER_IDENTITY_INDEX_TABLE = os.environ.get("USER_IDENTITY_INDEX_TABLE", "adp-dev-user-identity-index")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# The value projected when the account's rows disagree. Deliberately the same
# string a never-projected row reads as, so the two are indistinguishable to the
# reader: both mean "this cache cannot answer, ask Postgres".
UNKNOWN_PROVENANCE = ""


def reduce_provenance(methods: set[str]) -> str:
    """Collapse an account's per-tenant verification methods to one projected value.

    Returns the shared method when every row agrees, otherwise UNKNOWN_PROVENANCE.

    Kept as a pure function so the rule is unit-testable without Postgres or DDB,
    and so the fail-closed direction is stated in one place rather than inline in
    the loop below. Note what it does NOT do: it does not prefer the proven value,
    does not rank methods, and has no notion of "most trusted" — ranking is how a
    reduction quietly becomes "project whatever grants the most".
    """
    if len(methods) == 1:
        return next(iter(methods))
    return UNKNOWN_PROVENANCE


async def get_identity_provenance(
    db_url: str,
    *,
    provider_user_id: str | None = None,
    user_id: str | None = None,
) -> list[dict]:
    """Query Postgres for each GitHub account's verification methods.

    Grouped by provider_user_id (the DDB key), not by the user_identities row, for
    the reason the module docstring explains: one account has N rows and the
    projection has one slot. The aggregation happens in Python rather than via
    `array_agg` so the query stays portable to the SQLite test suite.

    The `user_id` filter is a semi-join, matching backfill_member_org_ids.py: it
    selects WHICH account to rebuild, and the rebuild must still see every row
    that account holds — otherwise a single-user repair would reduce over a subset
    and could project a proven value the full set does not agree on.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(db_url)

    params: dict[str, str] = {}
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
            ui.verification_method
        FROM user_identities ui
        WHERE ui.provider = 'github'
        {filter_sql}
    """)

    async with engine.connect() as conn:
        result = await conn.execute(query, params)
        rows = result.fetchall()

    await engine.dispose()

    grouped: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        # A NULL/absent column reduces to unknown rather than being skipped: an
        # account with one proven row and one NULL row must NOT project proven.
        grouped.setdefault((row[0], row[1]), set()).add(row[2] or UNKNOWN_PROVENANCE)

    return [
        {
            "provider_user_id": pid,
            "provider": provider,
            "methods": sorted(methods),
            "verification_method": reduce_provenance(methods),
        }
        for (pid, provider), methods in grouped.items()
    ]


def _update(client, table: str, key: dict, provider_user_id: str, verification_method: str, dry_run: bool) -> bool:
    """SET verification_method on one row, leaving every other attribute alone.

    UpdateItem rather than PutItem on purpose: these rows carry attributes this
    script does not know about (member_org_ids, user_kind, bot_kind), and a full
    overwrite would blank them.
    """
    if dry_run:
        logger.info("[DRY-RUN] Would update %s: %s → verification_method=%r", table, provider_user_id, verification_method)
        return True

    try:
        client.update_item(
            TableName=table,
            Key=key,
            UpdateExpression="SET verification_method = :vmethod, updated_at = :now",
            ExpressionAttributeValues={
                ":vmethod": {"S": verification_method},
                ":now": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            },
            # Only touch rows that already exist. UpdateItem upserts by default,
            # and creating a bare row carrying nothing but provenance would invent
            # an identity mapping that Postgres never projected — the reader would
            # see a row with no user_id.
            ConditionExpression="attribute_exists(#pk)",
            ExpressionAttributeNames={"#pk": next(iter(key))},
        )
        return True
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ConditionalCheckFailedException", "ValidationException"):
            logger.warning("%s row not found for %s — skipping (write-through will create it)", table, provider_user_id)
            return False
        raise


def update_old_table(client, provider_user_id: str, verification_method: str, dry_run: bool) -> bool:
    """Update the legacy identity-index table (PK identity_type, SK identity_value)."""
    return _update(
        client,
        IDENTITY_INDEX_TABLE,
        {
            "identity_type": {"S": "github_user"},
            "identity_value": {"S": provider_user_id},
        },
        provider_user_id,
        verification_method,
        dry_run,
    )


def update_new_table(client, provider_user_id: str, verification_method: str, dry_run: bool) -> bool:
    """Update the v2 user-identity-index table (PK provider, SK provider_user_id)."""
    return _update(
        client,
        USER_IDENTITY_INDEX_TABLE,
        {
            "provider": {"S": "github"},
            "provider_user_id": {"S": provider_user_id},
        },
        provider_user_id,
        verification_method,
        dry_run,
    )


async def main():
    parser = argparse.ArgumentParser(description="Project verification_method onto DDB identity rows from Postgres truth")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be updated without making changes")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--provider-user-id", help="Reconcile only this GitHub numeric user id (the projection's DDB key)")
    target.add_argument("--user-id", help="Reconcile only this ADP users.id UUID (resolved to its GitHub identities)")
    args = parser.parse_args()

    if not DATABASE_URL:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    single_user = args.provider_user_id or args.user_id
    scope = f"provider_user_id={args.provider_user_id}" if args.provider_user_id else (f"user_id={args.user_id}" if args.user_id else "all accounts")
    logger.info("Starting verification_method projection (dry_run=%s, scope=%s)", args.dry_run, scope)
    logger.info("  Legacy table: %s", IDENTITY_INDEX_TABLE)
    logger.info("  V2 table:     %s", USER_IDENTITY_INDEX_TABLE)
    logger.info("  Region:       %s", AWS_REGION)

    accounts = await get_identity_provenance(
        DATABASE_URL,
        provider_user_id=args.provider_user_id,
        user_id=args.user_id,
    )
    logger.info("Found %d GitHub accounts with identity rows", len(accounts))

    if not accounts:
        if single_user:
            # Non-zero: an operator repairing one account needs to know the target
            # was not found rather than read "complete" and assume it is now correct.
            logger.error("No GitHub identity matched %s — nothing written", scope)
            sys.exit(1)
        logger.info("Nothing to project — exiting")
        return

    ddb_client = boto3.client("dynamodb", region_name=AWS_REGION)

    success_count = 0
    error_count = 0
    ambiguous_count = 0

    for account in accounts:
        provider_user_id = account["provider_user_id"]
        verification_method = account["verification_method"]

        if verification_method == UNKNOWN_PROVENANCE and len(account["methods"]) > 1:
            # Surfaced per-account, not just counted: each of these is a real
            # account whose webhook dispatches depend on the canonical lookup
            # being reachable, so an operator should know they exist before
            # enforcement goes live rather than discover them in the deny metric.
            ambiguous_count += 1
            logger.warning(
                "Account %s holds disagreeing provenance across tenants (%s) — projecting unknown; "
                "the tenant-scoped canonical lookup decides for this account",
                provider_user_id,
                ", ".join(account["methods"]),
            )

        try:
            update_old_table(ddb_client, provider_user_id, verification_method, args.dry_run)
            update_new_table(ddb_client, provider_user_id, verification_method, args.dry_run)
            success_count += 1
        except Exception:
            logger.exception("Failed to project provenance for account %s", provider_user_id)
            error_count += 1

        # Throttle to avoid DDB throughput issues, matching backfill_member_org_ids.py.
        if not args.dry_run and success_count % 25 == 0:
            await asyncio.sleep(0.1)

    logger.info(
        "Projection complete: %d succeeded, %d failed, %d ambiguous (projected unknown), %d total",
        success_count,
        error_count,
        ambiguous_count,
        len(accounts),
    )
    # Non-zero on any failure so a runbook caller cannot treat a partial run as a
    # completed backfill and proceed to republish the enforcing Lambda.
    if error_count:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
