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

Issue #4006 changed --apply in three ways:

1. **Tenant scoping (was a cross-tenant privilege escalation).** The old
   gap-fill UPDATE had no `tenant_id` predicate, so it promoted whichever row
   happened to be `is_active` — and `is_active` is per-user session state flipped
   by switch_tenant. A user who earned org_admin in org A while actively browsing
   org B was granted org_admin *in org B*. Every write is now scoped to
   `users.org_id`, the only tenant the `users.role` authority can safely be
   inferred for.
2. **The no-row cohort is now fixable.** Users with no membership row at all (the
   output of the pre-#4006 onboarding write paths) get a row in `users.org_id`,
   tagged `joined_via='backfill'` so the two cohorts stay auditable.
3. **Portable upsert, not `ON CONFLICT`.** Migration 021 creates a
   PostgreSQL-only partial unique index `uq_tenant_memberships_one_active
   ON (user_id) WHERE is_active` plus the composite unique
   `(user_id, tenant_id)`. Neither is declared on the model, so the SQLite test
   suite builds neither and a PG-only upsert would be untestable. This is a
   SELECT-then-write keyed on `(user_id, tenant_id)` that only sets `is_active`
   when the user has no *other* active row — same semantics as
   `src/admin/memberships.py::upsert_tenant_membership`, which the application
   write paths use. Keep the two in sync.

A user whose active row belongs to a different tenant therefore gets an
*inactive* admin-level row in `users.org_id` rather than an elevation in the
tenant they happen to be browsing. That still resolves for them:
`_resolve_membership_role` falls back to any row matching the token's org_id
(access_control.py:118-126).

Usage:
    # Report gaps only (default — makes no writes):
    python audit_org_admin_memberships.py

    # Idempotently gap-fill every gapped admin's membership in users.org_id:
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
import uuid

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("audit-org-admin-memberships")

# users.role values that indicate the user is intended to hold org-admin
# authority. Mirrors the admin-level keys of
# src/admin/config.py::_MEMBERSHIP_ROLE_TO_ADMIN_ROLE and
# src/admin/memberships.py::ADMIN_LEVEL_ROLES.
ADMIN_LEVEL_ROLES = ("platform_admin", "admin", "org_admin")

# The role written into a tenant-scoped row. Never 'platform_admin': a membership
# row is scoped to one tenant and must not confer platform authority (#3981).
TENANT_ADMIN_ROLE = "org_admin"

# Query: users whose users.role is admin-level but who have no membership row
# that both is_active and carries an admin-level role. LEFT JOIN + IS NULL rather
# than NOT EXISTS so the report can show the row that *does* exist.
#
# `IN` with an expanding bindparam rather than PostgreSQL's `= ANY(...)` so the
# same SQL runs under SQLite in the test suite.
GAP_QUERY = text(
    """
    SELECT u.id, u.email, u.org_id, u.role AS user_role,
           tm.tenant_id, tm.role AS membership_role, tm.is_active
    FROM users u
    LEFT JOIN tenant_memberships tm
      ON tm.user_id = u.id AND tm.is_active = true
    WHERE lower(coalesce(u.role, '')) IN :admin_roles
      AND NOT EXISTS (
        SELECT 1 FROM tenant_memberships tm2
        WHERE tm2.user_id = u.id
          AND tm2.is_active = true
          AND lower(coalesce(tm2.role, '')) IN :admin_roles
      )
      AND coalesce(u.is_shadow, false) = false
    ORDER BY u.org_id, u.email
    """
).bindparams(bindparam("admin_roles", expanding=True))

# The user's existing row for the tenant we are about to write.
_EXISTING_ROW_QUERY = text(
    """
    SELECT id, role, is_active
    FROM tenant_memberships
    WHERE user_id = :user_id AND tenant_id = :org_id
    """
)

# Whether the user holds an active row in any OTHER tenant — which forbids
# activating this one (Postgres partial unique index) and must not be switched
# away from by a backfill.
_OTHER_ACTIVE_QUERY = text(
    """
    SELECT 1
    FROM tenant_memberships
    WHERE user_id = :user_id AND tenant_id <> :org_id AND is_active = true
    LIMIT 1
    """
)

# Promote the user's row IN users.org_id to org_admin, optionally activating it.
# `is_active` is resolved in Python (never blindly true) so the partial unique
# index cannot be violated.
_UPDATE_ROW_QUERY = text(
    """
    UPDATE tenant_memberships
    SET role = :role,
        is_active = :is_active,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = :id
    """
)

_INSERT_ROW_QUERY = text(
    """
    INSERT INTO tenant_memberships (id, user_id, tenant_id, role, is_active, joined_via, created_at)
    VALUES (:id, :user_id, :org_id, :role, :is_active, 'backfill', CURRENT_TIMESTAMP)
    """
)


def _is_admin_level(role) -> bool:
    """True if a role string denotes admin-level authority."""
    return (role or "").strip().lower() in ADMIN_LEVEL_ROLES


async def upsert_admin_membership(session: AsyncSession, user_id: str, org_id: str) -> str:
    """Ensure ``user_id`` holds an admin-level membership row in ``org_id``.

    Portable SELECT-then-write keyed on ``(user_id, org_id)``; see the module
    docstring for why this is not ``ON CONFLICT``. Activates the row only when
    the user has no active row in another tenant, so it can neither violate the
    Postgres partial unique index nor silently switch the user's active tenant.

    Returns one of ``"inserted"``, ``"updated"``, ``"noop"`` (for logging).
    """
    params = {"user_id": user_id, "org_id": org_id}
    existing = (await session.execute(_EXISTING_ROW_QUERY, params)).first()
    has_other_active = (await session.execute(_OTHER_ACTIVE_QUERY, params)).first() is not None

    if existing is not None:
        row_id, role, is_active = existing
        new_role = role if _is_admin_level(role) else TENANT_ADMIN_ROLE
        new_active = bool(is_active) or not has_other_active
        if new_role == role and new_active == bool(is_active):
            return "noop"
        await session.execute(
            _UPDATE_ROW_QUERY,
            {"id": row_id, "role": new_role, "is_active": new_active},
        )
        return "updated"

    await session.execute(
        _INSERT_ROW_QUERY,
        {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "org_id": org_id,
            "role": TENANT_ADMIN_ROLE,
            "is_active": not has_other_active,
        },
    )
    return "inserted"


async def run_audit(session: AsyncSession, apply: bool = False) -> int:
    """Report (and optionally gap-fill) org-admin membership gaps.

    Args:
        session: An open AsyncSession against the target database.
        apply: When True, upsert an admin-level membership in ``users.org_id``
            for every gapped user. Idempotent.

    Returns:
        0 when no gaps remain (safe to flip the least-privilege default), else 1.
    """
    bind_params = {"admin_roles": list(ADMIN_LEVEL_ROLES)}
    rows = (await session.execute(GAP_QUERY, bind_params)).all()

    if not rows:
        logger.info("OK: every admin-level user has an is_active admin-level tenant_memberships row.")
        logger.info("Safe to flip BG_ADMIN_RBAC_LEAST_PRIVILEGE_DEFAULT / merge PR 2.")
        return 0

    logger.warning("Found %d admin-level user(s) that would LOSE org-admin authority after the flip:", len(rows))
    gapped: list[tuple[str, str]] = []
    for user_id, email, org_id, user_role, tenant_id, membership_role, is_active in rows:
        if tenant_id is None:
            detail = "NO active membership row"
        else:
            detail = f"active membership tenant={tenant_id} role={membership_role!r} is_active={is_active}"
        logger.warning("  user=%s email=%s users.org_id=%s users.role=%r -> %s", user_id, email, org_id, user_role, detail)
        if org_id:
            gapped.append((user_id, org_id))
        else:
            # users.org_id is NOT NULL, so this is defensive only. Without it
            # there is no tenant the authority can safely be inferred for.
            logger.warning("  user=%s has no users.org_id — cannot infer a tenant; skipping.", user_id)

    if not apply:
        logger.warning("")
        logger.warning("Dry-run: no changes made. Re-run with --apply to gap-fill memberships in users.org_id.")
        return 1

    counts = {"inserted": 0, "updated": 0, "noop": 0}
    for user_id, org_id in gapped:
        counts[await upsert_admin_membership(session, user_id, org_id)] += 1
    await session.commit()
    logger.info(
        "Gap-fill complete: %d row(s) inserted, %d promoted, %d already correct (all scoped to users.org_id).",
        counts["inserted"],
        counts["updated"],
        counts["noop"],
    )

    # Re-verify after the write so the exit code reflects real state.
    remaining = (await session.execute(GAP_QUERY, bind_params)).all()
    if remaining:
        # Expected for a multi-tenant admin whose active row is another tenant:
        # they now hold an INACTIVE admin row in users.org_id, which resolves for
        # them but does not satisfy the is_active-based gap query.
        logger.warning(
            "%d user(s) still match the gap query — each now holds an inactive admin-level row in users.org_id "
            "(their active row belongs to another tenant; elevating there would be a cross-tenant escalation). "
            "Confirm these resolve correctly before flipping.",
            len(remaining),
        )
        return 1
    logger.info("OK: all gaps closed. Safe to flip BG_ADMIN_RBAC_LEAST_PRIVILEGE_DEFAULT / merge PR 2.")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="Audit org-admin tenant memberships (Issue #3987).")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Gap-fill: upsert an admin-level membership in users.org_id for each gapped user. Idempotent.",
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
            return await run_audit(session, apply=args.apply)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
