#!/usr/bin/env python3
"""One-off cleanup: delete catalog rows with a corrupt (URL-shaped) repo_id.

Issue #2864.

Root cause: the asset-registration path accepted a full GitHub URL where an
``org/repo`` slug was expected. Ingestion stripped only ONE URL prefix, so a
double-prefixed value like
``https://github.com/https://github.com/octocat/Hello-World`` became the
"slug", and the worker persisted a ``repositories`` row whose ``repo_name`` /
``git_url`` start with ``http`` (double-prefixed, unusable). Door ``browse(ls)``
reads ``repositories.repo_name`` as ``repo_id`` and surfaces the corrupt entry.

This script deletes those corrupt rows. A row is considered corrupt when its
identifier looks like a URL/scheme rather than a bare ``org/repo`` slug:
  - ``repositories.repo_name`` starts with ``http`` or ``git@``
  - ``knowledge_assets.source_ref`` (repo type) is double-prefixed, i.e.
    contains ``github.com/`` more than once, or begins with ``https://github.com/https``

Both predicates are deliberately narrow so a HEALTHY row such as
``octocat/Hello-World`` (repo_name) / ``https://github.com/octocat/Hello-World``
(source_ref) is NEVER matched.

Deleting a ``repositories`` row cascades to ``dependencies`` and ``index_runs``
(ON DELETE CASCADE, migration 001). ``knowledge_assets`` is independent, so the
corrupt asset row is deleted separately.

Usage:
  # Dry run (default) — shows what would be deleted, no mutations
  python scripts/cleanup_corrupt_repo_ids.py

  # Apply the deletion
  python scripts/cleanup_corrupt_repo_ids.py --apply

  # Verify afterwards (no corrupt rows remain)
  python scripts/cleanup_corrupt_repo_ids.py --verify

Environment variables:
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, DB_USE_IAM_AUTH, AWS_REGION
  (same as the ingestion worker — see images/ingestion/db.py)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Add the ingestion image dir so we can reuse its db helper.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "images", "ingestion"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("cleanup_corrupt_repo_ids")

# ---------------------------------------------------------------------------
# Predicates that identify corrupt rows. Kept narrow so healthy rows
# (repo_name='octocat/Hello-World', source_ref='https://github.com/octocat/Hello-World')
# are never matched.
# ---------------------------------------------------------------------------

# A repo_name should be a bare 'org/repo' slug — it can never contain '://'.
# Anchor on the scheme separator so orgs like 'httpie/cli' are never matched.
CORRUPT_REPO_NAME_SQL = (
    "repo_name LIKE 'http://%' OR repo_name LIKE 'https://%' OR repo_name LIKE 'git@%'"
)

# A repo source_ref should have exactly ONE github.com. Double-prefixed rows
# carry the scheme twice.
CORRUPT_SOURCE_REF_SQL = (
    "asset_type = 'repo' AND ("
    "source_ref LIKE 'https://github.com/https%' "
    "OR source_ref LIKE 'https://github.com/git@%'"
    ")"
)


def get_connection():
    """Get a Postgres connection using the shared ingestion db helper."""
    import db as stage_db

    return stage_db.get_connection()


def find_corrupt_repositories(conn) -> list[dict]:
    """Find repositories rows whose repo_name is URL-shaped (corrupt)."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT id, repo_name, git_url FROM repositories WHERE {CORRUPT_REPO_NAME_SQL} "
            "ORDER BY repo_name"
        )
        return [
            {"id": str(row[0]), "repo_name": row[1], "git_url": row[2]} for row in cursor.fetchall()
        ]
    finally:
        cursor.close()


def find_corrupt_assets(conn) -> list[dict]:
    """Find knowledge_assets repo rows whose source_ref is double-prefixed."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT id, source_ref FROM knowledge_assets WHERE {CORRUPT_SOURCE_REF_SQL} "
            "ORDER BY source_ref"
        )
        return [{"id": str(row[0]), "source_ref": row[1]} for row in cursor.fetchall()]
    finally:
        cursor.close()


def apply_cleanup(conn) -> dict[str, int]:
    """Delete corrupt rows. Returns counts of rows deleted per table."""
    cursor = conn.cursor()
    try:
        # Deleting a repositories row cascades to dependencies + index_runs.
        cursor.execute(f"DELETE FROM repositories WHERE {CORRUPT_REPO_NAME_SQL}")
        repos_deleted = cursor.rowcount

        cursor.execute(f"DELETE FROM knowledge_assets WHERE {CORRUPT_SOURCE_REF_SQL}")
        assets_deleted = cursor.rowcount

        conn.commit()
        return {"repositories": repos_deleted, "knowledge_assets": assets_deleted}
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def verify(conn) -> bool:
    """Verify no corrupt rows remain."""
    repos = find_corrupt_repositories(conn)
    assets = find_corrupt_assets(conn)
    if repos or assets:
        for r in repos:
            log.error("CORRUPT repositories row still present: %s", r["repo_name"])
        for a in assets:
            log.error("CORRUPT knowledge_assets row still present: %s", a["source_ref"])
        log.error("Verification FAILED — %d repo row(s), %d asset row(s)", len(repos), len(assets))
        return False
    log.info("Verification PASSED: no corrupt repo_id rows remain.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete catalog rows with a corrupt URL-shaped repo_id (Issue #2864)"
    )
    parser.add_argument("--apply", action="store_true", help="Apply deletion (default is dry-run)")
    parser.add_argument("--verify", action="store_true", help="Verify no corrupt rows remain")
    args = parser.parse_args()

    conn = get_connection()
    try:
        if args.verify:
            return 0 if verify(conn) else 1

        repos = find_corrupt_repositories(conn)
        assets = find_corrupt_assets(conn)

        log.info("=== Cleanup corrupt repo_id rows (Issue #2864) ===")
        log.info("Corrupt repositories rows: %d", len(repos))
        for r in repos:
            log.info("  repositories.id=%s repo_name=%s", r["id"], r["repo_name"])
        log.info("Corrupt knowledge_assets rows: %d", len(assets))
        for a in assets:
            log.info("  knowledge_assets.id=%s source_ref=%s", a["id"], a["source_ref"])

        if not repos and not assets:
            log.info("Nothing to clean up — no corrupt rows found.")
            return 0

        if not args.apply:
            log.info("--- DRY RUN --- (use --apply to execute)")
            return 0

        log.info("Applying cleanup...")
        counts = apply_cleanup(conn)
        log.info(
            "Deleted %d repositories row(s) (+ cascaded dependencies/index_runs) "
            "and %d knowledge_assets row(s).",
            counts["repositories"],
            counts["knowledge_assets"],
        )

        log.info("Running post-apply verification...")
        return 0 if verify(conn) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
