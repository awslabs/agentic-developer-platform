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
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.identity.verification import PROVEN_METHODS
from src.shared.models.onboarding import TenantMembership

if TYPE_CHECKING:
    from src.admin.identity.identity_index_writer import IdentityIndexWriter

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


async def set_membership_role(
    db: AsyncSession,
    *,
    user_id: str,
    tenant_id: str,
    role: str,
    joined_via: str = "admin_role_update",
) -> TenantMembership:
    """Set ``user_id``'s role in ``tenant_id`` to exactly ``role``, up OR down.

    Issue #4019. This is the deliberate counterpart to
    :func:`upsert_tenant_membership`, which never *lowers* a stored role — a rule
    that is correct for its own callers (idempotent onboarding/approval writes
    that must heal stale rows without clobbering privilege) but fatal for an
    explicit admin role change. Routing a demotion through that helper returns
    success while the membership row keeps its old role, so the "demoted" user
    retains full authority indefinitely: a silent privilege-revocation failure.
    An explicit admin role change is the one case where lowering is the intent,
    so it gets its own function rather than a flag on the idempotent one.

    Platform-level strings are still normalized to ``org_admin`` before storage
    (see :func:`normalize_membership_role`): a tenant-scoped row must never
    confer unscoped platform authority (#3981).

    Flushes but does NOT commit — the caller owns the transaction.

    Args:
        db: Session owning the enclosing transaction.
        user_id: ``users.id`` (a Postgres UUID, NOT a Cognito sub).
        tenant_id: ``organizations.id`` the membership is scoped to.
        role: Desired role; normalized via :func:`normalize_membership_role`.
        joined_via: Provenance tag (``String(32)``) used only when the row must
            be created (a user with no prior membership in this tenant).

    Returns:
        The created or updated ``TenantMembership``.
    """
    desired_role = normalize_membership_role(role)

    rows = (await db.execute(select(TenantMembership).where(TenantMembership.user_id == user_id))).scalars().all()
    existing = next((m for m in rows if m.tenant_id == tenant_id), None)
    has_other_active = any(m.is_active for m in rows if m.tenant_id != tenant_id)

    if existing is not None:
        from src.admin.membership_revocation import require_not_revoked

        require_not_revoked(existing)
        if existing.role != desired_role:
            logger.info(
                "membership set_role: user=%s tenant=%s %r -> %r (joined_via=%s)",
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
    )
    db.add(membership)
    await db.flush()
    logger.info(
        "membership set_role: created user=%s tenant=%s role=%s is_active=%s joined_via=%s",
        user_id,
        tenant_id,
        desired_role,
        membership.is_active,
        joined_via,
    )
    return membership


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
        from src.admin.membership_revocation import require_not_revoked

        require_not_revoked(existing)
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


