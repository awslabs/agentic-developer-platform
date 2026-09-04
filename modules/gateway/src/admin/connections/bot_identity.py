"""Seeds the platform GitHub App's own bot identity at install time.

Without this, the webhook Lambda's identity_resolver has no row for the
App's own bot user (``<slug>[bot]``). The very first time the agent comments
on — or edits its own comment on — an issue, that webhook delivery 403s as
``unknown_user``. Harmless (the triggering human event already resolved and
dispatched the run), but confusing noise in webhook-delivery logs, and it
recurs for every new tenant since nothing previously seeded it automatically.

Historically this was fixed per-tenant by a manual one-off run of
``platform/scripts/seed-bot-identities.py``. This module folds the same seed
(Postgres department/team/bot-user rows + DynamoDB identity-index rows) into
the install-callback flow so every new installation gets it for free.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.identity.identity_index_writer import IdentityIndexWriter
from src.shared.models.base import new_uuid, utcnow
from src.shared.models.organization import Department, Team, User

from .github_client import GitHubAppClient

logger = logging.getLogger(__name__)

# Issue #780's seed-bot-identities.py convention — kept identical so a
# pre-existing manually-seeded tenant is recognized as already-done rather
# than getting a second department/team/user row.
BOT_AGENTS_DEPARTMENT_NAME = "bot-agents"
BOT_AGENTS_TEAM_NAME = "bot-agents"


async def seed_bot_identity(
    *,
    org_id: str,
    app_slug: str,
    github_client: GitHubAppClient | None,
    db: AsyncSession,
) -> None:
    """Best-effort: make the platform App's own bot resolve as a known identity.

    Never raises — a failure here must not block an installation. Idempotent:
    safe to call on every install-callback, including reinstalls.
    """
    if github_client is None:
        logger.warning(
            "bot-identity seed skipped for org=%s: no GitHub client available",
            org_id,
        )
        return

    bot_login = f"{app_slug}[bot]"
    try:
        bot_user = await github_client.get_bot_user(bot_login)
        bot_github_id = bot_user["id"]
    except Exception as exc:
        logger.warning(
            "bot-identity seed: could not resolve %s's numeric id for org=%s: %s",
            bot_login,
            org_id,
            exc,
        )
        return

    user_id = await _ensure_postgres_rows(
        org_id=org_id, app_slug=app_slug, db=db
    )
    if user_id is None:
        return

    try:
        writer = IdentityIndexWriter()
        success = await writer.put_user_identity(
            provider_user_id=str(bot_github_id),
            user_id=user_id,
            org_id=org_id,
            provider_username=bot_login,
            user_kind="bot",
            bot_kind=app_slug,
        )
        if success:
            logger.info(
                "bot-identity seed: wrote identity-index for %s (github_id=%s) org=%s",
                bot_login,
                bot_github_id,
                org_id,
            )
        else:
            logger.warning(
                "bot-identity seed: identity-index write failed for %s org=%s",
                bot_login,
                org_id,
            )
    except Exception:
        logger.exception(
            "bot-identity seed: DDB write failed for %s org=%s", bot_login, org_id
        )


async def _ensure_postgres_rows(*, org_id: str, app_slug: str, db: AsyncSession) -> str | None:
    """Ensure the sentinel department/team/bot-user rows exist. Returns user_id."""
    try:
        department = (
            await db.execute(
                select(Department).where(
                    Department.org_id == org_id,
                    Department.name == BOT_AGENTS_DEPARTMENT_NAME,
                )
            )
        ).scalar_one_or_none()
        if department is None:
            department = Department(
                id=new_uuid(),
                org_id=org_id,
                name=BOT_AGENTS_DEPARTMENT_NAME,
                description="Sentinel department for bot agents",
                created_at=utcnow(),
            )
            db.add(department)
            await db.flush()

        team = (
            await db.execute(
                select(Team).where(
                    Team.org_id == org_id,
                    Team.name == BOT_AGENTS_TEAM_NAME,
                )
            )
        ).scalar_one_or_none()
        if team is None:
            team = Team(
                id=new_uuid(),
                org_id=org_id,
                department_id=department.id,
                name=BOT_AGENTS_TEAM_NAME,
                description="Sentinel team for bot agents",
                created_at=utcnow(),
            )
            db.add(team)
            await db.flush()

        user = (
            await db.execute(
                select(User).where(
                    User.org_id == org_id,
                    User.user_kind == "bot",
                    User.bot_kind == app_slug,
                )
            )
        ).scalar_one_or_none()
        if user is None:
            user = User(
                id=new_uuid(),
                org_id=org_id,
                team_id=team.id,
                email=f"{app_slug}@bot.adp.local",
                name=f"{app_slug} bot",
                role="agent",
                user_kind="bot",
                bot_kind=app_slug,
                is_shadow=False,
                created_at=utcnow(),
            )
            db.add(user)
            await db.flush()

        await db.commit()
    except Exception:
        logger.exception("bot-identity seed: Postgres write failed for org=%s", org_id)
        await db.rollback()
        return None

    return user.id
