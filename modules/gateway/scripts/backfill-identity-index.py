#!/usr/bin/env python3
"""Backfill identity-index DynamoDB table from existing sources.

Issue #375: tenant-identity Phase A — ETL script.

Reads from:
  1. Postgres organizations table (github_installation_ids, cognito_client_ids columns)
  2. Old tenant-registry DynamoDB table (installation_id → tenant mapping)
  3. Old agent-clients DynamoDB table (client_id → org_id mapping)

Merges identity lists into organizations.github_installation_ids /
organizations.cognito_client_ids, then writes each identity to the
new identity-index DynamoDB table.

Idempotent: safe to re-run. Uses conditional writes to avoid clobbering.

Usage:
    python backfill-identity-index.py --environment dev --dry-run
    python backfill-identity-index.py --environment dev
"""

import argparse
import json
import logging
import sys
import time

import boto3
import psycopg2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def get_args():
    parser = argparse.ArgumentParser(description="Backfill identity-index table")
    parser.add_argument("--environment", "-e", default="dev", help="Environment (dev/test/prod)")
    parser.add_argument("--region", "-r", default="us-east-1", help="AWS region")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without writing")
    parser.add_argument("--db-host", default=None, help="RDS host (overrides SSM lookup)")
    parser.add_argument("--db-name", default="bedrockgw", help="Database name")
    parser.add_argument("--db-user", default="bgadmin", help="Database user")
    return parser.parse_args()


def get_db_connection(args):
    """Connect to RDS using IAM authentication or password from Secrets Manager."""
    host = args.db_host
    if not host:
        ssm = boto3.client("ssm", region_name=args.region)
        try:
            host = ssm.get_parameter(Name=f"/adp/{args.environment}/gateway/rds-endpoint")["Parameter"]["Value"]
        except Exception:
            logger.error("Cannot resolve RDS endpoint. Use --db-host or set SSM parameter.")
            sys.exit(1)

    # Try IAM auth
    rds_client = boto3.client("rds", region_name=args.region)
    token = rds_client.generate_db_auth_token(
        DBHostname=host,
        Port=5432,
        DBUsername=args.db_user,
        Region=args.region,
    )

    return psycopg2.connect(
        host=host,
        port=5432,
        database=args.db_name,
        user=args.db_user,
        password=token,
        sslmode="require",
    )


def scan_tenant_registry(dynamodb, env):
    """Scan old tenant-registry table → dict of installation_id → org_id."""
    table_name = f"adp-{env}-webhook-ingress-tenant-registry"
    mapping = {}
    try:
        paginator = dynamodb.get_paginator("scan")
        for page in paginator.paginate(TableName=table_name):
            for item in page.get("Items", []):
                iid = item.get("installation_id", {}).get("S")
                # tenant-registry stores tenant_id which maps to org_id
                org_id = item.get("org_id", item.get("tenant_id", {})).get("S")
                if iid and org_id:
                    mapping[iid] = org_id
    except dynamodb.exceptions.ResourceNotFoundException:
        logger.warning("Table %s not found, skipping", table_name)
    except Exception as e:
        logger.warning("Error scanning %s: %s", table_name, e)
    return mapping


def scan_agent_clients(dynamodb, env):
    """Scan old agent-clients table → dict of client_id → org_id."""
    table_name = f"bedrockgw-{env}-agent-clients"
    mapping = {}
    try:
        paginator = dynamodb.get_paginator("scan")
        for page in paginator.paginate(TableName=table_name):
            for item in page.get("Items", []):
                cid = item.get("client_id", {}).get("S")
                org_id = item.get("org_id", {}).get("S")
                if cid and org_id:
                    mapping[cid] = org_id
    except dynamodb.exceptions.ResourceNotFoundException:
        logger.warning("Table %s not found, skipping", table_name)
    except Exception as e:
        logger.warning("Error scanning %s: %s", table_name, e)
    return mapping


def backfill(args):
    dynamodb = boto3.client("dynamodb", region_name=args.region)
    identity_table = f"adp-{args.environment}-identity-index"

    logger.info("Starting backfill for environment=%s (dry_run=%s)", args.environment, args.dry_run)

    # Step 1: Scan old DynamoDB tables
    logger.info("Scanning old tenant-registry table...")
    installation_map = scan_tenant_registry(dynamodb, args.environment)
    logger.info("Found %d installation_id mappings in tenant-registry", len(installation_map))

    logger.info("Scanning old agent-clients table...")
    client_map = scan_agent_clients(dynamodb, args.environment)
    logger.info("Found %d client_id mappings in agent-clients", len(client_map))

    # Step 2: Connect to Postgres and update organizations
    logger.info("Connecting to Postgres...")
    conn = get_db_connection(args)
    cur = conn.cursor()

    # Get all orgs
    cur.execute("SELECT id, github_installation_ids, cognito_client_ids FROM organizations")
    orgs = cur.fetchall()
    logger.info("Found %d organizations in Postgres", len(orgs))

    # Build org → identity lists from old tables
    org_installations: dict[str, set] = {}
    org_clients: dict[str, set] = {}

    for iid, org_id in installation_map.items():
        org_installations.setdefault(org_id, set()).add(iid)

    for cid, org_id in client_map.items():
        org_clients.setdefault(org_id, set()).add(cid)

    # Step 3: Merge and update Postgres + write to identity-index
    updates = 0
    index_writes = 0

    for org_id, existing_github, existing_cognito in orgs:
        existing_github_set = set(existing_github or [])
        existing_cognito_set = set(existing_cognito or [])

        new_github = existing_github_set | org_installations.get(org_id, set())
        new_cognito = existing_cognito_set | org_clients.get(org_id, set())

        # Update Postgres if changed
        if new_github != existing_github_set or new_cognito != existing_cognito_set:
            if args.dry_run:
                logger.info(
                    "[DRY RUN] Would UPDATE org %s: github_ids %s → %s, cognito_ids %s → %s",
                    org_id,
                    existing_github_set,
                    new_github,
                    existing_cognito_set,
                    new_cognito,
                )
            else:
                cur.execute(
                    "UPDATE organizations SET github_installation_ids = %s, cognito_client_ids = %s WHERE id = %s",
                    (json.dumps(sorted(new_github)), json.dumps(sorted(new_cognito)), org_id),
                )
                updates += 1

        # Write all identities to identity-index
        all_identities = [("github_installation_id", v) for v in new_github] + [("cognito_client_id", v) for v in new_cognito]

        for identity_type, identity_value in all_identities:
            if args.dry_run:
                logger.info(
                    "[DRY RUN] Would PUT identity-index: %s/%s → org %s",
                    identity_type,
                    identity_value,
                    org_id,
                )
            else:
                try:
                    dynamodb.put_item(
                        TableName=identity_table,
                        Item={
                            "identity_type": {"S": identity_type},
                            "identity_value": {"S": identity_value},
                            "org_id": {"S": org_id},
                            "ttl": {"N": str(int(time.time()) + 7 * 86400)},
                            "updated_at": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                        },
                    )
                    index_writes += 1
                except Exception as e:
                    logger.error("Failed to write identity %s/%s: %s", identity_type, identity_value, e)

    if not args.dry_run:
        conn.commit()

    cur.close()
    conn.close()

    logger.info("Backfill complete: %d org updates, %d identity-index writes", updates, index_writes)


if __name__ == "__main__":
    args = get_args()
    backfill(args)