async def project_member_org_ids(
    db: AsyncSession,
    *,
    user_id: str,
    writer: IdentityIndexWriter | None = None,
    provider_user_ids: set[str] | None = None,
) -> bool:
    """Write ``user_id``'s full membership org list to the DDB identity projection.

    Issue #4849. The single implementation of the ``member_org_ids`` write-through.
    Before this, four call sites each had their own copy, differing in the org-id
    query, how they resolved the GitHub id, which writer method they called, and
    whether they ran before or after commit — and five *other* membership-write
    call sites had no projection at all. Callers now call this instead.

    **Call this AFTER your commit, not before.** The projection is a read-optimized
    copy of committed Postgres state; publishing it from inside an open transaction
    advertises memberships that a subsequent rollback erases, and a reader has no
    way to detect that. This is why the projection is not written from inside
    :func:`upsert_tenant_membership` / :func:`set_membership_role`, which
    deliberately flush without committing so the caller owns the transaction.

    **Never raises.** A projection failure must not turn a committed membership
    write into an error response to the user: the Postgres row is authoritative and
    already durable, and reconciliation
    (``scripts/backfill_member_org_ids.py``) is the designed repair path. Failures
    are logged and reported via the return value.

    **No ``is_active`` filter.** ``is_active`` marks which single membership is the
    user's currently-selected workspace, not whether the membership is real — at
    most one row per user carries it. Every membership the user holds belongs in the
    projection. (The ``switch_tenant`` paths that flip this flag therefore do NOT
    need to project: they change which row is active, never the set of tenant ids.)

    **Fans out over every GitHub identity.** ``user_identities`` is uniquely indexed
    on ``(provider, provider_user_id, org_id)`` — per-org, so one user can hold
    several GitHub identity rows. The previous copies all used
    ``scalar_one_or_none()``, which raises on a multi-org user and silently
    projected nothing.

    **The org set is computed per GITHUB ACCOUNT, not per user row.** The DDB key
    is ``(provider, provider_user_id)`` — one row per GitHub account — but the
    per-org unique index above means one GitHub account can legitimately map to N
    ``users.id`` rows (one per org it joined). A projection computed as
    ``WHERE user_id = :the_mutated_user`` would therefore *clobber* the sibling
    users' orgs off the shared key: an org2 admin creating a user that claims
    GitHub id 123 would shrink the key to ``["org2"]`` and a fail-closed reader
    would start denying the same account's real org1 memberships. So for each
    GitHub id this user holds, the projected list is the UNION of
    ``tenant_memberships.tenant_id`` across user rows with a PROVEN binding to
    that account. The projection grants sign-in eligibility and satisfies the
    webhook's ``home_tenant_only`` policy, so a channel placement or self-asserted
    identity must not add another user's memberships to the account's authority.
    Reconciliation must apply the same proof filter.

    The set of IDs to REFRESH stays unfiltered, including a removed user's
    snapshot: an unproven row may have contributed stale memberships before this
    check existed. Refreshing that key must clear those memberships while
    retaining any proven siblings, rather than leave the old permissive list.

    Args:
        db: Session to read committed membership state through.
        user_id: ``users.id`` whose projection should be refreshed.
        writer: Optional injected writer (tests, and callers that already built one).
        provider_user_ids: GitHub IDs captured before deleting a user. Supply the
            snapshot AFTER commit so removed identities can still have their
            projection refreshed from the surviving membership rows.

    Returns:
        True if every identity row was projected (including the vacuous case of a
        user with no GitHub identity); False if any write failed or errored.
    """
    try:
        from src.admin.identity.identity_index_writer import IdentityIndexWriter
        from src.shared.models.vault import UserIdentity

        if provider_user_ids is None:
            identities = list(
                (
                    await db.execute(
                        select(UserIdentity).where(
                            UserIdentity.user_id == user_id,
                            UserIdentity.provider == "github",
                        )
                    )
                )
                .scalars()
                .all()
            )
            provider_user_ids = {i.provider_user_id for i in identities if i.provider_user_id}

        if not provider_user_ids:
            # Nothing to project onto. Not an error: a user can hold memberships
            # before any GitHub identity is linked, and the identity-creation path
            # projects when it lands.
            logger.info(
                "member_org_ids projection: user=%s has no github identity; nothing to project",
                user_id,
            )
            return True

        writer = writer or IdentityIndexWriter()
        ok = True
        for provider_user_id in sorted(provider_user_ids):
            # Keep every proven sibling's memberships on the shared key. The
            # unfiltered ID snapshot chooses what to refresh, not whose
            # memberships may confer authority through this GitHub account.
            member_org_ids = sorted(
                set(
                    (
                        await db.execute(
                            select(TenantMembership.tenant_id)
                            .join(UserIdentity, UserIdentity.user_id == TenantMembership.user_id)
                            .where(
                                TenantMembership.revoked_at.is_(None),
                                UserIdentity.provider == "github",
                                UserIdentity.provider_user_id == provider_user_id,
                                UserIdentity.verification_method.in_(PROVEN_METHODS),
                            )
                            .distinct()
                        )
                    )
                    .scalars()
                    .all()
                )
            )
            if not await writer.update_user_membership_orgs(
                provider_user_id=provider_user_id,
                member_org_ids=member_org_ids,
                provider="github",
            ):
                ok = False
                logger.warning(
                    "member_org_ids projection: write failed for user=%s github_id=%s orgs=%s",
                    user_id,
                    provider_user_id,
                    member_org_ids,
                )
        return ok
    except Exception:
        logger.exception(
            "member_org_ids projection: failed for user=%s (non-fatal; Postgres is authoritative)",
            user_id,
        )
        return False
