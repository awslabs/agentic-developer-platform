#!/usr/bin/env python3
"""Report and resolve quarantined installation ownership conflicts.

Issue #4070 (sub-EPIC #4068 ·A0, decision D3).

Why this script exists
----------------------
Migration 026 finds installations that more than one tenant claims and
**quarantines** them into ``installation_ownership_conflicts`` instead of
picking a winner. It must not pick one: only GitHub knows who actually installed
the App, Alembic has no App credentials and should not acquire any, and every
available heuristic (oldest ``created_at``, first entry in the JSON array) is a
coin flip on which paying customer keeps their installation. A wrong guess
silently re-homes a tenant, and a ``DELETE`` is not covered by the migration's
"the constraint is reversible" rollback.

This script is the deliberate operator step that *can* ask GitHub. Until it
runs, the resolver reports those installations as ``AMBIGUOUS`` and fails closed
— nobody gets access, which is the safe end of the trade.

What --apply does
-----------------
For each quarantined installation it asks GitHub which account owns the
installation, matches that account id against ``organizations.github_org_id``,
and keeps ONLY the claim belonging to the matching tenant. Losing claims are
removed from both representations (``channel_tenant_map`` rows and the
``organizations.github_installation_ids`` array). Every decision is logged
before it is made, so a revert can be reasoned about.

Conflicts GitHub cannot settle — the installation is gone (404), or no claiming
tenant's ``github_org_id`` matches, or the owning tenant has a NULL
``github_org_id`` — are left quarantined and reported. Those need a human; the
script will not guess on their behalf either.

Usage:
    # Report only (default — makes no writes):
    python resolve_installation_conflicts.py

    # Resolve every conflict GitHub can settle authoritatively:
    python resolve_installation_conflicts.py --apply

    # Restrict to one installation:
    python resolve_installation_conflicts.py --installation-id 12345678 --apply

Exit codes:
    0 — no unresolved conflicts remain
    1 — conflicts remain that GitHub could not settle (needs a human)

Environment variables:
    DATABASE_URL:           Postgres connection string (required)
    GITHUB_APP_ID:          GitHub App id (required unless --skip-github)
    GITHUB_APP_PRIVATE_KEY: PEM private key (required unless --skip-github)

After --apply, re-run ``scripts/backfill-identity-index.py`` so the DynamoDB
cache reflects the corrected Postgres record of truth (Postgres is the record of
truth; DDB is a cache).
"""

import argparse
import asyncio
import logging
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("resolve-installation-conflicts")

_CONFLICTS_QUERY = text(
    """
    SELECT installation_id, org_id, source
    FROM installation_ownership_conflicts
    WHERE resolved_at IS NULL
    ORDER BY installation_id, org_id
    """
)


async def _load_conflicts(session, only_installation: str | None) -> dict[str, list[tuple[str, str]]]:
    """installation_id -> [(org_id, source), ...] for every open conflict."""
    rows = (await session.execute(_CONFLICTS_QUERY)).fetchall()
    grouped: dict[str, list[tuple[str, str]]] = {}
    for installation_id, org_id, source in rows:
        if only_installation and str(installation_id) != only_installation:
            continue
        grouped.setdefault(str(installation_id), []).append((org_id, source))
    return grouped


