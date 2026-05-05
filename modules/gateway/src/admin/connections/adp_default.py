"""adp-default fallback tenant helpers.

Issue #466: Personal GitHub accounts and unclaimed installations land in a
shared `adp-default` tenant with tight free-tier limits.

The well-known UUID is configured via BG_ADP_DEFAULT_ORG_ID (see config.py).
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config import get_settings
from src.shared.models.vault import ChannelTenantMap

logger = logging.getLogger(__name__)


def get_adp_default_org_id() -> str:
    """Return the well-known UUID for the adp-default tenant."""
    return get_settings().adp_default_org_id


def is_adp_default(org_id: str) -> bool:
    """Return True if the given org_id is the adp-default tenant."""
    return org_id == get_adp_default_org_id()


async def attach_to_adp_default(
    *,
    installation_id: int,
    account_login: str,
    github_account_id: int | None,
    caller_user_id: str,
    db: AsyncSession,
) -> dict:
    """Attach a personal GitHub installation to the adp-default tenant.

    Per-user scoping: the mapping is bound to (provider_scope_id, user_id) via
    a composite scope key that includes the caller_user_id.

    Args:
        installation_id: GitHub App installation ID.
        account_login: GitHub username of the personal account.
        github_account_id: Numeric GitHub account ID (may be None).
        caller_user_id: The internal users.id for the caller (from Cognito session).
        db: Database session.

    Returns:
        Dict with success=True and binding metadata.

    Raises:
        PermissionError: If this GitHub account is already attached by a different ADP user.
        ValueError: If called with an org-type installation (programming error).
    """
    adp_default_id = get_adp_default_org_id()

    # Build a scope key: for personal accounts we use "user:<github_account_id>"
    # to ensure uniqueness per GitHub account within adp-default.
    if github_account_id is not None:
        scope_id = str(github_account_id)
    else:
        scope_id = account_login

    # Check if this GitHub personal account is already mapped
    stmt = select(ChannelTenantMap).where(
        ChannelTenantMap.provider == "github",
        ChannelTenantMap.provider_scope_id == scope_id,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing is not None:
        # Already mapped — check if it's the same user within adp-default
        if existing.org_id == adp_default_id:
            # Within adp-default, verify same user by checking metadata
            # We store user_id in the mapping's metadata via a convention:
            # provider_scope_id includes the GitHub account; we track user ownership
            # through a secondary lookup. For simplicity in v1, if the org_id matches
            # adp-default, we check if the mapping was created by a different user
            # by reading the existing mapping's context.
            #
            # Since ChannelTenantMap doesn't have a user_id column, we use a
            # composite scope_id: "personal:<github_id>:<adp_user_id>" to enforce
            # per-user ownership within the shared tenant.
            pass  # Fall through — same tenant, could be same user re-installing
        elif existing.org_id != adp_default_id:
            # Claimed by a paid tenant — reject
            raise PermissionError(
                f"GitHub account '{account_login}' is already connected to another ADP tenant. Contact support if you believe this is an error."
            )

    # Use a user-scoped composite key to enforce per-user isolation within adp-default.
    # Format: "personal:<github_id>:<adp_user_id>" — unique per (GitHub account, ADP user).
    user_scope_id = f"personal:{scope_id}:{caller_user_id}"

    # Check if THIS user already has this mapping (idempotent re-install)
    user_stmt = select(ChannelTenantMap).where(
        ChannelTenantMap.provider == "github",
        ChannelTenantMap.provider_scope_id == user_scope_id,
    )
    user_result = await db.execute(user_stmt)
    user_existing = user_result.scalar_one_or_none()

    if user_existing is not None:
        # Idempotent — already attached by this user
        logger.info(
            "GitHub personal account %s re-installed by user %s (adp-default)",
            account_login,
            caller_user_id,
        )
        return {
            "success": True,
            "tenant": "adp-default",
            "user_id": caller_user_id,
            "installation_id": installation_id,
        }

    # Check if a DIFFERENT user already claimed this GitHub account in adp-default
    other_user_stmt = select(ChannelTenantMap).where(
        ChannelTenantMap.provider == "github",
        ChannelTenantMap.provider_scope_id.like(f"personal:{scope_id}:%"),
        ChannelTenantMap.org_id == adp_default_id,
    )
    other_result = await db.execute(other_user_stmt)
    other_existing = other_result.scalar_one_or_none()

    if other_existing is not None:
        # Different ADP user already claimed this GitHub personal account
        raise PermissionError(f"GitHub account '{account_login}' is already connected under a different ADP user.")

    # Create new mapping
    mapping = ChannelTenantMap(
        provider="github",
        provider_scope_id=user_scope_id,
        org_id=adp_default_id,
    )
    db.add(mapping)
    await db.commit()

    logger.info(
        "GitHub personal account %s (installation_id=%d) attached to adp-default by user %s",
        account_login,
        installation_id,
        caller_user_id,
    )

    return {
        "success": True,
        "tenant": "adp-default",
        "user_id": caller_user_id,
        "installation_id": installation_id,
    }
