"""Magic-link token library for identity linking.

Issue #446: Vault Phase 2b — Magic-link identity linking flow

Tokens are HS256-signed JWTs with a single-use nonce enforced via the
magic_link_nonces Postgres table.  Gateway uses Postgres (not DDB) for
consistency with its existing data tier.

Token payload shape:
    {
        "iss": "adp-gateway",
        "jti": "<nonce-uuid>",           # stored in magic_link_nonces PK
        "provider": "slack",
        "provider_user_id": "U123",
        "channel_context": "T01/C02",    # Slack workspace/channel
        "target_user_id": "user-abc",    # None when issued by internal endpoint
        "iat": <unix>,
        "exp": <iat + 900>               # 15-minute TTL
    }
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.vault import MagicLinkNonce

logger = logging.getLogger(__name__)

_TOKEN_TTL_SECONDS = 900  # 15 minutes
_ISSUER = "adp-gateway"
_ALGORITHM = "HS256"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TokenExpiredError(Exception):
    """Token exp claim is in the past."""


class TokenInvalidError(Exception):
    """Signature verification failed or payload is malformed."""


class NonceAlreadyConsumedError(Exception):
    """Nonce was already used — replay attack or double-submit."""


class NonceNotFoundError(Exception):
    """Nonce does not exist in the DB (tampered jti or from a different env)."""


class ChannelContextMismatchError(Exception):
    """channel_context at consume time differs from the one in the token."""


class TargetUserMismatchError(Exception):
    """Token's target_user_id != the logged-in user attempting to consume it."""


# ---------------------------------------------------------------------------
# Token issuance
# ---------------------------------------------------------------------------


def issue_token(
    *,
    provider: str,
    provider_user_id: str,
    channel_context: str | None,
    target_user_id: str | None,
    secret_key: str,
) -> dict[str, Any]:
    """Sign and return a magic-link token.

    Returns a dict with keys:
        token           — the signed JWT string
        jti             — the nonce UUID (= JWT jti claim)
        expires_at      — datetime when the token expires
    """
    import uuid

    now = datetime.now(UTC)
    exp = now + timedelta(seconds=_TOKEN_TTL_SECONDS)
    jti = str(uuid.uuid4())

    payload = {
        "iss": _ISSUER,
        "jti": jti,
        "provider": provider,
        "provider_user_id": provider_user_id,
        "channel_context": channel_context,
        "target_user_id": target_user_id,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }

    token = jwt.encode(payload, secret_key, algorithm=_ALGORITHM)

    return {
        "token": token,
        "jti": jti,
        "expires_at": exp,
    }


# ---------------------------------------------------------------------------
# Token verification (signature + exp, no DB check)
# ---------------------------------------------------------------------------


def verify_token(token: str, secret_key: str) -> dict[str, Any]:
    """Verify signature and expiry; return the decoded payload.

    Raises:
        TokenExpiredError  — exp is in the past
        TokenInvalidError  — bad signature or malformed token
    """
    try:
        payload = jwt.decode(
            token,
            secret_key,
            algorithms=[_ALGORITHM],
            issuer=_ISSUER,
            options={"require": ["jti", "provider", "provider_user_id", "exp", "iat"]},
        )
        return payload
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("Magic-link token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenInvalidError(f"Invalid magic-link token: {exc}") from exc


# ---------------------------------------------------------------------------
# Nonce persistence helpers
# ---------------------------------------------------------------------------


async def store_nonce(
    *,
    jti: str,
    provider: str,
    provider_user_id: str,
    channel_context: str | None,
    target_user_id: str | None,
    expires_at: datetime,
    db: AsyncSession,
) -> MagicLinkNonce:
    """Persist the nonce row BEFORE returning the token to the caller."""
    nonce = MagicLinkNonce(
        jti=jti,
        provider=provider,
        provider_user_id=provider_user_id,
        channel_context=channel_context,
        target_user_id=target_user_id,
        expires_at=expires_at,
    )
    db.add(nonce)
    await db.commit()
    await db.refresh(nonce)
    return nonce


async def consume_nonce(
    *,
    jti: str,
    channel_context: str | None,
    consuming_user_id: str,
    db: AsyncSession,
) -> MagicLinkNonce:
    """Atomically consume the nonce.

    Rules:
    1. Nonce must exist.
    2. Nonce must not yet be consumed.
    3. channel_context must match (prevents cross-channel replay).
    4. If target_user_id is set, it must match consuming_user_id.

    Returns the nonce row on success.

    Raises:
        NonceNotFoundError
        TokenExpiredError          (nonce exp has passed at DB level)
        NonceAlreadyConsumedError
        ChannelContextMismatchError
        TargetUserMismatchError
    """
    stmt = select(MagicLinkNonce).where(MagicLinkNonce.jti == jti)
    result = await db.execute(stmt)
    nonce = result.scalar_one_or_none()

    if nonce is None:
        raise NonceNotFoundError(jti)

    now = datetime.now(UTC)
    if nonce.expires_at.replace(tzinfo=UTC) < now:
        raise TokenExpiredError("Magic-link nonce has expired")

    if nonce.consumed_at is not None:
        raise NonceAlreadyConsumedError(jti)

    # channel_context binding — both None is allowed (no channel context)
    if nonce.channel_context != channel_context:
        logger.warning(
            "Magic-link channel_context mismatch jti=%s stored=%r incoming=%r",
            jti,
            nonce.channel_context,
            channel_context,
        )
        raise ChannelContextMismatchError(f"channel_context mismatch: token was issued for {nonce.channel_context!r}")

    # target_user_id check — only enforced when the token was issued for a specific user
    if nonce.target_user_id is not None and nonce.target_user_id != consuming_user_id:
        logger.warning(
            "Magic-link target_user_id mismatch jti=%s target=%r consumer=%r",
            jti,
            nonce.target_user_id,
            consuming_user_id,
        )
        raise TargetUserMismatchError(f"Token was issued for user {nonce.target_user_id!r}, but consumed by {consuming_user_id!r}")

    # Mark as consumed
    nonce.consumed_at = now
    await db.commit()
    await db.refresh(nonce)

    logger.info(
        "Magic-link nonce consumed jti=%s provider=%s provider_user_id=%s user=%s",
        jti,
        nonce.provider,
        nonce.provider_user_id,
        consuming_user_id,
    )
    return nonce
