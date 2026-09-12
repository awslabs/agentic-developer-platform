"""Resolve a login to its organization-local accounts without matching email.

GitHub broker usernames contain the authenticated immutable GitHub id. Native
Cognito logins use explicit links written by the platform's org-placement path.
An arbitrary self-linked external identity is never proof of another account's
workspace access. Memberships, not connections or GitHub installations, grant
workspace access.
"""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import utcnow
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity

PLACEMENT_VERIFICATION = "org_placement"


async def login_user(db: AsyncSession, subject: str) -> User | None:
    return (await db.execute(select(User).where(or_(User.cognito_sub == subject, User.id == subject)))).scalar_one_or_none()


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
        predicates.append((UserIdentity.provider == "github") & (UserIdentity.provider_user_id == provider_user_id))
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


async def memberships_for_login(
    db: AsyncSession, subject: str, *, username: str = ""
) -> tuple[User | None, dict[str, tuple[User, TenantMembership | None]]]:
    user = await login_user(db, subject)
    if user is None:
        return None, {}
    ids = await linked_user_ids(db, user, username=username)
    rows = (
        await db.execute(select(User, TenantMembership).join(TenantMembership, TenantMembership.user_id == User.id).where(User.id.in_(ids)))
    ).all()
    by_org: dict[str, tuple[User, TenantMembership | None]] = {}
    for member, membership in rows:
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
    if user.org_id and user.org_id not in by_org:
        by_org[user.org_id] = user, None
    return user, by_org


async def workspace_user(db: AsyncSession, subject: str, org_id: str, *, username: str = "") -> User | None:
    # Keep the common single-org/FK lookup to one query on model-call paths.
    local = (await db.execute(select(User).where(User.org_id == org_id, or_(User.cognito_sub == subject, User.id == subject)))).scalar_one_or_none()
    if local is not None:
        return local
    _, memberships = await memberships_for_login(db, subject, username=username)
    pair = memberships.get(org_id)
    return pair[0] if pair else None


async def login_subject_for_user(db: AsyncSession, user: User) -> str | None:
    if user.cognito_sub:
        return user.cognito_sub
    subjects = set(
        (
            await db.scalars(
                select(UserIdentity.provider_user_id).where(
                    UserIdentity.user_id == user.id,
                    UserIdentity.org_id == user.org_id,
                    UserIdentity.provider == "cognito",
                    UserIdentity.verification_method == PLACEMENT_VERIFICATION,
                )
            )
        ).all()
    )
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
