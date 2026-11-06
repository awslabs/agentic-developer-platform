"""Resolve global authority from current server state, never a stale JWT alone."""

import asyncio
import logging
from datetime import UTC, datetime

import boto3
from botocore.config import Config
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.shared.config import get_settings
from src.shared.models.platform_admin import PlatformAdminGrant

logger = logging.getLogger(__name__)
PLATFORM_ROLES = frozenset({"admin", "platform_admin"})
PLATFORM_GROUPS = frozenset({"admins", "platform-admins"})


def current_cognito_admin(subject: str, username: str = "") -> bool:
    """Compatibility for existing/bootstrap admins without an explicit grant.

    Read current IdP authority on every request; do not persist or cache a grant
    from a token. AdminGetUser's subject comparison prevents username aliases
    from lending another login its privileges.
    """
    settings = get_settings()
    if not settings.cognito_user_pool_id:
        raise RuntimeError("Cognito authority is not configured")
    client = boto3.client(
        "cognito-idp", region_name=settings.aws_region, config=Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 1})
    )
    try:
        user = client.admin_get_user(UserPoolId=settings.cognito_user_pool_id, Username=username or subject)
    except client.exceptions.UserNotFoundException:
        if username:
            return False
        # Federated logins cannot always be addressed by sub in AdminGetUser.
        # Stored actors have a proven login subject but may have no username
        # hint (GitHub onboarding does not persist one). Resolve it through the
        # existing exact-sub lookup, then retain the fresh subject/enabled/role
        # checks below. Never infer privileges from a stored display role.
        from .cognito_claims import resolve_username_by_sub

        resolved = resolve_username_by_sub(client, settings.cognito_user_pool_id, subject)
        if not resolved:
            return False
        try:
            user = client.admin_get_user(UserPoolId=settings.cognito_user_pool_id, Username=resolved)
        except client.exceptions.UserNotFoundException:
            return False
    attrs = {item["Name"]: item["Value"] for item in user.get("UserAttributes", [])}
    if attrs.get("sub") != subject or not user.get("Enabled", False):
        return False
    if attrs.get("custom:role") in PLATFORM_ROLES:
        return True
    args = {"UserPoolId": settings.cognito_user_pool_id, "Username": user["Username"]}
    while True:
        page = client.admin_list_groups_for_user(**args)
        if any(group.get("GroupName") in PLATFORM_GROUPS for group in page.get("Groups", [])):
            return True
        if not page.get("NextToken"):
            return False
        args["NextToken"] = page["NextToken"]


async def set_platform_admin_grant(db, subject: str, *, enabled: bool) -> None:
    """Join the caller's authorized role-change transaction; never commit here."""
    if not subject:
        raise ValueError("A canonical Cognito subject is required for global authority")
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    statement = insert(PlatformAdminGrant).values(cognito_sub=subject, enabled=enabled, updated_at=datetime.now(UTC))
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[PlatformAdminGrant.cognito_sub],
            set_={"enabled": statement.excluded.enabled, "updated_at": statement.excluded.updated_at},
        )
    )


async def apply_platform_admin_authority(context, db=None):
    """Replace the token's admin claim with a current authority decision.

    Database revocations take precedence over all IdP privilege sources.
    In particular, failed Cognito synchronization and token refresh cannot undo
    a committed demotion. An explicit grant still requires current IdP authority,
    so disabling the login or removing its IdP roles also takes effect. Ordinary
    callers do not query this authority store.
    """
    if not context.is_admin:
        return context
    if context.account_type != "human" or context.auth_source != "jwt" or context.expires_at <= datetime.now(UTC):
        return context.model_copy(update={"is_admin": False})
    try:
        query = select(PlatformAdminGrant.enabled).where(PlatformAdminGrant.cognito_sub == context.user_id)
        if db is None:
            from src.shared.database import get_session_factory

            factory = get_session_factory()
            async with factory() as session:
                decision = await session.scalar(query)
            if decision is False:
                return context.model_copy(update={"is_admin": False})

            # The IdP call can take seconds. Do not occupy a request-pool slot
            # while waiting for it. A second, fresh transaction fences demotions
            # committed during that wait; neither authority source is cached.
            enabled = await asyncio.to_thread(current_cognito_admin, context.user_id, context.cognito_username)
            async with factory() as session:
                if await session.scalar(query) is False:
                    enabled = False
            return context.model_copy(update={"is_admin": bool(enabled)})

        # A supplied session belongs to the caller and may contain an uncommitted
        # revocation. Never commit, roll back, or replace that transaction here.
        enabled = await db.scalar(query)
        if enabled is not False:
            enabled = await asyncio.to_thread(current_cognito_admin, context.user_id, context.cognito_username)
            # Do not overlook a demotion committed while the IdP request ran.
            decision = await db.scalar(query)
            if decision is False:
                enabled = False
        return context.model_copy(update={"is_admin": bool(enabled)})
    except HTTPException:
        raise
    except Exception:
        logger.exception("Platform administrator authority unavailable")
        raise HTTPException(503, {"error": "platform_authority_unavailable", "message": "Administrator authority could not be verified."}) from None
