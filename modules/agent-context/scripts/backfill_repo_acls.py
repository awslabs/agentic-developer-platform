#!/usr/bin/env python3
"""Backfill: replace legacy public-sentinel ACLs with derived ones.

Issue #5658.

The problem this cleans up
--------------------------
Two ingestion paths stamped ``allowed_principals = ["*"]`` on repositories
regardless of their real GitHub visibility:

  * ``ingest-repo.py`` set it literally when storing a wiki, and
  * ``db.ensure_repo_exists`` defaulted to it when the caller omitted the
    argument — which both ``ingest-repo.py`` call sites did.

Both are fixed at the source, but the fix does not repair existing rows, and it
does not update rows that have not yet been re-ingested. The corrected ingestion
refreshes explicitly derived ACLs, while this dry-run backfill inventories and
repairs the legacy rows without fetching and re-indexing their source content.

What it does
------------
For each unverified wildcard ACL, re-derive visibility from GitHub. Confirmed
public rows receive persisted verification; private rows receive derived private
principals. Unknown rows remain unverified and are denied public access by readers,
even when skipped. --deny-unknown additionally replaces their ACL with [].
Verification checks persisted state, never temporary source observations.

Dry-run is the default. Nothing is written without ``--apply``.

Usage
-----
    # Dry run (default) — reports the plan, writes nothing
    python scripts/backfill_repo_acls.py

    # Dry run for a single repo
    python scripts/backfill_repo_acls.py --repo aws-e/adp

    # Apply
    python scripts/backfill_repo_acls.py --apply

    # Apply, and deny (rather than skip) rows GitHub could not resolve
    python scripts/backfill_repo_acls.py --apply --deny-unknown

    # Check for remaining legacy rows (exit 1 if any)
    python scripts/backfill_repo_acls.py --verify

Rollback
--------
``--apply`` writes a JSON journal of every change (``--journal``, default
``/tmp/acl-backfill-journal.json``) containing each row's id, name, previous ACL, new ACL and both verification
states. To undo:

    python scripts/backfill_repo_acls.py --rollback /tmp/acl-backfill-journal.json

Rollback restores the previous values verbatim and is itself dry-run by default;
add ``--apply``. Keep the journal — without it the prior ACLs are not recoverable
from the database, since the old value is overwritten in place.

Rollback restores both ACL and verification state with compare-and-swap guards.
Old journals without verification fields default to false, preserving quarantine.

Environment variables
---------------------
DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, DB_USE_IAM_AUTH, AWS_REGION
    Same as the ingestion worker — see images/ingestion/db.py.
GITHUB_TOKEN
    Installation token with repo + read:org scope, used to derive ACLs. Without
    it every row is indeterminate and the run reports that and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# Add the ingestion dir so we can reuse db.py and repo_acl.py (same convention as
# scripts/backfill_tenant_scope.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "images", "ingestion"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("backfill_repo_acls")

PUBLIC_SENTINEL = "*"
LEGACY_ACL = [PUBLIC_SENTINEL]

# Outcome classifications for a single row.
OUTCOME_PUBLIC_CONFIRMED = "public_confirmed"
OUTCOME_TIGHTEN = "tighten"
OUTCOME_INDETERMINATE = "indeterminate"


def get_connection():
    """Get a Postgres connection using the shared db helper."""
    import db as stage_db

    return stage_db.get_connection()


def find_legacy_rows(conn, repo: str | None = None) -> list[dict]:
    """Find repositories carrying an unverified public sentinel.

    Rows with a genuinely derived ACL are not candidates, and neither are rows
    with ``[]``/NULL — those already deny and will be filled in by the next
    ingestion via the ON CONFLICT branch.
    """
    cursor = conn.cursor()
    try:
        sql = """
            SELECT id, repo_name, owner, allowed_principals, tenant_id, acl_public_verified
            FROM repositories
            WHERE allowed_principals ? '*' AND acl_public_verified IS NOT TRUE
        """
        params: tuple = ()
        if repo:
            sql += " AND repo_name = %s"
            params = (repo,)
        sql += " ORDER BY repo_name"

        cursor.execute(sql, params)
        return [
            {
                "id": str(row[0]),
                "repo_name": row[1],
                "owner": row[2],
                "allowed_principals": row[3],
                "tenant_id": row[4],
                "acl_public_verified": row[5],
            }
            for row in cursor.fetchall()
        ]
    finally:
        cursor.close()


def classify(row: dict, *, token: str | None) -> dict:
    """Decide what should happen to one legacy row. Performs no writes."""
    import repo_acl

    repo_name = row["repo_name"]
    try:
        derived = repo_acl.resolve_allowed_principals(repo_name, token=token)
    except Exception as exc:  # network, auth, malformed response
        log.warning("Could not derive an ACL for %s: %s", repo_name, exc)
        derived = []

    if derived == [PUBLIC_SENTINEL]:
        return {**row, "outcome": OUTCOME_PUBLIC_CONFIRMED, "new_acl": LEGACY_ACL}

    if derived:
        return {**row, "outcome": OUTCOME_TIGHTEN, "new_acl": derived}

    # Empty: either a private repo whose collaborators we could not read, or an
    # API failure. Indistinguishable from here, and both mean "do not know".
    return {**row, "outcome": OUTCOME_INDETERMINATE, "new_acl": []}


def apply_changes(
    conn, plan: list[dict], *, deny_unknown: bool, journal_path: str | None = None
) -> list[dict]:
    """Write the planned ACLs. Returns journal entries for the rows changed."""
    cursor = conn.cursor()
    journal: list[dict] = []
    try:
        for item in plan:
            outcome = item["outcome"]
            if outcome == OUTCOME_INDETERMINATE and not deny_unknown:
                continue

            new_acl = item["new_acl"]
            public_verified = outcome == OUTCOME_PUBLIC_CONFIRMED and new_acl == LEGACY_ACL
            cursor.execute(
                "UPDATE repositories SET allowed_principals = %s::jsonb, acl_public_verified = %s "
                "WHERE id = %s AND repo_name = %s AND allowed_principals = %s::jsonb "
                "AND acl_public_verified = %s",
                (
                    json.dumps(new_acl),
                    public_verified,
                    item["id"],
                    item["repo_name"],
                    json.dumps(item["allowed_principals"]),
                    item.get("acl_public_verified", False),
                ),
            )
            if cursor.rowcount != 1:
                log.warning("Skipping concurrently changed ACL: %s", item["repo_name"])
                continue
            journal.append(
                {
                    "id": item["id"],
                    "repo_name": item["repo_name"],
                    "previous_acl": item["allowed_principals"],
                    "previous_public_verified": item.get("acl_public_verified", False),
                    "new_public_verified": public_verified,
                    "new_acl": new_acl,
                }
            )
        if journal and journal_path is not None:
            # Persist recovery data before the database commit. Refuse an existing
            # path rather than overwriting a previous run's only rollback record.
            fd = os.open(journal_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(journal, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
    return journal


def rollback(conn, journal_path: str, *, apply: bool) -> int:
    """Restore previous ACLs from a journal written by --apply."""
    with open(journal_path, encoding="utf-8") as handle:
        entries = json.load(handle)

    log.info("Rollback plan: %d row(s) from %s", len(entries), journal_path)
    for entry in entries:
        log.info(
            "  %s: %s -> %s",
            entry["repo_name"],
            json.dumps(entry["new_acl"]),
            json.dumps(entry["previous_acl"]),
        )

    if not apply:
        log.info("--- DRY RUN --- (use --apply to execute the rollback)")
        return 0

    cursor = conn.cursor()
    try:
        for entry in entries:
            cursor.execute(
                "UPDATE repositories SET allowed_principals = %s::jsonb, acl_public_verified = %s "
                "WHERE id = %s AND repo_name = %s AND allowed_principals = %s::jsonb "
                "AND acl_public_verified = %s",
                (
                    json.dumps(entry["previous_acl"]),
                    entry.get("previous_public_verified", False),
                    entry["id"],
                    entry["repo_name"],
                    json.dumps(entry["new_acl"]),
                    entry.get("new_public_verified", False),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()

    log.info("Restored %d row(s).", len(entries))
    return 0


def verify(conn) -> bool:
    """Fail while any row still carries an unverified public sentinel.

    Public verification must be persisted by trusted ingestion or this backfill.
    Quarantined wildcard rows fail completion verification.
    """
    rows = find_legacy_rows(conn)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM repositories")
        total = cursor.fetchone()[0]
        cursor.execute(
            "SELECT COUNT(*) FROM repositories "
            "WHERE allowed_principals = '[]'::jsonb OR allowed_principals IS NULL"
        )
        denied = cursor.fetchone()[0]
    finally:
        cursor.close()

    log.info(
        "%d repositories total: %d public-sentinel, %d deny-all, %d with derived ACLs",
        total,
        len(rows),
        denied,
        total - len(rows) - denied,
    )
    for row in rows:
        log.warning("Quarantined unverified public sentinel remains: %s", row["repo_name"])
    return not rows


def _summarize(plan: list[dict]) -> None:
    buckets: dict[str, list[dict]] = {}
    for item in plan:
        buckets.setdefault(item["outcome"], []).append(item)

    tighten = buckets.get(OUTCOME_TIGHTEN, [])
    public = buckets.get(OUTCOME_PUBLIC_CONFIRMED, [])
    unknown = buckets.get(OUTCOME_INDETERMINATE, [])

    log.info("=== Repo ACL backfill plan (Issue #5658) ===")
    log.info('Unverified wildcard rows examined:        %d', len(plan))
    log.info("  private, will be tightened:      %d", len(tighten))
    log.info("  confirmed public, mark verified:    %d", len(public))
    log.info("  indeterminate:                   %d", len(unknown))

    if tighten:
        log.info("--- Will be tightened ---")
        for item in tighten:
            log.info("  %s -> %s", item["repo_name"], json.dumps(item["new_acl"]))
    if unknown:
        log.info("--- Indeterminate (skipped unless --deny-unknown) ---")
        for item in unknown:
            log.info("  %s", item["repo_name"])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill derived ACLs over legacy public-sentinel rows (Issue #5658)"
    )
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run)")
    parser.add_argument("--verify", action="store_true", help="Report remaining legacy rows")
    parser.add_argument("--repo", default="", help="Limit to a single repo_name")
    parser.add_argument(
        "--deny-unknown",
        action="store_true",
        help="Set [] on rows whose ACL could not be derived (already quarantined for public reads)",
    )
    parser.add_argument(
        "--journal",
        default="/tmp/acl-backfill-journal.json",
        help="Where to write the rollback journal on --apply",
    )
    parser.add_argument("--rollback", default="", help="Restore ACLs from a journal file")
    args = parser.parse_args()

    conn = get_connection()
    try:
        if args.rollback:
            return rollback(conn, args.rollback, apply=args.apply)

        if args.verify:
            return 0 if verify(conn) else 1

        rows = find_legacy_rows(conn, args.repo or None)
        if not rows:
            log.info("No unverified wildcard ACLs — nothing to backfill.")
            return 0

        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            log.error(
                "GITHUB_TOKEN is not set — every row would be indeterminate and the "
                "run would change nothing useful. Set a token with repo + read:org."
            )
            return 1

        log.info("Deriving ACLs for %d row(s) from GitHub...", len(rows))
        plan = [classify(row, token=token) for row in rows]
        _summarize(plan)

        if not args.apply:
            log.info("--- DRY RUN --- (use --apply to execute)")
            return 0

        journal = apply_changes(
            conn, plan, deny_unknown=args.deny_unknown, journal_path=args.journal
        )
        if journal:
            log.info("Updated %d row(s). Rollback journal: %s", len(journal), args.journal)
            log.info("Keep that file — the previous ACLs are not otherwise recoverable.")
        else:
            log.info("No rows required a change.")

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
