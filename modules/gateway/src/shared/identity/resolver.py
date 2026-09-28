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
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.organization import User

logger = logging.getLogger("bedrockgateway.identity")


async def resolve_canonical_user_id(db: AsyncSession, cognito_sub: str) -> str:
    """Resolve a Cognito sub to the canonical ADP user_id (``users.id``).

    Args:
        db: Async database session. MUST be a session bound to the gateway DB —
            the ``users`` table lives there, not in the agent_context DB. Passing
            the wrong session raises UndefinedTableError, which is caught below
            and degrades to the raw sub (see #2213 follow-up).
        cognito_sub: The ``sub`` claim from the Cognito JWT (i.e.
            ``TokenContext.user_id``).

    Returns:
        The canonical ``users.id`` UUID if a matching row exists, otherwise
        the raw ``cognito_sub`` value (graceful fallback for unprovisioned
        identities, or if the ``users`` table is unreachable on this session).
    """
    try:
        canonical = await db.scalar(select(User.id).where(User.cognito_sub == cognito_sub))
    except SQLAlchemyError:
        # Defense-in-depth: a wrong-DB session (no ``users`` table) or a transient
        # DB error must not 500 the caller. Degrade to the raw sub — the same
        # graceful-fallback contract as "no matching row".
        logger.warning(
            "users lookup failed for cognito_sub=%s (wrong DB session or DB error); falling back to raw token user_id",
            cognito_sub,
            exc_info=True,
        )
        return cognito_sub
    if canonical:
        return canonical
    logger.warning(
        "No users row for cognito_sub=%s; falling back to raw token user_id",
        cognito_sub,
    )
    return cognito_sub
