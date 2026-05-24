#!/usr/bin/env python3
"""Seed bot identity rows in Postgres and DynamoDB.

Issue #780: Creates first-class bot identities so bots resolve through
the same identity_resolver path as humans. All operations are idempotent.

Usage:
    python seed-bot-identities.py \\
        --tenant-org-id sophos-test \\
        --bot-slug agent-developer \\
        --installation-id 12345 \\
        [--bot-github-id 123456789]

    # Seed all known bots at once:
    python seed-bot-identities.py \\
        --tenant-org-id sophos-test \\
        --all-bots \\
        --installation-id 12345

Environment variables:
    DATABASE_URL - Postgres connection string (required)
    AWS_REGION   - AWS region for DynamoDB (default: us-east-1)
    ENVIRONMENT  - Environment prefix for DDB tables (default: dev)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone

import boto3

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Known bot slugs and their GitHub App bot usernames
KNOWN_BOTS = {
    "agent-developer": "aws-e-adp-agent-dev[bot]",
    "agent-operations": "aws-e-adp-agent-ops[bot]",
    "agent-reviewer": "aws-e-adp-agent-reviewer[bot]",
    "agent-architect": "aws-e-adp-agent-architect[bot]",
    "agent-pm": "aws-e-adp-agent-pm[bot]",
}

SENTINEL_TEAM_NAME = "bot-agents"
SENTINEL_DEPARTMENT_NAME = "bot-agents"


def deterministic_uuid(provider: str, provider_user_id: str) -> str:
    """Generate a deterministic UUID v5 from provider + provider_user_id."""
    namespace = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
    return str(uuid.uuid5(namespace, f"{provider}:{provider_user_id}"))


def resolve_bot_github_id(bot_slug: str, bot_github_id: int | None) -> int | None:
    """Resolve GitHub user ID for a bot via override or gh api."""
    if bot_github_id is not None:
        return bot_github_id

    bot_username = KNOWN_BOTS.get(bot_slug)
    if not bot_username:
        logger.warning("Unknown bot slug '%s' — cannot resolve GitHub ID", bot_slug)
        return None

    try:
        result = subprocess.run(
            ["gh", "api", f"/users/{bot_username}", "--jq", ".id"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
        logger.warning(
            "gh api lookup failed for %s (rc=%d). Use --bot-github-id to override.",
            bot_username,
            result.returncode,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError) as e:
        logger.warning("gh api lookup failed: %s. Use --bot-github-id.", e)

    return None


def ensure_sentinel_department(conn, org_id: str) -> str:
    """Ensure bot-agents sentinel department exists. Returns id."""
    result = conn.execute(
        "SELECT id FROM departments WHERE org_id = %s AND name = %s",
        (org_id, SENTINEL_DEPARTMENT_NAME),
    )
    row = result.fetchone()
    if row:
        logger.info("Sentinel department already exists: %s", row[0])
        return row[0]

    dept_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO departments "
        "(id, org_id, name, description, created_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (
            dept_id,
            org_id,
            SENTINEL_DEPARTMENT_NAME,
            "Sentinel department for bot agents",
            now,
        ),
    )
    logger.info(
        "Created sentinel department '%s': %s",
        SENTINEL_DEPARTMENT_NAME,
        dept_id,
    )
    return dept_id


def ensure_sentinel_team(conn, org_id: str, department_id: str) -> str:
    """Ensure bot-agents sentinel team exists. Returns id."""
    result = conn.execute(
        "SELECT id FROM teams WHERE org_id = %s AND name = %s",
        (org_id, SENTINEL_TEAM_NAME),
    )
    row = result.fetchone()
    if row:
        logger.info("Sentinel team already exists: %s", row[0])
        return row[0]

    team_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO teams "
        "(id, org_id, department_id, name, description, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            team_id,
            org_id,
            department_id,
            SENTINEL_TEAM_NAME,
            "Sentinel team for bot agents",
            now,
        ),
    )
    logger.info(
        "Created sentinel team '%s': %s",
        SENTINEL_TEAM_NAME,
        team_id,
    )
    return team_id


def ensure_bot_user(
    conn,
    bot_slug: str,
    bot_github_id: int,
    org_id: str,
    team_id: str,
) -> str:
    """Ensure a bot user row exists in Postgres. Returns user_id."""
    user_id = deterministic_uuid("github", str(bot_github_id))

    result = conn.execute("SELECT id FROM users WHERE id = %s", (user_id,))
    row = result.fetchone()
    if row:
        logger.info("Bot user '%s' already exists: %s", bot_slug, user_id)
        return user_id

    conn.execute(
        "INSERT INTO users "
        "(id, org_id, team_id, email, name, role, "
        "user_kind, bot_kind, cognito_sub, "
        "cognito_username, is_shadow, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, "
        "%s, %s, %s, %s, %s, %s)",
        (
            user_id,
            org_id,
            team_id,
            f"{bot_slug}@bot.adp.local",
            f"{bot_slug} bot",
            "agent",
            "bot",
            bot_slug,
            None,  # cognito_sub
            None,  # cognito_username
            False,  # is_shadow
            datetime.now(timezone.utc),
        ),
    )
    logger.info("Created bot user '%s': %s", bot_slug, user_id)
    return user_id


def ensure_ddb_identity_row(
    table, bot_github_id: int, user_id: str, org_id: str, bot_slug: str
) -> None:
    """Ensure identity-index DDB row exists (old table)."""
    key = {
        "identity_type": "github_user",
        "identity_value": str(bot_github_id),
    }
    resp = table.get_item(Key=key)
    if resp.get("Item"):
        logger.info(
            "DDB identity-index row exists for github_id=%d",
            bot_github_id,
        )
        return

    table.put_item(
        Item={
            **key,
            "user_id": user_id,
            "org_id": org_id,
            "provider_username": KNOWN_BOTS.get(bot_slug, bot_slug),
            "user_kind": "bot",
            "bot_kind": bot_slug,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    logger.info(
        "Created DDB identity-index row for github_id=%d",
        bot_github_id,
    )


def ensure_ddb_user_identity_row(
    table, bot_github_id: int, user_id: str, org_id: str, bot_slug: str
) -> None:
    """Ensure user-identity-index DDB row exists (v2 table)."""
    key = {
        "provider": "github",
        "provider_user_id": str(bot_github_id),
    }
    resp = table.get_item(Key=key)
    if resp.get("Item"):
        logger.info(
            "DDB user-identity-index row exists for github_id=%d",
            bot_github_id,
        )
        return

    table.put_item(
        Item={
            **key,
            "user_id": user_id,
            "org_id": org_id,
            "provider_username": KNOWN_BOTS.get(bot_slug, bot_slug),
            "user_kind": "bot",
            "bot_kind": bot_slug,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    logger.info(
        "Created DDB user-identity-index row for github_id=%d",
        bot_github_id,
    )


def seed_bot(
    bot_slug: str,
    bot_github_id: int | None,
    tenant_org_id: str,
    installation_id: int,
    db_url: str,
    aws_region: str,
    environment: str,
) -> dict:
    """Seed a single bot identity. Returns summary dict."""
    resolved_github_id = resolve_bot_github_id(bot_slug, bot_github_id)
    if resolved_github_id is None:
        logger.error(
            "Cannot resolve GitHub ID for '%s'. Pass --bot-github-id explicitly.",
            bot_slug,
        )
        return {
            "bot_slug": bot_slug,
            "status": "failed",
            "reason": "no_github_id",
        }

    logger.info(
        "Seeding bot '%s' (github_id=%d) for tenant '%s'",
        bot_slug,
        resolved_github_id,
        tenant_org_id,
    )

    # Postgres operations
    try:
        import psycopg2

        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        cur = conn.cursor()

        class CursorWrapper:
            """Thin wrapper so helpers can call .execute/.fetchone."""

            def __init__(self, cursor):
                self._cur = cursor

            def execute(self, query, params=None):
                self._cur.execute(query, params)
                return self._cur

            def fetchone(self):
                return self._cur.fetchone()

        cw = CursorWrapper(cur)
        department_id = ensure_sentinel_department(cw, tenant_org_id)
        team_id = ensure_sentinel_team(cw, tenant_org_id, department_id)
        user_id = ensure_bot_user(
            cw, bot_slug, resolved_github_id, tenant_org_id, team_id
        )

        cur.close()
        conn.close()
    except ImportError:
        logger.error(
            "psycopg2 not installed. Install with: pip install psycopg2-binary"
        )
        return {
            "bot_slug": bot_slug,
            "status": "failed",
            "reason": "missing_psycopg2",
        }
    except Exception as e:
        logger.error("Postgres operation failed for '%s': %s", bot_slug, e)
        return {
            "bot_slug": bot_slug,
            "status": "failed",
            "reason": str(e),
        }

    # DynamoDB operations
    try:
        dynamodb = boto3.resource("dynamodb", region_name=aws_region)

        id_table_name = f"adp-{environment}-identity-index"
        uid_table_name = f"adp-{environment}-user-identity-index"

        id_table = dynamodb.Table(id_table_name)
        uid_table = dynamodb.Table(uid_table_name)

        ensure_ddb_identity_row(
            id_table, resolved_github_id, user_id, tenant_org_id, bot_slug
        )
        ensure_ddb_user_identity_row(
            uid_table,
            resolved_github_id,
            user_id,
            tenant_org_id,
            bot_slug,
        )
    except Exception as e:
        logger.error("DynamoDB operation failed for '%s': %s", bot_slug, e)
        return {
            "bot_slug": bot_slug,
            "status": "failed",
            "reason": str(e),
        }

    return {
        "bot_slug": bot_slug,
        "status": "ok",
        "user_id": user_id,
        "github_id": resolved_github_id,
        "team_id": team_id,
        "department_id": department_id,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Seed bot identity rows in Postgres and DynamoDB."
    )
    parser.add_argument(
        "--tenant-org-id",
        required=True,
        help="Home tenant org_id for the bot(s)",
    )
    parser.add_argument(
        "--installation-id",
        required=True,
        type=int,
        help="GitHub App installation ID",
    )
    parser.add_argument("--bot-slug", help="Bot slug (e.g. agent-developer)")
    parser.add_argument(
        "--bot-github-id",
        type=int,
        help="Override: numeric GitHub user ID for the bot",
    )
    parser.add_argument(
        "--all-bots",
        action="store_true",
        help="Seed all known bots",
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="Postgres connection URL",
    )
    parser.add_argument(
        "--aws-region",
        default=os.environ.get("AWS_REGION", "us-east-1"),
    )
    parser.add_argument(
        "--environment",
        default=os.environ.get("ENVIRONMENT", "dev"),
    )

    args = parser.parse_args()

    if not args.db_url:
        logger.error("DATABASE_URL not set. Pass --db-url or set env var.")
        sys.exit(1)

    if not args.all_bots and not args.bot_slug:
        logger.error("Specify --bot-slug or --all-bots")
        sys.exit(1)

    slugs = list(KNOWN_BOTS.keys()) if args.all_bots else [args.bot_slug]
    results = []

    for slug in slugs:
        gh_id = args.bot_github_id if not args.all_bots else None
        result = seed_bot(
            bot_slug=slug,
            bot_github_id=gh_id,
            tenant_org_id=args.tenant_org_id,
            installation_id=args.installation_id,
            db_url=args.db_url,
            aws_region=args.aws_region,
            environment=args.environment,
        )
        results.append(result)

    # Print summary
    print("\n" + "=" * 60)
    print("SEED RESULTS")
    print("=" * 60)
    for r in results:
        if r["status"] == "ok":
            print(
                f"  [OK] {r['bot_slug']}: "
                f"user_id={r['user_id']}, "
                f"github_id={r['github_id']}"
            )
        else:
            print(f"  [FAIL] {r['bot_slug']}: {r.get('reason', 'unknown')}")
    print("=" * 60)

    # Output JSON for automation consumption
    print("\nJSON output:")
    print(json.dumps(results, indent=2))

    failed = [r for r in results if r["status"] != "ok"]
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
