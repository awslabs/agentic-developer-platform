#!/usr/bin/env python3
"""One-time backfill: stamp tenant_id on existing private repos.

Issue #1771 (Story 2 of E8 #1721).

Rules:
  - Private repos (allowed_principals != '["*"]'): set tenant_id = owner
  - Public repos (allowed_principals = '["*"]'): leave tenant_id = NULL (shared)
  - Already-stamped repos (tenant_id IS NOT NULL): skip (idempotent)

Operates on:
  1. Postgres `repositories` table
  2. Neptune graph nodes (sets `tenant_id` property on nodes belonging to private repos)

Usage:
  # Dry run (default) — shows what would change, no mutations
  python scripts/backfill_tenant_scope.py

  # Apply changes
  python scripts/backfill_tenant_scope.py --apply

  # Apply with Neptune backfill
  python scripts/backfill_tenant_scope.py --apply --neptune-endpoint <host:port>

  # Verify after apply
  python scripts/backfill_tenant_scope.py --verify

Environment variables:
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, DB_USE_IAM_AUTH, AWS_REGION
  (same as the ingestion worker — see images/ingestion/db.py)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Add parent dir so we can import from images/ingestion/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "images", "ingestion"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("backfill_tenant_scope")


def get_connection():
    """Get a Postgres connection using the shared db helper."""
    import db as stage_db

    return stage_db.get_connection()


def find_repos_to_backfill(conn) -> list[dict]:
    """Find private repos that need tenant_id stamped.

    Returns list of dicts: [{id, repo_name, owner, allowed_principals}]
    """
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, repo_name, owner, allowed_principals
            FROM repositories
            WHERE tenant_id IS NULL
              AND allowed_principals != '["*"]'::jsonb
            ORDER BY repo_name
        """)
        rows = cursor.fetchall()
        return [
            {
                "id": str(row[0]),
                "repo_name": row[1],
                "owner": row[2],
                "allowed_principals": row[3],
            }
            for row in rows
        ]
    finally:
        cursor.close()


def find_public_repos(conn) -> list[dict]:
    """Find public repos (for verification — should remain tenant_id=NULL).

    Returns list of dicts: [{id, repo_name, tenant_id}]
    """
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, repo_name, tenant_id
            FROM repositories
            WHERE allowed_principals = '["*"]'::jsonb
            ORDER BY repo_name
        """)
        rows = cursor.fetchall()
        return [{"id": str(row[0]), "repo_name": row[1], "tenant_id": row[2]} for row in rows]
    finally:
        cursor.close()


def apply_postgres_backfill(conn) -> int:
    """Set tenant_id = owner for private repos. Returns count of rows updated."""
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE repositories
            SET tenant_id = owner,
                updated_at = NOW()
            WHERE tenant_id IS NULL
              AND allowed_principals != '["*"]'::jsonb
        """)
        count = cursor.rowcount
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def apply_neptune_backfill(neptune_endpoint: str, region: str, repos: list[dict]) -> dict:
    """Stamp tenant_id property on Neptune nodes for private repos.

    For each private repo, sets tenant_id = owner on all nodes with matching
    `repo` property. Does NOT delete/reload — just adds the property.

    Returns: {success: int, failed: int, errors: [str]}
    """
    sys.path.insert(
        0,
        os.path.join(os.path.dirname(__file__), "..", "pipeline", "neptune_ingestion"),
    )
    from load_csv_to_neptune import neptune_query

    neptune_url = f"https://{neptune_endpoint}/opencypher"
    success = 0
    failed = 0
    errors = []

    for repo in repos:
        repo_name = repo["repo_name"]
        tenant_id = repo["owner"]

        cypher = """
            MATCH (n) WHERE n.repo = $repo
            SET n.tenant_id = $tenant_id
            RETURN count(n) AS cnt
        """
        result = neptune_query(
            neptune_url, region, cypher, {"repo": repo_name, "tenant_id": tenant_id}
        )

        if "error" in result:
            failed += 1
            msg = f"{repo_name}: {str(result['error'])[:200]}"
            errors.append(msg)
            log.warning("Neptune backfill failed for %s: %s", repo_name, msg)
        else:
            cnt = 0
            try:
                results = result.get("results", [])
                if results:
                    cnt = int(results[0].get("cnt", 0))
            except (IndexError, KeyError, TypeError, ValueError):
                pass
            success += 1
            log.info("Neptune: stamped tenant_id=%s on %d nodes for %s", tenant_id, cnt, repo_name)

    return {"success": success, "failed": failed, "errors": errors}


