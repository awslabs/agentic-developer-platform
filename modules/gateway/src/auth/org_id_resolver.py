"""Shared helper to resolve effective org_id for credential operations.

Issue #600: GitHub-federated users get org_id='' on AWS link because their
Cognito token doesn't carry custom:org_id. This helper falls back to the
Postgres users.org_id (source of truth) when the token claim is empty.

Any route that writes or queries UserCredential using token_context.org_id
should call resolve_effective_org_id() instead of reading the field directly.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.organization import User
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger(__name__)


async def resolve_effective_org_id(
    token_context: TokenContext,
    db: AsyncSession,
) -> str:
    """Return token_context.org_id if populated, else fall back to users.org_id.

    Defends against GitHub-federated users whose refresh tokens were issued
    before the auth-broker wrote custom:org_id to Cognito (Issue #600).

    Raises:
        HTTPException(409) if neither the token nor the DB row has an org_id.
    """
    if token_context.org_id:
        return token_context.org_id

    stmt = select(User.org_id).where(User.cognito_sub == token_context.user_id)
    result = await db.execute(stmt)
    db_org_id = result.scalar_one_or_none()

    if not db_org_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "user_not_assigned_to_org",
                "message": "User has no org assignment; complete onboarding first.",
            },
        )

    logger.warning(
        "Resolved org_id from DB fallback (token had empty org_id): user_id=%s org_id=%s",
        token_context.user_id,
        db_org_id,
    )
    return db_org_id
