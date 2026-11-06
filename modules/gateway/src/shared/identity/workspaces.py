"""Resolve a login to its organization-local accounts without matching email.

GitHub broker usernames contain the authenticated immutable GitHub id. Native
Cognito logins use explicit links written by the platform's org-placement path.
An arbitrary self-linked external identity is never proof of another account's
workspace access. Memberships, not connections or GitHub installations, grant
workspace access.
"""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.identity.person_anchor import PERSON_ANCHOR_PROVIDER_PRECEDENCE
from src.shared.identity.verification import PROVEN_METHODS
from src.shared.models.base import utcnow
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Team, TeamMembership, User
from src.shared.models.vault import UserIdentity

PLACEMENT_VERIFICATION = "org_placement"


async def primary_team_for_workspace(db: AsyncSession, user: User, org_id: str) -> Team | None:
    """Resolve the same authoritative primary for selection and claim refresh.

    Membership rows win over the legacy pointer. Only a user with no memberships
    may fall back to that pointer, and only within the requested organization.
    """
    memberships = (
        await db.scalars(
            select(TeamMembership).where(TeamMembership.user_id == user.id, TeamMembership.org_id == org_id).execution_options(populate_existing=True)
        )
    ).all()
    primaries = [row for row in memberships if row.is_primary]
    if len(primaries) > 1:
        raise ValueError("Multiple primary teams are assigned in this organization")
    primary = next(iter(primaries), None)
    team_id = primary.team_id if primary else (user.team_id if not memberships else "")
    team = await db.scalar(select(Team).where(Team.id == team_id, Team.org_id == org_id)) if team_id else None
    if primary and team is None:
        raise ValueError("Primary membership does not reference a team in this organization")
    return team


async def login_user(db: AsyncSession, subject: str, *, refresh: bool = False) -> User | None:
    query = select(User).where(or_(User.cognito_sub == subject, User.id == subject)).execution_options(populate_existing=refresh)
    return (await db.execute(query)).scalar_one_or_none()


async def linked_user_ids(db: AsyncSession, user: User, *, username: str = "") -> set[str]:
    predicates = []
    if user.cognito_sub:
        predicates.append(
            (UserIdentity.provider == "cognito")
            & (UserIdentity.provider_user_id == user.cognito_sub)
            & (UserIdentity.verification_method == PLACEMENT_VERIFICATION)
        )
    # Only the SIGNED Cognito username is accepted here. Do not substitute a
    # provider_user_id supplied to the identity-link API or a mutable email.
    # Case-insensitive Cognito pools normalize the broker's GitHub_<id> name
    # to github_<id> in tokens. Normalize only the provider prefix; the numeric
    # account id remains the immutable identity proof.
    provider, _, provider_user_id = username.partition("_")
    if provider.lower() == "github" and provider_user_id.isascii() and provider_user_id.isdigit():
        predicates.append(
            (UserIdentity.provider == "github")
            & (UserIdentity.provider_user_id == provider_user_id)
            & UserIdentity.verification_method.in_(PROVEN_METHODS)
        )
    if not predicates:
        return {user.id}
    rows = await db.scalars(
        select(User.id)
        .join(UserIdentity, UserIdentity.user_id == User.id)
        .where(
            UserIdentity.org_id == User.org_id,
            or_(User.cognito_sub.is_(None), User.cognito_sub == user.cognito_sub),
            or_(*predicates),
        )
    )
    return {user.id, *rows.all()}


async def person_user_ids(db: AsyncSession, user: User, *, username: str = "") -> set[str]:
    """Every ``users.id`` provably held by the same person as ``user`` (#8011).

    One person can own several ``users`` rows (GitHub onboarding, Cognito first
    login and admin invite each create one). The rows are fused on the same
    evidence ``budget.person_ledger.resolve_person_identity`` uses, never on email:

    * the platform's own ``org_placement`` link to the row's Cognito login
      (``linked_user_ids``; ``users.cognito_sub`` itself is unique per row), and,
      when the caller holds a token, the SIGNED Cognito ``username``
      (``GitHub_<id>``) -- the same proof ``workspace_user`` accepts, so an owner
      check with a token and chat admission with only the subject fuse the same
      rows whenever the GitHub id is also linked on the login row;
    * a *proven* external identity in a person-anchor namespace
      (``PERSON_ANCHOR_PROVIDER_PRECEDENCE``: GitHub, directory) that another row
      holds too -- the GitHub numeric id is the cross-org join key.

    The result always contains ``user.id``. Which of the rows is *the* principal in
    a given tenant is the caller's decision (``chat_authority.tenant_member_principal``).
    """
    ids = await linked_user_ids(db, user, username=username)
    anchors = (
        await db.execute(
            select(UserIdentity.provider, UserIdentity.provider_user_id)
            .where(
                UserIdentity.user_id.in_(ids),
                UserIdentity.provider.in_([provider.value for provider in PERSON_ANCHOR_PROVIDER_PRECEDENCE]),
                UserIdentity.verification_method.in_(PROVEN_METHODS),
            )
            .distinct()
        )
    ).all()
    if anchors:
        same_anchor = [(UserIdentity.provider == provider) & (UserIdentity.provider_user_id == external_id) for provider, external_id in anchors]
        shared = await db.scalars(select(UserIdentity.user_id).where(or_(*same_anchor), UserIdentity.verification_method.in_(PROVEN_METHODS)))
        ids |= set(shared.all())
    return ids


