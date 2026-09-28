"""Team-membership writes: the one place ``team_memberships`` is mutated.

Issue #4840 (EPIC #4839), design note §2.1 (ruling R2). Many-to-many user<->team
membership with at most one primary per user per org.

Three invariants live here, and callers should not re-implement any of them:

1. **Portability / the SQLite blind spot** (design note §1.5b). Migration
   ``040`` creates ``uq_team_memberships_one_primary`` — a PostgreSQL-only partial
   unique index on ``(user_id, org_id) WHERE is_primary`` — behind a dialect guard,
   because `create_all()` (and hence the SQLite test suite) would build it as a
   *plain* unique index that wrongly rejects a user's second, non-primary team. The
   consequence: **CI cannot catch a second-primary write at the DB level**, so a
   blind ``INSERT ... is_primary=true`` passes tests and raises ``IntegrityError``
   in production. ``ON CONFLICT`` would be equally untestable. So: SELECT-then-
   upsert keyed on ``(user_id, team_id)``, with the one-primary rule enforced in
   Python before the write. This is the same reasoning, one grain down, as
   ``src/admin/memberships.py`` (see its docstring, ``:13-21``).

2. **``users.team_id`` is a cache, not the authority.** It stays the denormalized
   primary-team pointer, updated in the same transaction as membership. The
   Cognito pre-token Lambda reads Cognito attributes, NOT this pointer: HTTP
   writers must finish with ``team_membership_claims.commit_team_memberships``
   to synchronize the selected login after commit.

3. **A membership row must never confer authority.** ``team_memberships.role``
   describes a user's function *within a team* (member/lead) and is deliberately
   NOT consulted by ``admin/access_control.py``. Org-level authority comes from
   ``tenant_memberships`` (migration 021) and nothing here. Roles are normalized to
   a small closed set so a caller cannot smuggle ``org_admin`` into a team row and
   have some future reader mistake it for a grant — the mirror of
   ``memberships.py``'s second invariant.

Do not confuse ``team_memberships`` (here, team grain) with ``tenant_memberships``
(``src/admin/memberships.py``, org grain, the live authz authority). One character
apart, different tables, both exist.

Every function flushes but does NOT commit — the caller owns the transaction, so a
membership change and its ``users.team_id`` update land atomically or not at all.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.exceptions import ResourceNotFoundError, SecondPrimaryTeamError
from src.shared.models.organization import Team, TeamMembership, User

logger = logging.getLogger(__name__)

# Roles a team membership row may carry. Deliberately small and closed: a team row
# describes function within a team, never authority (see module docstring 3).
VALID_TEAM_ROLES: frozenset[str] = frozenset({"member", "lead"})

DEFAULT_TEAM_ROLE = "member"


def normalize_team_role(role: str | None) -> str:
    """Normalize a role for storage in ``team_memberships.role``.

    Anything unrecognized — including admin-level strings someone might hope get
    honored, like ``org_admin`` — collapses to ``member``. Failing closed here is
    what keeps a team row from ever reading as a privilege grant.
    """
    normalized = (role or "").strip().lower()
    if normalized in VALID_TEAM_ROLES:
        return normalized
    return DEFAULT_TEAM_ROLE


async def _load_user_in_org(db: AsyncSession, *, user_id: str, org_id: str) -> User:
    """Fetch a user, asserting it belongs to ``org_id``. Raises 404 if not.

    Tenant scoping is enforced by making the org part of the lookup rather than
    checking it after: a user in another tenant is indistinguishable from one that
    does not exist, which is what we want an out-of-org caller to learn.
    """
    from src.shared.models.onboarding import TenantMembership

    revoked = (
        select(TenantMembership.id)
        .where(TenantMembership.user_id == user_id, TenantMembership.tenant_id == org_id, TenantMembership.revoked_at.is_not(None))
        .exists()
    )
    user = (await db.execute(select(User).where(User.id == user_id, User.org_id == org_id, ~revoked).with_for_update())).scalar_one_or_none()
    if user is None:
        raise ResourceNotFoundError("User", user_id)
    return user


async def _load_team_in_org(db: AsyncSession, *, team_id: str, org_id: str) -> Team:
    """Fetch a team, asserting it belongs to ``org_id``. Raises 404 if not."""
    team = (await db.execute(select(Team).where(Team.id == team_id, Team.org_id == org_id))).scalar_one_or_none()
    if team is None:
        raise ResourceNotFoundError("Team", team_id)
    return team


async def list_memberships(db: AsyncSession, *, user_id: str, org_id: str) -> list[TeamMembership]:
    """Return every team membership ``user_id`` holds in ``org_id``.

    Primary first, then oldest-first, so the caller (and the UI) get a stable order
    without having to sort.
    """
    await _load_user_in_org(db, user_id=user_id, org_id=org_id)
    rows = (
        (
            await db.execute(
                select(TeamMembership)
                .where(TeamMembership.user_id == user_id, TeamMembership.org_id == org_id)
                .order_by(TeamMembership.is_primary.desc(), TeamMembership.created_at)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def add_membership(
    db: AsyncSession,
    *,
    user_id: str,
    team_id: str,
    org_id: str,
    role: str = DEFAULT_TEAM_ROLE,
    is_primary: bool = False,
    source: str = "admin",
    external_id: str | None = None,
) -> TeamMembership:
    """Idempotently ensure ``user_id`` is a member of ``team_id``.

    Portable upsert keyed on ``(user_id, team_id)`` — a SELECT-then-write, not
    ``ON CONFLICT``; see module docstring 1.

    If ``is_primary`` is requested and the user already has a *different* primary
    team, this raises :class:`SecondPrimaryTeamError` rather than silently moving
    the primary: the caller asked for a membership, not a re-pointing of the
    Cognito claim, and guessing wrong there changes which team the user appears to
    be on everywhere. Use :func:`set_primary_team` to move it deliberately.

    When the user has NO primary yet (e.g. a shadow user whose ``team_id`` is
    ``""``), the first membership added becomes primary automatically — the
    first-membership rule, mirroring ``memberships.py``'s first-active behavior.

    **Lazy materialization of a pre-backfill primary.** User-creating writers
    still mint ``users.team_id`` with no membership row, so a user created after
    the backfill can have a real team pointer and an empty membership set. For
    such a user the first-membership rule would auto-promote whatever team the
    first ``add_membership`` call names — silently re-pointing ``users.team_id``
    and therefore the Cognito ``custom:team_id`` claim. So: when the user has no
    primary membership but ``users.team_id`` is non-empty, differs from the
    requested team, and names a real team in this org, that existing team is
    first materialized as a primary membership row (``is_primary=True``,
    ``source="admin"``, mirroring the backfill's semantics), and only then is the
    requested membership added — non-primary unless explicitly ``is_primary=True``,
    in which case the just-materialized row is demoted (demote-flush-promote,
    like :func:`set_primary_team`) rather than refused, since the caller never
    set that primary deliberately. If ``users.team_id`` is empty or dangling
    (no such team in the org), the plain first-membership rule applies.
    """
    user = await _load_user_in_org(db, user_id=user_id, org_id=org_id)
    await _load_team_in_org(db, team_id=team_id, org_id=org_id)

    desired_role = normalize_team_role(role)

    rows = (await db.execute(select(TeamMembership).where(TeamMembership.user_id == user_id, TeamMembership.org_id == org_id))).scalars().all()
    existing = next((m for m in rows if m.team_id == team_id), None)
    current_primary = next((m for m in rows if m.is_primary), None)

    # Lazy materialization (see docstring): a post-backfill user may carry a real
    # users.team_id with no membership rows; give that team its primary row FIRST
    # so this call cannot silently re-point the Cognito custom:team_id claim.
    materialized: TeamMembership | None = None
    if current_primary is None and user.team_id and user.team_id != team_id:
        legacy_team = (await db.execute(select(Team).where(Team.id == user.team_id, Team.org_id == org_id))).scalar_one_or_none()
        if legacy_team is not None:
            materialized = TeamMembership(
                user_id=user_id,
                team_id=user.team_id,
                org_id=org_id,
                role=DEFAULT_TEAM_ROLE,
                is_primary=True,
                source="admin",
            )
            db.add(materialized)
            await db.flush()
            logger.info("team membership lazily materialized from users.team_id: user=%s team=%s org=%s", user_id, user.team_id, org_id)
            rows = [*rows, materialized]
            current_primary = materialized

    # No primary anywhere yet -> this membership becomes it.
    wants_primary = is_primary or current_primary is None

    if wants_primary and current_primary is not None and current_primary.team_id != team_id:
        if current_primary is materialized:
            # The conflicting primary was materialized in this very call, not set
            # deliberately by anyone — so an explicit is_primary=True moves it
            # rather than 409ing. Demote-then-flush before promoting, because the
            # partial unique index is checked per-statement (set_primary_team).
            materialized.is_primary = False
            await db.flush()
            current_primary = None
        else:
            raise SecondPrimaryTeamError(user_id=user_id, existing_team_id=current_primary.team_id, requested_team_id=team_id)

    if existing is not None:
        existing.role = desired_role
        if wants_primary and not existing.is_primary:
            existing.is_primary = True
        if existing.is_primary:
            _point_user_at_primary(user, team_id)
        await db.flush()
        return existing

    membership = TeamMembership(
        user_id=user_id,
        team_id=team_id,
        org_id=org_id,
        role=desired_role,
        is_primary=wants_primary,
        source=source,
        external_id=external_id,
    )
    db.add(membership)
    if wants_primary:
        _point_user_at_primary(user, team_id)
    await db.flush()
    logger.info(
        "team membership added: user=%s team=%s org=%s role=%s is_primary=%s source=%s",
        user_id,
        team_id,
        org_id,
        desired_role,
        wants_primary,
        source,
    )
    return membership


async def remove_membership(db: AsyncSession, *, user_id: str, team_id: str, org_id: str) -> None:
    """Remove ``user_id``'s membership of ``team_id``. Idempotent.

    Removing the primary promotes the oldest remaining membership so the user is
    never left with teams but no primary (which would leave ``users.team_id``
    pointing at a team they are no longer on). If it was their last membership,
    ``users.team_id`` is set to ``""`` — the same "no team" sentinel the shadow-user
    and approval paths already write, so no reader sees a novel value.
    """
    user = await _load_user_in_org(db, user_id=user_id, org_id=org_id)

    rows = (await db.execute(select(TeamMembership).where(TeamMembership.user_id == user_id, TeamMembership.org_id == org_id))).scalars().all()
    target = next((m for m in rows if m.team_id == team_id), None)
    if target is None:
        return

    was_primary = target.is_primary
    await db.delete(target)
    # Flush the delete BEFORE promoting a successor: within one flush SQLAlchemy
    # emits UPDATEs before DELETEs on the same table, so the promote would run
    # while the old primary row still exists and trip the per-statement
    # uq_team_memberships_one_primary partial index on Postgres — the same
    # reasoning as set_primary_team's demote-then-flush. SQLite cannot catch
    # this (no partial index there), so the ordering here is the only guard.
    await db.flush()

    if was_primary:
        remaining = sorted((m for m in rows if m.team_id != team_id), key=lambda m: m.created_at)
        if remaining:
            promoted = remaining[0]
            promoted.is_primary = True
            _point_user_at_primary(user, promoted.team_id)
            logger.info("team membership removed primary: user=%s org=%s promoted team=%s", user_id, org_id, promoted.team_id)
        else:
            _point_user_at_primary(user, "")
            logger.info("team membership removed last: user=%s org=%s team_id cleared", user_id, org_id)

    await db.flush()


async def set_primary_team(db: AsyncSession, *, user_id: str, team_id: str, org_id: str) -> TeamMembership:
    """Make ``team_id`` the user's primary, demoting whichever team held it.

    This is the deliberate counterpart to :func:`add_membership`'s refusal to move
    the primary implicitly. Creates the membership if the user is not on the team
    yet. Updates ``users.team_id`` in the same transaction (docstring 2).
    """
    user = await _load_user_in_org(db, user_id=user_id, org_id=org_id)
    await _load_team_in_org(db, team_id=team_id, org_id=org_id)

    rows = (await db.execute(select(TeamMembership).where(TeamMembership.user_id == user_id, TeamMembership.org_id == org_id))).scalars().all()

    # Demote first: the partial unique index is checked per-statement, so leaving
    # the old primary set while promoting the new one would trip it on Postgres.
    for row in rows:
        if row.is_primary and row.team_id != team_id:
            row.is_primary = False
    await db.flush()

    target = next((m for m in rows if m.team_id == team_id), None)
    if target is None:
        target = TeamMembership(
            user_id=user_id,
            team_id=team_id,
            org_id=org_id,
            role=DEFAULT_TEAM_ROLE,
            is_primary=True,
            source="admin",
        )
        db.add(target)
    else:
        target.is_primary = True

    _point_user_at_primary(user, team_id)
    await db.flush()
    logger.info("team membership primary set: user=%s org=%s team=%s", user_id, org_id, team_id)
    return target


async def replace_memberships(
    db: AsyncSession,
    *,
    user_id: str,
    org_id: str,
    desired: list[dict],
) -> list[TeamMembership]:
    """Replace ``user_id``'s entire membership set in ``org_id``. Idempotent.

    This backs the admin UI's save action, where the client sends the full intended
    set rather than a diff. ``desired`` is a list of dicts with ``team_id``, and
    optional ``role`` / ``is_primary`` / ``source`` / ``external_id``.

    Rules:
      - at most one entry may be ``is_primary``; a second raises
        :class:`SecondPrimaryTeamError` (the same stable code the UI branches on)
      - a non-empty set with no primary marked keeps the user's CURRENT primary
        when that team is still in the set (an unflagged save must not move the
        Cognito claim), falling back to the first entry only when it is not —
        so the set can never leave the user with teams but no primary
      - teams no longer in the set are removed; teams already present are updated
        in place, preserving ``created_at`` and ``id``
      - an empty set removes every membership and clears ``users.team_id`` to ``""``
    """
    user = await _load_user_in_org(db, user_id=user_id, org_id=org_id)

    # Collapse duplicate team_ids, last entry wins, so a sloppy client cannot
    # create two rows for one team and trip the (user_id, team_id) unique.
    by_team: dict[str, dict] = {}
    for entry in desired:
        by_team[entry["team_id"]] = entry

    primaries = [team_id for team_id, entry in by_team.items() if entry.get("is_primary")]
    if len(primaries) > 1:
        raise SecondPrimaryTeamError(user_id=user_id, existing_team_id=primaries[0], requested_team_id=primaries[1])

    for team_id in by_team:
        await _load_team_in_org(db, team_id=team_id, org_id=org_id)

    existing_stmt = select(TeamMembership).where(TeamMembership.user_id == user_id, TeamMembership.org_id == org_id)
    existing_rows = (await db.execute(existing_stmt)).scalars().all()
    existing_by_team = {m.team_id: m for m in existing_rows}

    if primaries:
        primary_team_id = primaries[0]
    elif by_team:
        # No entry flagged: keep the current primary if its team is still in the
        # submitted set (docstring); only fall back to the first entry otherwise.
        current_primary_team = next((m.team_id for m in existing_rows if m.is_primary), None)
        primary_team_id = current_primary_team if current_primary_team in by_team else next(iter(by_team))
    else:
        primary_team_id = None

    # Delete removed memberships, and demote the old primary, BEFORE promoting the
    # new one — same per-statement index reasoning as set_primary_team.
    for row in existing_rows:
        if row.team_id not in by_team:
            await db.delete(row)
        elif row.is_primary and row.team_id != primary_team_id:
            row.is_primary = False
    await db.flush()

    result: list[TeamMembership] = []
    for team_id, entry in by_team.items():
        role = normalize_team_role(entry.get("role"))
        is_primary = team_id == primary_team_id
        row = existing_by_team.get(team_id)
        if row is None:
            row = TeamMembership(
                user_id=user_id,
                team_id=team_id,
                org_id=org_id,
                role=role,
                is_primary=is_primary,
                source=entry.get("source") or "admin",
                external_id=entry.get("external_id"),
            )
            db.add(row)
        else:
            row.role = role
            row.is_primary = is_primary
            if entry.get("external_id") is not None:
                row.external_id = entry["external_id"]
        result.append(row)

    _point_user_at_primary(user, primary_team_id or "")
    await db.flush()
    logger.info(
        "team memberships replaced: user=%s org=%s count=%d primary=%s",
        user_id,
        org_id,
        len(result),
        primary_team_id,
    )
    return result


def _point_user_at_primary(user: User, team_id: str) -> None:
    """Keep the denormalized ``users.team_id`` pointer in step with the primary.

    The pointer is a cache of this table, not an independent fact (docstring 2).
    This alone does not update Cognito. HTTP writers commit and synchronize the
    selected login through ``team_membership_claims.commit_team_memberships``.
    """
    if user.team_id != team_id:
        logger.info("users.team_id pointer updated: user=%s %r -> %r", user.id, user.team_id, team_id)
        user.team_id = team_id
