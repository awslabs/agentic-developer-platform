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
from sqlalchemy import update as sa_update
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
    delivery_method: str | None = None,
) -> MagicLinkNonce:
    """Persist the nonce row BEFORE returning the token to the caller.

    ``delivery_method`` records HOW the link reaches the claimed account, so the
    consume path can tell a private delivery from a post in a conversation other
    people can read (#5664, A10). It defaults to ``None``, which
    ``delivery_proves_ownership`` treats as unproven — a minter that does not say
    how it delivered gets an unproven link, not a trusted one.
    """
    nonce = MagicLinkNonce(
        jti=jti,
        provider=provider,
        provider_user_id=provider_user_id,
        channel_context=channel_context,
        target_user_id=target_user_id,
        expires_at=expires_at,
        delivery_method=delivery_method,
    )
    db.add(nonce)
    await db.commit()
    await db.refresh(nonce)
    return nonce


class ClaimNotBoundToNonceError(Exception):
    """A signed claim disagrees with the stored nonce it names.

    The JWT and the nonce row both carry provider / provider_user_id. Only the row
    is authoritative: it was written by the minter, whereas the token is data the
    consumer hands back. They can only diverge if a token was altered or crafted,
    so the mismatch is refused rather than resolved in either direction.
    """


async def consume_nonce(
    *,
    jti: str,
    channel_context: str | None,
    consuming_user_id: str,
    db: AsyncSession,
    claimed_provider: str | None = None,
    claimed_provider_user_id: str | None = None,
) -> MagicLinkNonce:
    """Consume the nonce exactly once, without committing (#5664, A10).

    Two properties this function is responsible for, beyond its original checks:

    **One-time consumption is atomic.** It used to read the row, test
    ``consumed_at IS NULL`` in Python, then write and commit. Two concurrent
    requests could both pass the test before either wrote, so a single nonce could
    be redeemed twice — and on this flow each redemption writes an identity link.
    The claim is now staked with a single conditional UPDATE whose WHERE clause
    carries the ``consumed_at IS NULL`` predicate, so the database decides the
    winner and the loser sees ``NonceAlreadyConsumedError``.

    **Consumption commits with the identity write, not before it.** This function
    deliberately does NOT commit. It flushes, leaving the consumption pending in
    the caller's transaction so that "nonce spent" and "identity linked" land in
    one commit. Committing here would make the two separable: a failure between
    them burned the nonce without linking anything, which is unrecoverable for the
    user because the nonce cannot be reissued by them.

    ``claimed_provider`` / ``claimed_provider_user_id`` bind the signed token to
    the stored row. When supplied they must equal the row's values; the caller
    should then use the ROW's values for the identity write.

    Raises:
        NonceNotFoundError
        TokenExpiredError          (nonce exp has passed at DB level)
        NonceAlreadyConsumedError
        ChannelContextMismatchError
        TargetUserMismatchError
        ClaimNotBoundToNonceError
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

    # Every signed claim must agree with the row before the nonce is spent, so a
    # tampered token cannot redirect a legitimate nonce at a different account.
    if claimed_provider is not None and claimed_provider != nonce.provider:
        logger.warning(
            "Magic-link provider not bound to nonce jti=%s stored=%r claimed=%r",
            jti,
            nonce.provider,
            claimed_provider,
        )
        raise ClaimNotBoundToNonceError("Token provider does not match the issued nonce")

    if claimed_provider_user_id is not None and claimed_provider_user_id != nonce.provider_user_id:
        logger.warning(
            "Magic-link provider_user_id not bound to nonce jti=%s stored=%r claimed=%r",
            jti,
            nonce.provider_user_id,
            claimed_provider_user_id,
        )
        raise ClaimNotBoundToNonceError("Token provider_user_id does not match the issued nonce")

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

    # Stake the claim in one statement. The predicate is what makes this safe under
    # concurrency: whichever request the database serialises second matches zero
    # rows and is refused, so two callers cannot both proceed to write a link.
    claim = await db.execute(sa_update(MagicLinkNonce).where(MagicLinkNonce.jti == jti, MagicLinkNonce.consumed_at.is_(None)).values(consumed_at=now))
    if claim.rowcount != 1:
        logger.warning("Magic-link nonce lost the consume race jti=%s consumer=%s", jti, consuming_user_id)
        raise NonceAlreadyConsumedError(jti)

    # Flush, do NOT commit: the caller commits this together with the identity row.
    await db.flush()
    # The UPDATE bypassed the ORM's view of this instance, so refresh the attribute
    # rather than leaving the caller with a stale `consumed_at=None`.
    await db.refresh(nonce)

    logger.info(
        "Magic-link nonce consumed jti=%s provider=%s provider_user_id=%s user=%s delivery=%s",
        jti,
        nonce.provider,
        nonce.provider_user_id,
        consuming_user_id,
        nonce.delivery_method,
    )
    return nonce