def verify(conn) -> bool:
    """Verify backfill correctness: public=NULL, private=owner."""
    cursor = conn.cursor()
    ok = True
    try:
        # Check: no public repo should have a tenant_id
        cursor.execute("""
            SELECT repo_name, tenant_id
            FROM repositories
            WHERE allowed_principals = '["*"]'::jsonb
              AND tenant_id IS NOT NULL
        """)
        bad_public = cursor.fetchall()
        if bad_public:
            ok = False
            for row in bad_public:
                log.error("PUBLIC repo has tenant_id set: %s -> %s", row[0], row[1])

        # Check: private repos should have tenant_id = owner
        cursor.execute("""
            SELECT repo_name, owner, tenant_id
            FROM repositories
            WHERE allowed_principals != '["*"]'::jsonb
              AND (tenant_id IS NULL OR tenant_id != owner)
        """)
        bad_private = cursor.fetchall()
        if bad_private:
            ok = False
            for row in bad_private:
                log.error(
                    "PRIVATE repo has wrong tenant_id: %s (owner=%s, tenant_id=%s)",
                    row[0],
                    row[1],
                    row[2],
                )

        if ok:
            # Summary counts
            cursor.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE allowed_principals = '["*"]'::jsonb AND tenant_id IS NULL) AS public_ok,
                    COUNT(*) FILTER (WHERE allowed_principals != '["*"]'::jsonb AND tenant_id = owner) AS private_ok,
                    COUNT(*) AS total
                FROM repositories
            """)
            row = cursor.fetchone()
            log.info(
                "Verification PASSED: %d public (NULL), %d private (owner-scoped), %d total",
                row[0],
                row[1],
                row[2],
            )
        else:
            log.error("Verification FAILED — see errors above")

        return ok
    finally:
        cursor.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill tenant_id for existing private repos (Issue #1771)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes (default is dry-run)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify backfill correctness (run after --apply)",
    )
    parser.add_argument(
        "--neptune-endpoint",
        default="",
        help="Neptune cluster endpoint (host:port) for node property backfill",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", "us-east-1"),
        help="AWS region (default: us-east-1)",
    )
    args = parser.parse_args()

    conn = get_connection()
    try:
        if args.verify:
            ok = verify(conn)
            return 0 if ok else 1

        # Find repos to backfill
        repos = find_repos_to_backfill(conn)
        public = find_public_repos(conn)

        log.info("=== Backfill Tenant Scope (Issue #1771) ===")
        log.info("Private repos to backfill: %d", len(repos))
        log.info("Public repos (will NOT be touched): %d", len(public))

        if repos:
            log.info("--- Private repos (tenant_id will be set to owner) ---")
            for r in repos:
                log.info("  %s -> tenant_id=%s", r["repo_name"], r["owner"])

        if not repos:
            log.info("Nothing to backfill — all private repos already have tenant_id set.")
            return 0

        if not args.apply:
            log.info("--- DRY RUN --- (use --apply to execute)")
            return 0

        # Apply Postgres backfill
        log.info("Applying Postgres backfill...")
        count = apply_postgres_backfill(conn)
        log.info("Updated %d rows in repositories table.", count)

        # Apply Neptune backfill if endpoint provided
        if args.neptune_endpoint:
            log.info("Applying Neptune backfill...")
            result = apply_neptune_backfill(args.neptune_endpoint, args.region, repos)
            log.info(
                "Neptune: %d repos succeeded, %d failed",
                result["success"],
                result["failed"],
            )
            if result["errors"]:
                for e in result["errors"]:
                    log.warning("  %s", e)
        else:
            log.info("Skipping Neptune backfill (no --neptune-endpoint provided)")

        # Verify
        log.info("Running post-apply verification...")
        ok = verify(conn)
        return 0 if ok else 1

    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
