"""Canonical identity resolution: Cognito sub to users.id.

This module provides the gateway's standard resolver for mapping a Cognito
``sub`` claim (the provider identity carried in JWT tokens) to the canonical
``users.id`` UUID stored in Postgres.

The resolver is the complement of ``src/shared/services/canonical_user.py``
which resolves by primary key (``users.id`` -> User row). This module resolves
in the opposite direction: ``cognito_sub`` -> ``users.id``.

Contract:
- If a ``users`` row exists with ``cognito_sub == sub``, return its ``id``.
- If no matching row exists (identity not yet provisioned), fall back to the
  raw ``cognito_sub`` value so callers degrade gracefully rather than failing.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.organization import User

logger = logging.getLogger("bedrockgateway.identity")


async def resolve_canonical_user_id(db: AsyncSession, cognito_sub: str) -> str:
    """Resolve a Cognito sub to the canonical ADP user_id (``users.id``).

    Args:
        db: Async database session.
        cognito_sub: The ``sub`` claim from the Cognito JWT (i.e.
            ``TokenContext.user_id``).

    Returns:
        The canonical ``users.id`` UUID if a matching row exists, otherwise
        the raw ``cognito_sub`` value (graceful fallback for unprovisioned
        identities).
    """
    canonical = await db.scalar(select(User.id).where(User.cognito_sub == cognito_sub))
    if canonical:
        return canonical
    logger.warning(
        "No users row for cognito_sub=%s; falling back to raw token user_id",
        cognito_sub,
    )
    return cognito_sub