async def _github_owner_account_id(installation_id: str) -> int | None:
    """Ask GitHub which account owns this installation. None if it cannot say."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from src.admin.connections.github_client import GitHubAppClient

    app_id = os.environ.get("GITHUB_APP_ID", "")
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
    if not app_id or not private_key:
        logger.error("GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY are required to resolve conflicts")
        return None

    client = GitHubAppClient(app_id=app_id, private_key_pem=private_key)
    try:
        installation = await client.get_installation(int(installation_id))
    except Exception as exc:
        # A 404 means the installation no longer exists; anything else is an
        # outage. Either way GitHub cannot settle this one right now, so we
        # leave it quarantined rather than falling back to a guess.
        logger.warning("GitHub could not describe installation %s: %s", installation_id, exc)
        return None

    account_id = (installation.get("account") or {}).get("id")
    return int(account_id) if account_id is not None else None


async def _resolve_one(session, installation_id: str, claims: list[tuple[str, str]], *, apply: bool) -> bool:
    """Resolve a single conflict. Returns True if it is settled."""
    claimant_ids = [org_id for org_id, _ in claims]
    logger.info("installation %s is claimed by %d tenants: %s", installation_id, len(claimant_ids), claimant_ids)

    account_id = await _github_owner_account_id(installation_id)
    if account_id is None:
        logger.warning("  → UNRESOLVED: GitHub could not identify the owning account. Leaving quarantined.")
        return False

    # Which claiming tenant's github_org_id matches what GitHub reported?
    rows = (
        await session.execute(
            text("SELECT id, github_org_id FROM organizations WHERE id = ANY(:ids)"),
            {"ids": claimant_ids},
        )
    ).fetchall()
    matching = [org_id for org_id, github_org_id in rows if github_org_id and str(github_org_id) == str(account_id)]

    if len(matching) != 1:
        logger.warning(
            "  → UNRESOLVED: GitHub says account id=%s, but %d claiming tenant(s) match that id. Leaving quarantined.",
            account_id,
            len(matching),
        )
        return False

    winner = matching[0]
    losers = [org_id for org_id in claimant_ids if org_id != winner]
    logger.info("  → GitHub account id=%s ⇒ owner is tenant %s; revoking claims from %s", account_id, winner, losers)

    if not apply:
        logger.info("  → dry run, no writes (pass --apply to act)")
        return True

    for loser in losers:
        # Remove the losing claim from BOTH representations, or the resolver
        # would still see two claimants and keep failing closed.
        await session.execute(
            text("DELETE FROM channel_tenant_map WHERE provider = 'github' AND installation_id = :iid AND org_id = :org_id"),
            {"iid": installation_id, "org_id": loser},
        )
        org_row = (
            await session.execute(
                text("SELECT github_installation_ids FROM organizations WHERE id = :org_id"),
                {"org_id": loser},
            )
        ).fetchone()
        if org_row is not None:
            remaining = [str(i) for i in (org_row[0] or []) if str(i) != installation_id]
            await session.execute(
                text("UPDATE organizations SET github_installation_ids = :ids WHERE id = :org_id"),
                {"ids": remaining, "org_id": loser},
            )
        logger.info("  → revoked claim: tenant %s no longer holds installation %s", loser, installation_id)

    # Clear the dispute flag so migration 027's partial unique index now covers
    # this installation, and mark the quarantine rows resolved.
    await session.execute(
        text("UPDATE channel_tenant_map SET ownership_disputed = false WHERE provider = 'github' AND installation_id = :iid"),
        {"iid": installation_id},
    )
    await session.execute(
        text("UPDATE installation_ownership_conflicts SET resolved_at = now() WHERE installation_id = :iid AND resolved_at IS NULL"),
        {"iid": installation_id},
    )
    await session.commit()
    logger.info("  → RESOLVED: installation %s now maps only to tenant %s", installation_id, winner)
    return True


async def main_async(args: argparse.Namespace) -> int:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        logger.error("DATABASE_URL is required")
        return 1

    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            conflicts = await _load_conflicts(session, args.installation_id)
            if not conflicts:
                logger.info("No open installation ownership conflicts. Nothing to do.")
                return 0

            logger.info(
                "%d installation(s) quarantined as cross-tenant ambiguous.%s",
                len(conflicts),
                "" if args.apply else " Running in REPORT mode — no writes.",
            )

            unresolved = 0
            for installation_id, claims in conflicts.items():
                if not await _resolve_one(session, installation_id, claims, apply=args.apply):
                    unresolved += 1

            if unresolved:
                logger.error("%d conflict(s) still need a human decision.", unresolved)
                return 1

            if args.apply:
                logger.info("All conflicts resolved. Re-run scripts/backfill-identity-index.py to refresh the DynamoDB cache.")
            return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Report and resolve quarantined installation ownership conflicts (#4070).")
    parser.add_argument("--apply", action="store_true", help="Actually revoke losing claims (default: report only).")
    parser.add_argument("--installation-id", help="Restrict to a single installation id.")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
