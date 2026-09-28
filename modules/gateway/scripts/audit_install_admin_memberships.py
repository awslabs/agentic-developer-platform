#!/usr/bin/env python3
"""Report org-admin memberships that the pre-#4072 install callback may have minted
in a tenant the installer had no standing in.

Issue #4072 (#5, CRITICAL), item I5.

What this looks for
-------------------
Before #4072 the unauthenticated GitHub install callback re-derived its *target
tenant* from caller-supplied data (``installation_id`` -> GitHub account ->
``github_org_id``), so an install could be routed into a tenant the caller did not
belong to. Every downstream write then landed in that tenant, including a
``tenant_memberships`` row with ``role='org_admin'`` and ``joined_via='app_install'``
(#4006 makes the installer an org admin by contract).

The forensic signature of a *possible* takeover is therefore:

    an admin-level tenant_memberships row, joined_via='app_install',
    whose tenant_id is NOT the user's own users.org_id

Why this is REPORT-ONLY (decision D3)
-------------------------------------
That signature is **ambiguous**, and the ambiguity is not resolvable from the
database alone. The very same rows are produced by the legitimate #2952 flow this
issue deliberately preserved: a user whose home tenant is A genuinely installs the
App on GitHub org B, and correctly becomes an org admin of tenant B. Nothing in
Postgres distinguishes "installed an org they administer on GitHub" from "named a
victim's installation id", because the distinguishing fact — whether the person was
actually an admin of that GitHub organization at install time — lives in GitHub, not
here.

Decision D3 is therefore to **preserve the cohort and log it**: revoking on this
signature would strip legitimate org admins of access to workspaces they built,
which is a worse and much more likely outcome than leaving a suspicious row in
place for a human to adjudicate. There is deliberately no ``--apply`` mode. Any
revocation is an operator decision, taken per row, after checking GitHub org
membership — not something a script infers.

This is the same reasoning #4006 arrived at the hard way: its ``--apply`` gap-fill
performed a cross-tenant privilege escalation *itself* because it inferred a tenant
from ambiguous state. See ``scripts/audit_org_admin_memberships.py``.

Interpreting the output
-----------------------
For each reported row, check whether the user is (or was) an admin/owner of the
GitHub organization bound to that tenant:

    gh api "orgs/<github_org_login>/memberships/<github_login>"

* ``role: admin``  -> legitimate #2952 shared-workspace onboarding. Leave it.
* ``404`` / ``role: member`` -> candidate takeover. Escalate; revoke by hand after
  confirming with the tenant's owners.

Exit codes: 0 = nothing to review, 1 = rows to review, 2 = usage error.
Unlike the #3987 audit this is NOT a merge gate — a nonzero exit means "a human
should look", not "the deploy is unsafe".

Usage:
    DATABASE_URL=postgresql+asyncpg://... python audit_install_admin_memberships.py

Environment variables:
    DATABASE_URL: Postgres connection string (required)
"""

import argparse
import asyncio
import logging
import os
import sys

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("audit-install-admin-memberships")

# Membership roles that confer admin-level authority over a tenant. Mirrors
# src/admin/memberships.py::ADMIN_LEVEL_ROLES.
ADMIN_LEVEL_ROLES = ("platform_admin", "admin", "org_admin")

# The joined_via stamp written by _create_installer_membership
# (src/admin/connections/service.py) — i.e. "this row came from an App install".
INSTALL_JOINED_VIA = "app_install"

# Rows whose tenant differs from the member's own org. The join to organizations
# pulls the GitHub org binding so the operator has what they need for the `gh api`
# check without a second query.
SUSPECT_QUERY = text(
    """
    SELECT
        tm.user_id,
        u.email,
        u.org_id            AS home_tenant,
        tm.tenant_id        AS granted_tenant,
        tm.role,
        tm.is_active,
        tm.joined_via,
        tm.created_at,
        o.github_org_id,
        o.created_via       AS tenant_created_via
    FROM tenant_memberships tm
    JOIN users u ON u.id = tm.user_id
    LEFT JOIN organizations o ON o.id = tm.tenant_id
    WHERE tm.joined_via = :install_joined_via
      AND LOWER(TRIM(tm.role)) IN :admin_roles
      AND tm.tenant_id IS DISTINCT FROM u.org_id
    ORDER BY tm.created_at DESC
    """
).bindparams(bindparam("admin_roles", expanding=True))


async def run_audit(session: AsyncSession) -> int:
    """Report install-minted admin memberships outside the member's home tenant.

    Makes no writes. See the module docstring for why (decision D3).

    Returns:
        0 when there is nothing to review, 1 when there are rows to adjudicate.
    """
    rows = (
        await session.execute(
            SUSPECT_QUERY,
            {"install_joined_via": INSTALL_JOINED_VIA, "admin_roles": list(ADMIN_LEVEL_ROLES)},
        )
    ).all()

    if not rows:
        logger.info("OK: no install-minted admin memberships outside the member's home tenant.")
        return 0

    logger.warning(
        "Found %d install-minted admin membership(s) in a tenant other than the member's home tenant.",
        len(rows),
    )
    logger.warning("This set is AMBIGUOUS by construction: it contains both legitimate #2952")
    logger.warning("shared-workspace onboarding and any pre-#4072 cross-tenant takeover.")
    logger.warning("Nothing is changed. Adjudicate each row against GitHub org membership.")
    logger.warning("")

    for (
        user_id,
        email,
        home_tenant,
        granted_tenant,
        role,
        is_active,
        joined_via,
        created_at,
        github_org_id,
        tenant_created_via,
    ) in rows:
        logger.warning(
            "  user=%s email=%s home_tenant=%s -> granted_tenant=%s role=%r is_active=%s joined_via=%s created_at=%s "
            "tenant_github_org_id=%s tenant_created_via=%s",
            user_id,
            email,
            home_tenant,
            granted_tenant,
            role,
            is_active,
            joined_via,
            created_at,
            github_org_id,
            tenant_created_via,
        )

    logger.warning("")
    logger.warning("For each row, confirm the user administers the bound GitHub org:")
    logger.warning('  gh api "orgs/<github_org_login>/memberships/<github_login>"')
    logger.warning("  role=admin -> legitimate, leave it. 404/member -> escalate, revoke by hand.")
    logger.warning("Report-only by design (decision D3): revoking on this signature alone would")
    logger.warning("strip legitimate org admins of workspaces they own.")
    return 1


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report install-minted cross-tenant admin memberships (Issue #4072). Report-only; makes no writes.",
    )
    parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is required")
        return 2

    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            return await run_audit(session)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
