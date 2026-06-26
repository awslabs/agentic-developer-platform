#!/usr/bin/env python3
"""Seed the 14 public evaluation-corpus repos into knowledge_assets as shared tier.

Issue #2089 (#2082 Phase-1 story 6): Registers the public corpus repos as
shared-tier rows: tenant_id=NULL, owner_sub=NULL, registered_by='platform',
making the registry the single source of truth for shared corpus visibility.

Idempotent: uses INSERT ... ON CONFLICT DO NOTHING against the
uq_knowledge_assets_source_scope unique index (source_ref, COALESCE(tenant_id,''),
COALESCE(owner_sub,'')). Safe to re-run.

Usage:
    python platform/scripts/seed-shared-corpus.py [--skip-dispatch] [--dry-run]

    # With explicit DB URL:
    DATABASE_URL=postgresql://user:pass@host:5432/bedrockgateway \
        python platform/scripts/seed-shared-corpus.py

    # Discover from SSM (requires AWS CLI):
    ENVIRONMENT=dev python platform/scripts/seed-shared-corpus.py

Environment variables:
    DATABASE_URL      - Postgres connection string (preferred)
    BG_DATABASE_URL   - Alternative env var name (same purpose)
    ENVIRONMENT       - dev | staging | prod (default: dev) — for SSM discovery
    AWS_REGION        - AWS region (default: us-east-1)
    INGESTION_QUEUE_URL - SQS queue URL for dispatch (optional; skipped if unset)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from datetime import UTC, datetime

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The 14 public evaluation-corpus repos (from repos.txt)
# ---------------------------------------------------------------------------

SHARED_CORPUS_REPOS: list[str] = [
    # Skill / agent collections
    "addyosmani/agent-skills",
    "obra/superpowers",
    "msitarzewski/agency-agents",
    "mvanhorn/last30days-skill",
    "mattpocock/skills",
    "Imbad0202/academic-research-skills",
    # Real codebases (structural / impact queries)
    "colbymchenry/codegraph",
    "CloakHQ/CloakBrowser",
    "chopratejas/headroom",
    "Panniantong/Agent-Reach",
    "santifer/career-ops",
    "Egonex-AI/Understand-Anything",
    # Large curated-list / markdown repos (browse / semantic)
    "awesome-selfhosted/awesome-selfhosted",
    "Hack-with-Github/Awesome-Hacking",
]

# Shared-tier constants
REGISTERED_BY = "platform"
ASSET_TYPE = "repo"
INITIAL_STATUS = "registered"


# ---------------------------------------------------------------------------
# Database connection discovery
# ---------------------------------------------------------------------------


def discover_db_url(environment: str, aws_region: str) -> str:
    """Discover DATABASE_URL from environment or SSM parameters."""
    # Check explicit env vars first
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("BG_DATABASE_URL")
    if db_url:
        return db_url

    # Fallback: discover from SSM
    logger.info("DATABASE_URL not set — discovering from SSM (env=%s)...", environment)
    try:
        import boto3

        ssm = boto3.client("ssm", region_name=aws_region)

        def _get_param(name: str, default: str = "") -> str:
            try:
                resp = ssm.get_parameter(Name=name)
                return resp["Parameter"]["Value"]
            except Exception:
                return default

        host = _get_param(f"/adp/{environment}/gateway/rds-host")
        if not host:
            logger.error("Cannot discover RDS host from SSM. Set DATABASE_URL.")
            sys.exit(1)

        port = _get_param(f"/adp/{environment}/gateway/rds-port", "5432")
        dbname = _get_param(f"/adp/{environment}/gateway/rds-dbname", "bedrockgateway")
        user = _get_param(f"/adp/{environment}/gateway/rds-username", "bgadmin")

        # Generate IAM auth token
        rds_client = boto3.client("rds", region_name=aws_region)
        token = rds_client.generate_db_auth_token(
            DBHostname=host, Port=int(port), DBUsername=user, Region=aws_region
        )

        return f"postgresql://{user}:{token}@{host}:{port}/{dbname}?sslmode=require"
    except ImportError:
        logger.error("boto3 not installed and DATABASE_URL not set.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Seed logic
# ---------------------------------------------------------------------------


def build_source_ref(owner_repo: str) -> str:
    """Build the canonical source_ref for a GitHub repo."""
    return f"https://github.com/{owner_repo}"


def seed_shared_corpus(
    db_url: str,
    *,
    skip_dispatch: bool = False,
    dry_run: bool = False,
    queue_url: str = "",
    aws_region: str = "us-east-1",
) -> dict:
    """Insert shared-tier rows for the 14 public corpus repos.

    Returns a summary dict with counts and details.
    """
    try:
        import psycopg2
    except ImportError:
        logger.error(
            "psycopg2 not installed. Install with: pip install psycopg2-binary"
        )
        sys.exit(1)

    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    cur = conn.cursor()

    inserted = []
    skipped = []

    for owner_repo in SHARED_CORPUS_REPOS:
        source_ref = build_source_ref(owner_repo)
        asset_id = str(uuid.uuid4())

        if dry_run:
            logger.info("[DRY-RUN] Would insert: %s (id=%s)", source_ref, asset_id)
            inserted.append({"owner_repo": owner_repo, "id": asset_id})
            continue

        # INSERT ... ON CONFLICT DO NOTHING (idempotent via unique index)
        cur.execute(
            """
            INSERT INTO knowledge_assets
                (id, asset_type, source_ref, tenant_id, owner_sub, project_id,
                 status, registered_by, metadata, display_name, tags)
            VALUES
                (%s, %s, %s, NULL, NULL, NULL,
                 %s, %s, '{}'::jsonb, %s, '[]'::jsonb)
            ON CONFLICT (source_ref, COALESCE(tenant_id, ''), COALESCE(owner_sub, ''))
            DO NOTHING
            RETURNING id
            """,
            (
                asset_id,
                ASSET_TYPE,
                source_ref,
                INITIAL_STATUS,
                REGISTERED_BY,
                owner_repo.split("/")[-1],  # display_name = repo name
            ),
        )

        result = cur.fetchone()
        if result:
            inserted.append({"owner_repo": owner_repo, "id": result[0]})
            logger.info("Inserted: %s (id=%s)", source_ref, result[0])
        else:
            skipped.append({"owner_repo": owner_repo, "reason": "already_exists"})
            logger.info("Skipped (already exists): %s", source_ref)

    cur.close()
    conn.close()

    # Dispatch to SQS for newly inserted rows
    dispatched = 0
    if not skip_dispatch and not dry_run and inserted and queue_url:
        dispatched = _dispatch_ingestion_batch(inserted, queue_url, aws_region)

    summary = {
        "total_repos": len(SHARED_CORPUS_REPOS),
        "inserted": len(inserted),
        "skipped": len(skipped),
        "dispatched": dispatched,
        "dry_run": dry_run,
        "details": {
            "inserted": inserted,
            "skipped": skipped,
        },
    }
    return summary


def _dispatch_ingestion_batch(
    inserted_items: list[dict],
    queue_url: str,
    aws_region: str,
) -> int:
    """Publish SQS messages for newly inserted assets."""
    try:
        import boto3

        sqs = boto3.client("sqs", region_name=aws_region)
    except ImportError:
        logger.warning("boto3 not available — skipping SQS dispatch.")
        return 0

    dispatched = 0
    for item in inserted_items:
        owner_repo = item["owner_repo"]
        asset_id = item["id"]
        message = {
            "source": owner_repo,
            "content_type": ASSET_TYPE,
            "registry_asset_id": asset_id,
            "scope": {
                "tenant_id": None,
                "owner_sub": None,
                "project_id": None,
                "visibility": "shared",
            },
            "steps": ["s3_upload", "cgc", "deepwiki", "graphrag"],
            "triggered_by": "platform_seed",
            "enqueued_at": datetime.now(UTC).isoformat(),
        }

        try:
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(message),
                MessageAttributes={
                    "content_type": {
                        "DataType": "String",
                        "StringValue": ASSET_TYPE,
                    },
                },
            )
            dispatched += 1
            logger.info("Dispatched ingestion for %s", owner_repo)
        except Exception as e:
            logger.warning("SQS dispatch failed for %s: %s", owner_repo, e)

    return dispatched


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Seed 14 public corpus repos into knowledge_assets as shared tier."
    )
    parser.add_argument(
        "--skip-dispatch",
        action="store_true",
        help="Skip SQS ingestion dispatch (insert rows only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be inserted without touching the database",
    )
    parser.add_argument(
        "--environment",
        default=os.environ.get("ENVIRONMENT", "dev"),
        help="Environment (dev/staging/prod) for SSM discovery",
    )
    parser.add_argument(
        "--aws-region",
        default=os.environ.get("AWS_REGION", "us-east-1"),
        help="AWS region",
    )

    args = parser.parse_args()

    # Discover DB URL
    db_url = discover_db_url(args.environment, args.aws_region)

    # Discover queue URL
    queue_url = os.environ.get("INGESTION_QUEUE_URL", "")
    if not queue_url and not args.skip_dispatch:
        logger.info(
            "INGESTION_QUEUE_URL not set — SQS dispatch will be skipped. "
            "Set it to enable automatic ingestion."
        )

    # Run the seed
    summary = seed_shared_corpus(
        db_url,
        skip_dispatch=args.skip_dispatch,
        dry_run=args.dry_run,
        queue_url=queue_url,
        aws_region=args.aws_region,
    )

    # Print summary
    print("\n" + "=" * 60)
    print("SHARED CORPUS SEED RESULTS")
    print("=" * 60)
    print(f"  Total repos:  {summary['total_repos']}")
    print(f"  Inserted:     {summary['inserted']}")
    print(f"  Skipped:      {summary['skipped']}")
    print(f"  Dispatched:   {summary['dispatched']}")
    if summary["dry_run"]:
        print("  Mode:         DRY-RUN (no changes made)")
    print("=" * 60)

    # JSON output for automation
    print("\nJSON output:")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
