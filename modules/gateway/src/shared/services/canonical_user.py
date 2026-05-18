"""Canonical user resolver for credential endpoints.

Issue #700: credential resolver should canonicalize via Cognito sub before
scoping vault lookup.

The resolver implements monotonic-narrowing canonical-user resolution:
- If the inbound user row has `cognito_sub` set, it IS the canonical user.
  Return as-is.
- If `cognito_sub` is NULL, log a structured WARNING and fall through with
  the inbound row unchanged (shadow users are legitimate per #446).

Security invariant: the resolver MUST NOT walk sideways via shared
`user_identities` rows to find a different `users.id`. That would be a
privilege-escalation vector.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.organization import User

logger = logging.getLogger(__name__)


async def resolve_canonical_user(
    db: AsyncSession,
    user_id: str,
    *,
    calling_endpoint: str = "unknown",
) -> User:
    """Resolve the canonical user for credential lookups.

    Args:
        db: Async database session.
        user_id: The inbound user PK (users.id).
        calling_endpoint: Name of the calling endpoint for structured logging.

    Returns:
        The User row to use for credential queries (always the same row
        identified by user_id — we never cross user boundaries).

    Raises:
        None — if the user has no cognito_sub, we log a warning and return
        the user unchanged. Callers should handle user-not-found before
        calling this function.
    """
    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        return None  # type: ignore[return-value]

    if user.cognito_sub is None:
        logger.warning(
            "Canonical user resolution: user has no cognito_sub, falling through with inbound row unchanged.",
            extra={
                "user_id": user_id,
                "is_shadow": user.is_shadow,
                "calling_endpoint": calling_endpoint,
                "org_id": user.org_id,
            },
        )

    # Whether cognito_sub is set or not, we return the same row.
    # The resolver never crosses user boundaries.
    return user
