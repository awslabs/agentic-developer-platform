"""Register the platform App's bot without re-homing it on another installation.

A GitHub App has one bot account across all installations. Postgres therefore
owns one canonical bot user and explicit tenant memberships; both DynamoDB
indexes project that same identity and its complete membership set.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.identity.identity_index_writer import IdentityIndexWriter
from src.admin.memberships import upsert_tenant_membership
from src.shared.identity import format_person_anchor
from src.shared.models.base import new_uuid
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserIdentity

from .github_client import GitHubAppClient

logger = logging.getLogger(__name__)

BOT_AGENTS_DEPARTMENT_NAME = "bot-agents"
BOT_AGENTS_TEAM_NAME = "bot-agents"
# Preserve IDs minted by platform/scripts/seed-bot-identities.py (#780).
_BOT_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


async def seed_bot_identity(
    *,
    org_id: str,
    installation_id: int,
    app_slug: str,
    github_client: GitHubAppClient | None,
    db: AsyncSession,
) -> None:
    """Best-effort seed on an authorized install callback, including reinstalls.

    Own the seed's transactions so a failure cannot commit or roll back the
    caller's work. PostgreSQL locks serialize callbacks for the same GitHub bot
    across gateway replicas. The second transaction re-reads committed state
    under the same lock before projecting it, so a delayed callback cannot
    overwrite a newer callback's membership list.
    """
    if github_client is None:
        logger.warning("bot-identity seed skipped for org=%s: no GitHub client available", org_id)
        return

    bot_login = f"{app_slug}[bot]"
    try:
        bot = await github_client.get_bot_user(bot_login, installation_id=installation_id)
        bot_id = bot["id"]
        if type(bot_id) is not int or bot_id <= 0 or bot.get("type") != "Bot" or bot.get("login", "").lower() != bot_login.lower():
            raise ValueError("GitHub lookup did not return the requested bot account")

        async with AsyncSession(bind=db.bind, expire_on_commit=False) as seed_db:
            async with seed_db.begin():
                await _lock_bot(seed_db, bot_id)
                user = await _ensure_bot_user(seed_db, org_id=org_id, app_slug=app_slug, bot_id=bot_id, bot_login=bot_login)
                # The home tenant remains stable, including for legacy seeds.
                for tenant_id in sorted({user.org_id, org_id}):
                    await upsert_tenant_membership(seed_db, user_id=user.id, tenant_id=tenant_id, role="member", joined_via="github_app_install")
                user_id = user.id

            # Only durable Postgres memberships may be published. Reacquiring
            # the lock also orders writes when callbacks interleave at commit.
            async with seed_db.begin():
                await _lock_bot(seed_db, bot_id)
                user = await seed_db.get(User, user_id, populate_existing=True)
                member_org_ids = sorted((await seed_db.scalars(select(TenantMembership.tenant_id).where(TenantMembership.user_id == user_id))).all())
                success = await IdentityIndexWriter().put_user_identity(
                    provider_user_id=str(bot_id),
                    user_id=user.id,
                    org_id=user.org_id,
                    provider_username=bot_login,
                    user_kind="bot",
                    bot_kind=user.bot_kind,
                    member_org_ids=member_org_ids,
                    # #5664 (A10): matches the UserIdentity row _ensure_bot_user
                    # writes. This is the platform's own GitHub App bot, seeded by
                    # the install callback rather than claimed by a user.
                    verification_method="admin_manual",
                )
                if not success:
                    logger.warning("bot-identity seed: identity-index write failed for %s org=%s; retry the install callback", bot_login, org_id)
    except Exception:
        logger.exception("bot-identity seed failed for %s org=%s (installation remains valid)", bot_login, org_id)


async def _lock_bot(db: AsyncSession, bot_id: int) -> None:
    if db.get_bind().dialect.name == "postgresql":
        # Transaction-scoped locks release on rollback/connection failure too.
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('adp-bot-identity'), hashtext(:bot_id))"), {"bot_id": str(bot_id)})


async def _ensure_bot_user(db: AsyncSession, *, org_id: str, app_slug: str, bot_id: int, bot_login: str) -> User:
    identities = (await db.scalars(select(UserIdentity).where(UserIdentity.provider == "github", UserIdentity.provider_user_id == str(bot_id)))).all()
    identity_user_ids = {identity.user_id for identity in identities}
    if len(identity_user_ids) > 1:
        raise ValueError("Bot has conflicting canonical identity links; operator reconciliation required")

    user = await db.get(User, next(iter(identity_user_ids))) if identity_user_ids else None
    if identity_user_ids and user is None:
        raise ValueError("Bot identity refers to a missing user")
    if user is None:
        legacy_id = str(uuid.uuid5(_BOT_NAMESPACE, format_person_anchor(str(bot_id))))
        user = await db.get(User, legacy_id)
    if user is None:
        # Adopt an earlier install-time seed with a random UUID without changing
        # its user ID or home tenant. Ambiguous rows require explicit repair.
        candidates = (await db.scalars(select(User).where(User.user_kind == "bot", User.bot_kind == app_slug))).all()
        if len(candidates) > 1:
            raise ValueError("Multiple unlinked bot users exist; operator reconciliation required")
        user = candidates[0] if candidates else None
    if user is not None and user.user_kind != "bot":
        raise ValueError("Refusing to replace a human identity with a bot")
    if user is not None and not identities:
        other_identity = await db.scalar(
            select(UserIdentity.id).where(
                UserIdentity.user_id == user.id,
                UserIdentity.provider == "github",
                UserIdentity.provider_user_id != str(bot_id),
            )
        )
        if other_identity is not None:
            raise ValueError("Existing bot user belongs to a different GitHub account")

    if user is None:
        # Different Apps installed into the same org share the sentinel rows.
        # Lock the org before creating them, even though each App has its own
        # bot lock. This avoids duplicate department/team rows on first install.
        await db.scalar(select(Organization.id).where(Organization.id == org_id).with_for_update())
        department = await db.scalar(select(Department).where(Department.org_id == org_id, Department.name == BOT_AGENTS_DEPARTMENT_NAME))
        if department is None:
            department = Department(id=new_uuid(), org_id=org_id, name=BOT_AGENTS_DEPARTMENT_NAME, description="Sentinel department for bot agents")
            db.add(department)
            await db.flush()
        team = await db.scalar(select(Team).where(Team.org_id == org_id, Team.name == BOT_AGENTS_TEAM_NAME))
        if team is None:
            team = Team(
                id=new_uuid(), org_id=org_id, department_id=department.id, name=BOT_AGENTS_TEAM_NAME, description="Sentinel team for bot agents"
            )
            db.add(team)
            await db.flush()
        user = User(
            id=str(uuid.uuid5(_BOT_NAMESPACE, format_person_anchor(str(bot_id)))),
            org_id=org_id,
            team_id=team.id,
            email=f"{app_slug}@bot.adp.local",
            name=f"{app_slug} bot",
            role="agent",
            user_kind="bot",
            bot_kind=app_slug,
            is_shadow=False,
        )
        db.add(user)
        await db.flush()

    # A single provider link makes the bot visible to canonical resolution and
    # membership reconciliation, not only to the DynamoDB webhook cache.
    if not identities:
        db.add(
            UserIdentity(
                org_id=user.org_id,
                user_id=user.id,
                team_id=user.team_id,
                provider="github",
                provider_user_id=str(bot_id),
                provider_username=bot_login,
                verification_method="admin_manual",
            )
        )
        await db.flush()
    return user
