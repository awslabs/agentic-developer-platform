#!/usr/bin/env python3
"""Copy canonical GitHub identity proof to existing, matching DDB bindings.

Issue #5664 (A10). Run after deploying the gateway writers and before enforcing
webhook provenance; see docs/runbooks/identity-provenance-rollout.md.

A provider account can have different users and proof in different tenants.
Each projection already names a source user_id and org_id: copy only the method
from that exact canonical provider/account/user/tenant tuple. Do not reduce proof
across tenants or select a new mapping. This leaves any_adp_user and membership
routing decisions to the resolver.

Stale or ambiguous projected bindings have their proof cleared, conditionally,
and are reported as incomplete. Missing projections are not created: this script
does not own their membership, bot classification or other required attributes.
Repair the mapping through the canonical writer, then retry. Legacy magic_link
and other unproven methods are copied unchanged, never upgraded to proof.

main() rereads each account under Postgres FOR SHARE locks held through both DDB
attempts. These locks serialize changes to the selected identity rows; DDB
conditions also reject changes to the observed mapping, method or updated_at.
This is not a cross-store transaction, does not drain pending write-throughs,
and does not repair orphan DDB keys absent from the source query. Cutover still
requires writer/reader coordination and verification documented in the runbook.

Usage:
    python scripts/backfill_identity_provenance.py --dry-run
    python scripts/backfill_identity_provenance.py
    python scripts/backfill_identity_provenance.py --provider-user-id 1234567
    python scripts/backfill_identity_provenance.py --user-id <ADP-users.id>

Dry-run reads Postgres and both DDB tables, but writes nothing. Both tables are
required by default. Nonzero exit means a required projection is incomplete;
success does not mean every canonical link is proven. Retrying after a partial
write is safe. The script never mutates Postgres.

Environment: DATABASE_URL (required), IDENTITY_INDEX_TABLE,
USER_IDENTITY_INDEX_TABLE, AWS_REGION (defaults below).
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from collections import Counter

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

IDENTITY_INDEX_TABLE = os.environ.get("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
USER_IDENTITY_INDEX_TABLE = os.environ.get("USER_IDENTITY_INDEX_TABLE", "adp-dev-user-identity-index")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
UNKNOWN_PROVENANCE = ""


async def _query_accounts(conn, *, provider_user_id=None, user_id=None, lock=False) -> list[dict]:
    """Keep the complete binding for every row of the selected GitHub accounts.

    --user-id selects accounts, not a subset of their bindings. Postgres row locks
    keep their proof stable during projection. SQLite ignores row locks and is
    used only for the offline query/projection tests.
    """
    params = {}
    filter_sql = ""
    if provider_user_id:
        filter_sql = "AND ui.provider_user_id = :provider_user_id"
        params["provider_user_id"] = provider_user_id
    elif user_id:
        filter_sql = """AND ui.provider_user_id IN (
            SELECT ui2.provider_user_id FROM user_identities ui2
            WHERE ui2.provider = 'github' AND ui2.user_id = :user_id
        )"""
        params["user_id"] = user_id

    lock_sql = "FOR SHARE" if lock and conn.dialect.name == "postgresql" else ""
    result = await conn.execute(
        text(f"""
            SELECT ui.provider, ui.provider_user_id, ui.user_id, ui.org_id, ui.verification_method
            FROM user_identities ui
            WHERE ui.provider = 'github' {filter_sql}
            ORDER BY ui.provider_user_id, ui.org_id, ui.user_id
            {lock_sql}
        """),
        params,
    )
    grouped = {}
    for provider, pid, uid, org_id, method in result.fetchall():
        account = grouped.setdefault((provider, pid), {"provider": provider, "provider_user_id": pid, "bindings": []})
        account["bindings"].append({"user_id": uid, "org_id": org_id, "verification_method": method or UNKNOWN_PROVENANCE})
    return list(grouped.values())


async def get_identity_provenance(db_url: str, *, provider_user_id: str | None = None, user_id: str | None = None) -> list[dict]:
    """Read the canonical binding snapshot; main refreshes it under row locks."""
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            return await _query_accounts(conn, provider_user_id=provider_user_id, user_id=user_id)
    finally:
        await engine.dispose()


def _update(client, table: str, key: dict, account: dict, dry_run: bool) -> str:
    """Return updated/planned, missing, mismatched, ambiguous or conflict.

    Read consistently, match the canonical tuple, then compare-and-set the
    observed binding and proof. Validation and service errors remain errors.
    SET preserves attributes owned by other projections, including memberships.
    """
    item = client.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
    if not item:
        logger.warning("%s: %s:%s missing; mapping repair required", table, account["provider"], account["provider_user_id"])
        return "missing"

    uid = item.get("user_id", {}).get("S")
    org_id = item.get("org_id", {}).get("S")
    matches = [binding for binding in account["bindings"] if uid and org_id and binding["user_id"] == uid and binding["org_id"] == org_id]
    if len(matches) == 1:
        method = matches[0]["verification_method"]
        status = "updated"
    else:
        # Do not bless another user's row, or choose among conflicting sources.
        method = UNKNOWN_PROVENANCE
        status = "ambiguous" if matches else "mismatched"
        logger.warning(
            "%s: %s:%s %s binding user=%s org=%s; clearing proof, mapping repair required",
            table,
            account["provider"],
            account["provider_user_id"],
            status,
            uid,
            org_id,
        )

    if dry_run:
        logger.info(
            "[DRY-RUN] %s: %s:%s user=%s org=%s method=%r result=%s",
            table,
            account["provider"],
            account["provider_user_id"],
            uid,
            org_id,
            method,
            status,
        )
        return "planned" if status == "updated" else status

    names = {"#pk": next(iter(key))}
    values = {
        ":method": {"S": method},
        ":now": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    }
    conditions = ["attribute_exists(#pk)"]
    for index, attr in enumerate(("user_id", "org_id", "verification_method", "updated_at")):
        name = f"#observed{index}"
        names[name] = attr
        if attr in item:
            value = f":observed{index}"
            values[value] = item[attr]
            conditions.append(f"{name} = {value}")
        else:
            conditions.append(f"attribute_not_exists({name})")
    try:
        client.update_item(
            TableName=table,
            Key=key,
            UpdateExpression="SET verification_method = :method, updated_at = :now",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression=" AND ".join(conditions),
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning(
                "%s: %s:%s changed during projection; retry with fresh canonical state", table, account["provider"], account["provider_user_id"]
            )
            return "conflict"
        raise
    return status


def update_old_table(client, account: dict, dry_run: bool) -> str:
    """The legacy github_user namespace must never receive another provider."""
    if account["provider"] != "github":
        raise ValueError("The legacy github_user projection only supports GitHub")
    return _update(
        client,
        IDENTITY_INDEX_TABLE,
        {"identity_type": {"S": "github_user"}, "identity_value": {"S": account["provider_user_id"]}},
        account,
        dry_run,
    )


def update_new_table(client, account: dict, dry_run: bool) -> str:
    return _update(
        client,
        USER_IDENTITY_INDEX_TABLE,
        {"provider": {"S": account["provider"]}, "provider_user_id": {"S": account["provider_user_id"]}},
        account,
        dry_run,
    )


async def main():
    parser = argparse.ArgumentParser(description="Project canonical proof onto matching DDB identity bindings")
    parser.add_argument("--dry-run", action="store_true", help="Read and plan without writing")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--provider-user-id", help="Reconcile this GitHub numeric user id")
    target.add_argument("--user-id", help="Reconcile the GitHub accounts linked to this ADP users.id")
    args = parser.parse_args()

    if not DATABASE_URL:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)
    logger.info(
        "Starting provenance projection (dry_run=%s), legacy=%s v2=%s region=%s",
        args.dry_run,
        IDENTITY_INDEX_TABLE,
        USER_IDENTITY_INDEX_TABLE,
        AWS_REGION,
    )
    accounts = await get_identity_provenance(DATABASE_URL, provider_user_id=args.provider_user_id, user_id=args.user_id)
    if not accounts:
        if args.provider_user_id or args.user_id:
            logger.error("No canonical GitHub identity matched the requested target; nothing written")
            sys.exit(1)
        logger.info("No canonical GitHub accounts to project; this does not verify orphan DDB rows")
        return

    # Network failures must not leave canonical identity rows locked for the
    # SDK's long default timeout/retry window.
    client = boto3.client(
        "dynamodb",
        region_name=AWS_REGION,
        config=Config(connect_timeout=5, read_timeout=10, retries={"mode": "standard", "total_max_attempts": 3}),
    )
    engine = create_async_engine(DATABASE_URL)
    counts = Counter()
    table_counts = Counter()
    expected = "planned" if args.dry_run else "updated"
    try:
        for index, account in enumerate(accounts, 1):
            outcomes = {}
            try:
                # Hold source locks across both independent DDB attempts. A
                # partially successful pair is retried, never reported complete.
                async with engine.begin() as conn:
                    current = await _query_accounts(conn, provider_user_id=account["provider_user_id"], lock=not args.dry_run)
                    canonical = current[0] if current else {**account, "bindings": []}
                    for label, writer in (("legacy", update_old_table), ("v2", update_new_table)):
                        try:
                            outcomes[label] = writer(client, canonical, args.dry_run)
                        except Exception:
                            logger.exception("%s projection error for GitHub account %s", label, account["provider_user_id"])
                            outcomes[label] = "error"
            except Exception:
                logger.exception("Canonical read/transaction error for GitHub account %s", account["provider_user_id"])
                outcomes["source"] = "error"
            complete = len(outcomes) == 2 and all(result == expected for result in outcomes.values())
            counts["complete" if complete else "incomplete"] += 1
            if not complete and expected in outcomes.values():
                counts["partial"] += 1
            for label, result in outcomes.items():
                table_counts[f"{label}:{result}"] += 1
            logger.info("Account %s: %s", account["provider_user_id"], outcomes)
            if not args.dry_run and index % 25 == 0:
                await asyncio.sleep(0.1)
    finally:
        await engine.dispose()

    logger.info(
        "%s: %d complete, %d incomplete (%d partial), %d accounts; table outcomes=%s",
        "Dry-run plan" if args.dry_run else "Projection",
        counts["complete"],
        counts["incomplete"],
        counts["partial"],
        len(accounts),
        dict(table_counts),
    )
    if counts["incomplete"]:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
