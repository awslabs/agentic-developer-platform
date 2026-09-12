"""Placing an existing platform person into an organization (Issue #4943).

The gap this closes. The members panel's add-member modal picks from the
PLATFORM-wide roster (``GET /admin/users``, #4827) while every membership write is
org-scoped by design: ``team_memberships._load_user_in_org`` resolves a user by
``(users.id, org_id)`` and 404s anything outside the path org. So an admin could
pick a real person from a real list and the only possible outcome was a refusal —
there was no route for "bring this person into this org" at all. The operator's
standing ruling (2026-09-11) is that adding is a MAPPING decision: an admin places
a person into an org and a team. This module is the org half of that action.

**Why a ``users`` row is minted rather than only a membership.** ``users`` is one
row per (person, org) — ``users.org_id`` is ``NOT NULL``, and
``memberships.project_member_org_ids`` documents the invariant explicitly: one
GitHub account legitimately maps to N ``users.id`` rows, one per org it joined.
Writing only a ``tenant_memberships`` row would therefore leave three things
broken: the follow-up team add still 404s (it resolves through ``users``), the
org's member list never shows the person, and — least visibly — the
``member_org_ids`` projection never gains the org, because it joins memberships to
``user_identities`` on ``user_id``, so a membership hung off another org's user row
is invisible to it. ``onboarding/approval.attach_approved_member`` is the shipped
precedent for exactly this shape (users row + identity rows + membership, one
transaction).

**``cognito_sub`` is deliberately left NULL on the mirror row.** ``uq_users_cognito_sub``
is a partial unique index over non-NULL subs (#700), so copying the sub onto a
second row raises ``IntegrityError`` in production — and would pass CI, since
SQLite never builds the partial index (the divergence ``memberships.py`` warns
about). The sub is not needed here either: sign-in resolves the person through
their GitHub identity and the membership projection, which is what the identity
copies below feed.

Flushes but does NOT commit — the caller owns the transaction, so the users row,
the identities and the membership land atomically or not at all. The caller is also
responsible for calling ``project_member_org_ids`` AFTER its commit (see that
function's contract).
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.exceptions import ResourceConflictError, ResourceNotFoundError, UnknownPlatformUserError
from src.admin.memberships import normalize_membership_role, upsert_tenant_membership
from src.shared.models.base import new_uuid
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity

logger = logging.getLogger(__name__)

# Provenance for the membership row. Already in use by the admin user-create path
# (``identity/users_service.py``), which is the same act one step earlier: an admin
# deciding somebody belongs in this org. A new value would split one meaning across
# two tags for no reader's benefit.
JOINED_VIA = "admin_create"

# ``users.team_id`` for the mirror row. The empty string is the shipped no-team
# sentinel (``team_memberships.add_membership`` names it: "a shadow user whose
# ``team_id`` is ``''``"), and it matters which one is used: a non-empty pointer at
# a real team would make the follow-up team add treat that team as a
# lazily-materialized primary and land the requested team as secondary, so the
# person's ``custom:team_id`` claim would name a team the admin never chose.
NO_TEAM = ""


async def _resolve_org(db: AsyncSession, org_id: str) -> Organization:
    org = await db.get(Organization, org_id)
    if org is None:
        raise ResourceNotFoundError("Organization", org_id)
    return org


async def _github_identities(db: AsyncSession, user_id: str) -> list[UserIdentity]:
    """The person's GitHub identity rows, oldest first.

    GitHub only: ``cognito`` rows are per-sub and the mirror row deliberately has no
    sub, so copying one would assert a Cognito identity for a row that cannot own it.
    """
    rows = (
        (
            await db.execute(
                select(UserIdentity)
                .where(UserIdentity.user_id == user_id, UserIdentity.provider == "github")
                .order_by(UserIdentity.created_at, UserIdentity.provider_user_id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _existing_row_in_org(db: AsyncSession, *, person: User, org_id: str) -> User | None:
    """The person's existing ``users`` row in ``org_id``, if they already have one.

    This is the idempotency key, and it is identity-first rather than email-first on
    purpose: the GitHub account is what the platform authenticates and what the
    membership projection is keyed on, while email is mutable and is the only handle
    available for a person who has never linked GitHub. Matching on both means a
    re-run of the same admin action heals (finds the row it made last time) instead
    of minting a duplicate member the org would then see twice.
    """
    if person.org_id == org_id:
        return person

    provider_user_ids = [i.provider_user_id for i in await _github_identities(db, person.id) if i.provider_user_id]
    if provider_user_ids:
        matches = list(
            (
                await db.execute(
                    select(User)
                    .join(UserIdentity, UserIdentity.user_id == User.id)
                    .where(
                        User.org_id == org_id,
                        UserIdentity.provider == "github",
                        UserIdentity.provider_user_id.in_(provider_user_ids),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        if len(matches) > 1:
            raise ResourceConflictError("User", "github_identity", "Linked accounts belong to different users in the target organization")
        # An email collision is not evidence that two GitHub identities belong
        # to the same person. In particular, never graft this identity onto a
        # placeholder or somebody else's higher-privilege membership.
        return matches[0] if matches else None

    return (await db.execute(select(User).where(User.org_id == org_id, User.email == person.email).limit(1))).scalar_one_or_none()


async def _mirror_identities(db: AsyncSession, *, source: User, target: User) -> None:
    """Copy the person's GitHub identity rows onto their row in the target org.

    Without these the membership exists and is invisible where it counts:
    ``project_member_org_ids`` fans out over ``user_identities`` per GitHub account,
    so a member row carrying no identity projects nothing and the platform-mode
    sign-in gate keeps denying a person who now genuinely belongs to the org.

    ``user_identities`` is uniquely indexed on ``(provider, provider_user_id, org_id)``,
    so an identity already present in this org — a returning person, or a second run
    of this action — is skipped rather than re-inserted.
    """
    has_primary = any(identity.is_primary for identity in await _github_identities(db, target.id))
    for identity in await _github_identities(db, source.id):
        already = (
            await db.execute(
                select(UserIdentity).where(
                    UserIdentity.org_id == target.org_id,
                    UserIdentity.provider == "github",
                    UserIdentity.provider_user_id == identity.provider_user_id,
                )
            )
        ).scalar_one_or_none()
        if already is not None:
            continue
        # Preserve a new placement's person-cap anchor. An existing destination
        # primary must not be displaced by adding another linked account.
        is_primary = identity.is_primary and not has_primary
        db.add(
            UserIdentity(
                id=new_uuid(),
                user_id=target.id,
                org_id=target.org_id,
                team_id=target.team_id,
                provider="github",
                provider_user_id=identity.provider_user_id,
                provider_username=identity.provider_username,
                is_primary=is_primary,
                verification_method=identity.verification_method,
                verified_at=identity.verified_at,
            )
        )
        has_primary = has_primary or is_primary
    await db.flush()


async def add_user_to_org(db: AsyncSession, *, user_id: str, org_id: str, role: str = "member") -> User:
    """Idempotently make the platform person ``user_id`` a member of ``org_id``.

    Returns the ``users`` row **in ``org_id``** — which is the point of returning
    anything: it is a different id from the one that was passed in whenever the
    person came from another org, and it is the id every subsequent org-scoped
    write (notably the team add this call precedes) must use. A caller that
    reuses the platform-roster id gets the 404 this route exists to prevent.

    Args:
        db: Session owning the enclosing transaction.
        user_id: ``users.id`` from the platform roster (``GET /admin/users``).
        org_id: Organization to place the person into.
        role: Org role to grant; normalized for storage by the membership helper.

    Returns:
        The person's ``users`` row in ``org_id``, created or pre-existing.

    Raises:
        ResourceNotFoundError: ``org_id`` does not exist.
        UnknownPlatformUserError: ``user_id`` names no platform user (422).
    """
    await _resolve_org(db, org_id)

    person = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if person is None:
        raise UnknownPlatformUserError(user_id)

    target = await _existing_row_in_org(db, person=person, org_id=org_id)
    if target is None:
        target = User(
            id=new_uuid(),
            org_id=org_id,
            team_id=NO_TEAM,
            email=person.email,
            name=person.name,
            role=normalize_membership_role(role),
            is_shadow=person.is_shadow,
            user_kind=person.user_kind,
            bot_kind=person.bot_kind,
        )
        db.add(target)
        await db.flush()
        logger.info("org member: minted users row user=%s org=%s from platform user=%s", target.id, org_id, person.id)

    await _mirror_identities(db, source=person, target=target)

    await upsert_tenant_membership(db, user_id=target.id, tenant_id=org_id, role=role, joined_via=JOINED_VIA)
    return target