async def memberships_for_login(
    db: AsyncSession, subject: str, *, username: str = "", refresh: bool = False
) -> tuple[User | None, dict[str, tuple[User, TenantMembership | None]]]:
    user = await login_user(db, subject, refresh=refresh)
    if user is None:
        return None, {}
    ids = await linked_user_ids(db, user, username=username)
    rows = (
        await db.execute(
            select(User, TenantMembership)
            .join(TenantMembership, TenantMembership.user_id == User.id)
            .where(User.id.in_(ids))
            .execution_options(populate_existing=refresh)
        )
    ).all()
    by_org: dict[str, tuple[User, TenantMembership | None]] = {}
    revoked_orgs = {membership.tenant_id for member, membership in rows if membership.revoked_at is not None}
    for member, membership in rows:
        if membership.tenant_id in revoked_orgs:
            continue
        # Legacy rows attach several memberships to the canonical user. A real
        # org-local account wins over that legacy representation, never a union
        # of their roles.
        previous = by_org.get(membership.tenant_id)
        if previous:
            if previous[0].org_id == membership.tenant_id:
                if member.org_id == membership.tenant_id and member.id != previous[0].id:
                    raise ValueError("Multiple accounts match this login in the same organization")
                continue
            if member.org_id != membership.tenant_id:
                continue
        by_org[membership.tenant_id] = member, membership
    # Older native-user provisioning did not create member-role membership rows.
    # Its own account still confers the existing least-privilege member access.
    # No display role is promoted into authority by this fallback.
    if user.org_id and user.org_id not in by_org and user.org_id not in revoked_orgs:
        by_org[user.org_id] = user, None
    return user, by_org


async def workspace_user(db: AsyncSession, subject: str, org_id: str, *, username: str = "", refresh: bool = False) -> User | None:
    """Resolve workspace membership; authority revalidation can request fresh ORM rows."""
    # Keep the common single-org/FK lookup to one query on model-call paths.
    revoked = (
        select(TenantMembership.id)
        .where(
            TenantMembership.user_id == User.id,
            TenantMembership.tenant_id == org_id,
            TenantMembership.revoked_at.is_not(None),
        )
        .exists()
    )
    local = (
        await db.execute(
            select(User, revoked)
            .where(User.org_id == org_id, or_(User.cognito_sub == subject, User.id == subject))
            .execution_options(populate_existing=refresh)
        )
    ).one_or_none()
    if local is not None:
        return None if local[1] else local[0]
    _, memberships = await memberships_for_login(db, subject, username=username, refresh=refresh)
    pair = memberships.get(org_id)
    return pair[0] if pair else None


async def login_subject_for_user(db: AsyncSession, user: User, *, lock: bool = False) -> str | None:
    """Resolve only the platform's login link; optionally hold it through use.

    Callers requesting a lock must also hold the user row in their transaction.
    """
    if user.cognito_sub:
        return user.cognito_sub
    query = select(UserIdentity.provider_user_id).where(
        UserIdentity.user_id == user.id,
        UserIdentity.org_id == user.org_id,
        UserIdentity.provider == "cognito",
        UserIdentity.verification_method == PLACEMENT_VERIFICATION,
    )
    if lock:
        query = query.with_for_update(read=True, of=UserIdentity)
    subjects = set((await db.scalars(query)).all())
    if len(subjects) > 1:
        raise ValueError("Multiple logins own this organization's account")
    return next(iter(subjects), None)


async def link_login_to_workspace(db: AsyncSession, source: User, target: User) -> None:
    """Called only after an authorized placement or a proven workspace switch."""
    subject = await login_subject_for_user(db, source)
    if not subject:
        return
    if target.cognito_sub and target.cognito_sub != subject:
        raise ValueError("The destination account already belongs to a different login")
    target_subject = await login_subject_for_user(db, target)
    if target_subject and target_subject != subject:
        raise ValueError("The destination account already belongs to a different login")
    for user in {source.id: source, target.id: target}.values():
        existing = await db.scalar(
            select(UserIdentity).where(
                UserIdentity.org_id == user.org_id,
                UserIdentity.provider == "cognito",
                UserIdentity.provider_user_id == subject,
            )
        )
        if existing:
            if existing.user_id != user.id:
                raise ValueError("This login is already assigned to a different account in the organization")
            existing.verification_method = PLACEMENT_VERIFICATION
            existing.verified_at = utcnow()
        else:
            db.add(
                UserIdentity(
                    user_id=user.id,
                    org_id=user.org_id,
                    team_id=user.team_id,
                    provider="cognito",
                    provider_user_id=subject,
                    verification_method=PLACEMENT_VERIFICATION,
                    verified_at=utcnow(),
                )
            )
    await db.flush()
