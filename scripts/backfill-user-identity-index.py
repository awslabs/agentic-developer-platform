#!/usr/bin/env python3
"""One-time backfill: Postgres user_identities → DDB user-identity-index.

Issue #537: Identity projection redesign.

Reads all rows from user_identities and writes them to the new
adp-<env>-user-identity-index DynamoDB table using PutItem (idempotent).
Safe to run multiple times — PutItem overwrites produce identical state.

Usage:
    python scripts/backfill-user-identity-index.py [--env dev] [--dry-run]

Prerequisites:
    - DATABASE_URL env var set (or --database-url flag)
    - AWS credentials configured with DynamoDB PutItem on the target table
    - USER_IDENTITY_INDEX_V2_WRITE=true on the gateway (for ongoing writes)
"""

import argparse
import os
import sys
import time

import boto3


def main():
    parser = argparse.ArgumentParser(description="Backfill user-identity-index DDB table from Postgres")
    parser.add_argument("--env", default="dev", help="Environment name (default: dev)")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""), help="Postgres connection string")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be written without writing")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    args = parser.parse_args()

    table_name = f"adp-{args.env}-user-identity-index"

    if not args.database_url:
        print("ERROR: DATABASE_URL env var or --database-url flag required", file=sys.stderr)
        sys.exit(1)

    # Import here so script can show --help without psycopg2 installed
    try:
        import psycopg2
    except ImportError:
        print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary", file=sys.stderr)
        sys.exit(1)

    # Connect to Postgres
    conn = psycopg2.connect(args.database_url)
    cur = conn.cursor()
    cur.execute(
        "SELECT provider, provider_user_id, user_id, org_id, provider_username "
        "FROM user_identities"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    print(f"Found {len(rows)} rows in user_identities")

    if args.dry_run:
        for provider, provider_user_id, user_id, org_id, provider_username in rows:
            print(f"  [DRY RUN] PutItem: provider={provider} provider_user_id={provider_user_id} "
                  f"user_id={user_id} org_id={org_id} provider_username={provider_username}")
        print(f"\nDry run complete. {len(rows)} items would be written to {table_name}")
        return

    # Write to DynamoDB
    dynamodb = boto3.client("dynamodb", region_name=args.region)
    written = 0
    errors = 0

    for provider, provider_user_id, user_id, org_id, provider_username in rows:
        item = {
            "provider": {"S": provider},
            "provider_user_id": {"S": provider_user_id},
            "user_id": {"S": user_id},
            "org_id": {"S": org_id},
            "updated_at": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            "ttl": {"N": str(int(time.time()) + 30 * 86400)},  # 30-day TTL
        }
        if provider_username:
            item["provider_username"] = {"S": provider_username}

        try:
            dynamodb.put_item(TableName=table_name, Item=item)
            written += 1
        except Exception as e:
            print(f"  ERROR writing provider={provider} provider_user_id={provider_user_id}: {e}", file=sys.stderr)
            errors += 1

    print(f"\nBackfill complete: {written} written, {errors} errors, {len(rows)} total")

    # Post-run assertion
    if errors > 0:
        print(f"WARNING: {errors} errors occurred. Re-run the script to retry failed items.", file=sys.stderr)
        sys.exit(1)

    # Verify count
    scan_resp = dynamodb.scan(TableName=table_name, Select="COUNT")
    ddb_count = scan_resp["Count"]
    print(f"Verification: DDB table has {ddb_count} items, Postgres has {len(rows)} rows")
    if ddb_count < len(rows):
        print("WARNING: DDB count is less than Postgres count. Some items may have TTL-expired or been deleted.", file=sys.stderr)
        sys.exit(1)

    print("SUCCESS: Backfill verified.")


if __name__ == "__main__":
    main()
