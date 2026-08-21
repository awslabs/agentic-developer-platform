"""Portable tenant-membership writes shared by every user-creating path.

Issue #4006: migration 021 (#2961) made ``tenant_memberships.role`` the authority
for org-level role, and #3987 PR 1 (#3998) made the read side
(``AccessControl._resolve_membership_role``) trust it. But the write paths that
mint an admin-level ``users`` row were never updated, so every newly-onboarded
org admin was a "no-row" principal surviving only on the legacy ORG_ADMIN
fallback — which #3987 PR 2 removes. This module is the single place those write
paths call so the store the read side trusts is always populated.

Two invariants every caller needs and none should re-implement:

1. **Portability.** Migration ``021:52-54`` creates a *PostgreSQL-only* partial
   unique index ``uq_tenant_memberships_one_active ON (user_id) WHERE is_active``
   that is not declared on the model, so ``create_all()`` (and hence the SQLite
   test suite) never builds it. A blind ``INSERT ... is_active=true`` therefore
   passes tests and raises ``IntegrityError`` in production. ``ON CONFLICT``
   would be equally untestable. So: SELECT-then-upsert keyed on
   ``(user_id, tenant_id)``, and ``is_active`` set only when the user has no
   other active row (the first-membership-active rule already used by
   ``connections/service.py``).

2. **A tenant row must never confer platform authority.** ``platform_admin`` /
   ``admin`` are normalized to ``org_admin`` before storage, matching the
   deliberate ``_MEMBERSHIP_ROLE_TO_ADMIN_ROLE`` mapping in ``admin/config.py``
   (see #3981, which removed the org_admin -> platform escalation bridge).
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.onboarding import TenantMembership

logger = logging.getLogger(__name__)

# users.role / membership-role strings that denote admin-level authority. Mirrors
# the admin-level keys of admin/config.py::_MEMBERSHIP_ROLE_TO_ADMIN_ROLE and the
# ADMIN_LEVEL_ROLES tuple in scripts/audit_org_admin_memberships.py — the audit
# and the write paths must agree on this set or the audit can never reach 0.
ADMIN_LEVEL_ROLES: frozenset[str] = frozenset({"platform_admin", "admin", "org_admin"})

# Platform-level strings that must be stored as org_admin (see module docstring).
_PLATFORM_LEVEL_ROLES: frozenset[str] = frozenset({"platform_admin", "admin"})

# The role a platform-level string collapses to in a tenant-scoped row.
TENANT_ADMIN_ROLE = "org_admin"


def is_admin_level_role(role: str | None) -> bool:
    """Return True if ``role`` denotes admin-level authority."""
    return (role or "").strip().lower() in ADMIN_LEVEL_ROLES


def normalize_membership_role(role: str | None) -> str:
    """Normalize a role string for storage in ``tenant_memberships.role``.

    Platform-level strings collapse to ``org_admin``: a membership row is scoped
    to one tenant by construction and must never be able to confer unscoped
    platform authority (#3981). Everything else is stored lowercased as-is;
    ``membership_role_to_admin_role`` fails closed on anything unrecognized.
    """
    normalized = (role or "").strip().lower()
    if normalized in _PLATFORM_LEVEL_ROLES:
        return TENANT_ADMIN_ROLE
    return normalized or "member"


async def upsert_tenant_membership(
    db: AsyncSession,
    *,
    user_id: str,
    tenant_id: str,
    role: str,
    joined_via: str,
    github_org_id: str | None = None,
) -> TenantMembership:
    """Idempotently ensure ``user_id`` has a membership in ``tenant_id``.

    Portable upsert keyed on ``(user_id, tenant_id)`` — see the module docstring
    for why this is a SELECT-then-write rather than ``ON CONFLICT``.

    On an existing row: raises the stored ``role`` to ``role`` when the new value
    is admin-level and the stored one is not (this is what heals the no-row /
    stale-``member`` cohorts on re-approve), and activates it if the user has no
    other active row. Never *lowers* an existing role and never deactivates
    another tenant's active row — switching the active tenant is the job of
    ``switch_tenant`` (``connections/routes.py``), not of a membership write.

    Flushes but does NOT commit: the caller owns the transaction, which is the
    whole point (the membership must land atomically with the ``users`` row).

    Args:
        db: Session owning the enclosing transaction.
        user_id: ``users.id`` (a Postgres UUID, NOT a Cognito sub).
        tenant_id: ``organizations.id`` the membership is scoped to.
        role: Desired role; normalized via :func:`normalize_membership_role`.
        joined_via: Provenance tag (``String(32)``) — e.g. ``onboarding_approval``.
        github_org_id: Optional GitHub org login, for parity with existing rows.

    Returns:
        The created or updated ``TenantMembership``.
    """
    desired_role = normalize_membership_role(role)

    rows = (await db.execute(select(TenantMembership).where(TenantMembership.user_id == user_id))).scalars().all()
    existing = next((m for m in rows if m.tenant_id == tenant_id), None)
    has_other_active = any(m.is_active for m in rows if m.tenant_id != tenant_id)

    if existing is not None:
        # Only ever raise privilege, never lower it: a user who is already
        # org_admin here must not be demoted by a later member-level write.
        if is_admin_level_role(desired_role) and not is_admin_level_role(existing.role):
            logger.info(
                "membership upsert: raising role user=%s tenant=%s %r -> %r (joined_via=%s)",
                user_id,
                tenant_id,
                existing.role,
                desired_role,
                joined_via,
            )
            existing.role = desired_role
        if not existing.is_active and not has_other_active:
            existing.is_active = True
        await db.flush()
        return existing

    membership = TenantMembership(
        user_id=user_id,
        tenant_id=tenant_id,
        role=desired_role,
        is_active=not has_other_active,
        joined_via=joined_via,
        github_org_id=github_org_id,
    )
    db.add(membership)
    await db.flush()
    logger.info(
        "membership upsert: created user=%s tenant=%s role=%s is_active=%s joined_via=%s",
        user_id,
        tenant_id,
        desired_role,
        membership.is_active,
        joined_via,
    )
    return membership
