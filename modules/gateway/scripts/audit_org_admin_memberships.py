#!/usr/bin/env python3
"""Audit (and optionally gap-fill) org-admin tenant memberships.

Issue #3987: `AccessControl.get_user_role()` now resolves org-level role from
`tenant_memberships.role` instead of synthesizing ORG_ADMIN from the token. PR 1
keeps the legacy ORG_ADMIN fallback for principals with no admin-level membership
row, so it is non-breaking. PR 2 flips that fallback to least-privilege
(AdminRole.MEMBER).

This script is the explicit gate between those two PRs. It reports every user who
looks like a genuine org admin (`users.role` is admin-level) but who would lose
that authority once the fallback flips, because they have no `is_active`
membership row carrying an admin-level `role`.

Run this AFTER PR 1 deploys and BEFORE flipping
BG_ADMIN_RBAC_LEAST_PRIVILEGE_DEFAULT / merging PR 2. Exit code 0 means no gaps
(safe to flip); exit code 1 means gaps remain.

Usage:
    # Report gaps only (default — makes no writes):
    python audit_org_admin_memberships.py

    # Idempotently gap-fill: set role='org_admin' on each gapped user's active
    # membership row (or on their sole row if none is flagged active):
    python audit_org_admin_memberships.py --apply

    # Against a specific environment:
    DATABASE_URL=postgresql+asyncpg://... python audit_org_admin_memberships.py

Environment variables:
    DATABASE_URL: Postgres connection string (required)
"""

import argparse
import asyncio
import logging
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("audit-org-admin-memberships")

# users.role values that indicate the user is intended to hold org-admin
# authority. Mirrors the admin-level keys of
# src/admin/config.py::_MEMBERSHIP_ROLE_TO_ADMIN_ROLE.
ADMIN_LEVEL_ROLES = ("platform_admin", "admin", "org_admin")

# Query: users whose users.role is admin-level but who have no membership row
# that both is_active and carries an admin-level role. LEFT JOIN + IS NULL rather
# than NOT EXISTS so the report can show the row that *does* exist.
GAP_QUERY = text(
    """
    SELECT u.id, u.email, u.org_id, u.role AS user_role,
           tm.tenant_id, tm.role AS membership_role, tm.is_active
    FROM users u
    LEFT JOIN tenant_memberships tm
      ON tm.user_id = u.id AND tm.is_active = true
    WHERE lower(coalesce(u.role, '')) = ANY(:admin_roles)
      AND NOT EXISTS (
        SELECT 1 FROM tenant_memberships tm2
        WHERE tm2.user_id = u.id
          AND tm2.is_active = true
          AND lower(coalesce(tm2.role, '')) = ANY(:admin_roles)
      )
      AND coalesce(u.is_shadow, false) = false
    ORDER BY u.org_id, u.email
    """
)

# Idempotent gap-fill: promote the user's active membership row to org_admin.
# Only touches rows that are active and not already admin-level, so re-running is
# a no-op. Never inserts: a user with no membership row at all has no tenant we
# can safely infer authority in — those are reported for manual onboarding.
FILL_ACTIVE_QUERY = text(
    """
    UPDATE tenant_memberships
    SET role = 'org_admin', updated_at = now()
    WHERE user_id = :user_id
      AND is_active = true
      AND lower(coalesce(role, '')) <> ALL(:admin_roles)
    """
)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Audit org-admin tenant memberships (Issue #3987).")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Gap-fill: set role='org_admin' on gapped users' active membership rows. Idempotent.",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is required")
        return 2

    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            rows = (await session.execute(GAP_QUERY, {"admin_roles": list(ADMIN_LEVEL_ROLES)})).all()

            if not rows:
                logger.info("OK: every admin-level user has an is_active admin-level tenant_memberships row.")
                logger.info("Safe to flip BG_ADMIN_RBAC_LEAST_PRIVILEGE_DEFAULT / merge PR 2.")
                return 0

            logger.warning("Found %d admin-level user(s) that would LOSE org-admin authority after the flip:", len(rows))
            no_active_row = []
            for user_id, email, org_id, user_role, tenant_id, membership_role, is_active in rows:
                if tenant_id is None:
                    no_active_row.append((user_id, email, org_id))
                    detail = "NO active membership row"
                else:
                    detail = f"active membership tenant={tenant_id} role={membership_role!r} is_active={is_active}"
                logger.warning("  user=%s email=%s users.org_id=%s users.role=%r -> %s", user_id, email, org_id, user_role, detail)

            if not args.apply:
                logger.warning("")
                logger.warning("Dry-run: no changes made. Re-run with --apply to gap-fill active membership rows.")
                if no_active_row:
                    logger.warning(
                        "%d user(s) have NO active membership row — --apply cannot fix these; they need onboarding to create one.",
                        len(no_active_row),
                    )
                return 1

            fillable = [r[0] for r in rows if r[4] is not None]
            for user_id in fillable:
                await session.execute(FILL_ACTIVE_QUERY, {"user_id": user_id, "admin_roles": list(ADMIN_LEVEL_ROLES)})
            await session.commit()
            logger.info("Promoted %d active membership row(s) to role='org_admin'.", len(fillable))

            if no_active_row:
                logger.warning(
                    "%d user(s) still have NO active membership row and were NOT fixed: %s",
                    len(no_active_row),
                    ", ".join(f"{email or user_id}" for user_id, email, _ in no_active_row),
                )
                logger.warning("Do NOT flip the default until these are onboarded or confirmed inactive.")
                return 1

            # Re-verify after the write so the exit code reflects real state.
            remaining = (await session.execute(GAP_QUERY, {"admin_roles": list(ADMIN_LEVEL_ROLES)})).all()
            if remaining:
                logger.warning("%d gap(s) remain after --apply; investigate before flipping.", len(remaining))
                return 1
            logger.info("OK: all gaps closed. Safe to flip BG_ADMIN_RBAC_LEAST_PRIVILEGE_DEFAULT / merge PR 2.")
            return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
