"""Authenticated Redis/ElastiCache client factory (Issue #4342).

## Why this exists

``infra/modules/redis/`` moved ElastiCache to IAM authentication: the ``default``
user is disabled (``access_string = "off ~* -@all"``), an IAM-auth user + user
group are provisioned, and transit encryption is on. The application never
followed: every call site built its client with ``redis.from_url(BG_REDIS_URL)``
against a bare ``rediss://host:port/0`` carrying no credential, so every
connection authenticated as the *disabled* ``default`` user and was rejected with
``Authentication required``.

That failure was swallowed everywhere it mattered. The reservation engine
(#4287) catches the connection exception and returns ``None``, which the
enforcement path treats as "degrade, allow" — so the per-run / per-chain spend
cap was inert platform-wide with nothing surfaced to anyone. This module is the
missing half of that migration.

## Why a presigned URL and not ``generate_auth_token()``

The Terraform comment (and the issue quoting it) says the app "uses IAM tokens
generated via the AWS SDK's ``generate_auth_token()`` method". No such call
exists for ElastiCache — unlike RDS, which really does expose
``boto3.client("rds").generate_db_auth_token`` (see
:func:`src.shared.database.get_rds_auth_token`). The documented ElastiCache
mechanism is a SigV4-**presigned** ``Action=connect`` request whose signed URL,
minus its scheme, is used as the password. So the *shape* of the RDS path is
mirrored here — a token function plus a short cache — but the token itself is
built with :class:`botocore.signers.RequestSigner`.

## Why token refresh needs no pool teardown

IAM tokens are short-lived (~15 min), and a token minted once at startup is a
latent timed outage: connections work until it expires, then fail en masse.

The RDS path solves this by tearing the engine down per request
(``reset_engine()`` + ``NullPool``) so every connection re-reads a fresh token.
Redis does not need that hammer. ``redis.asyncio``'s connection handshake calls
``CredentialProvider.get_credentials()`` on *every* connection — including pool
reconnects — so supplying a provider re-mints the token exactly when a
connection is actually established. Pools stay intact, and there is no window in
which a live connection holds an expired token.

The token is still cached below its own expiry (same reasoning and same margin as
``database.py``) so that connection churn does not re-sign on every single
handshake.
"""

from __future__ import annotations

import time
from urllib.parse import ParseResult, urlencode, urlunparse

import redis.asyncio as redis
from redis.credentials import CredentialProvider

from src.shared.config import get_settings
from src.shared.logging import get_logger

logger = get_logger(__name__)

# ElastiCache caps the presigned connect token's lifetime at 15 minutes.
_TOKEN_EXPIRY_SECONDS = 900

# Serve a cached token for 10 of those 15 minutes, leaving a 5-minute margin so a
# token handed to a connection is never close to expiring. Same numbers, and the
# same reasoning, as ``database.py``'s ``_IAM_TOKEN_TTL``.
_TOKEN_CACHE_TTL = 600

# (cache_name, username, region) -> (token, minted_at_monotonic)
_token_cache: dict[tuple[str, str, str], tuple[str, float]] = {}


def reset_token_cache() -> None:
    """Drop all cached tokens. For tests, and for forcing a re-mint."""
    _token_cache.clear()


def get_elasticache_auth_token(cache_name: str, username: str, region: str) -> str:
    """Generate an ElastiCache IAM auth token, cached below its expiry.

    The token is a SigV4-presigned ``Action=connect`` URL with the ``https://``
    scheme stripped; ElastiCache validates the signature and the ``User``
    parameter against the ``elasticache:Connect`` grant.

    Args:
        cache_name: The **replication group id** (not the endpoint hostname).
            This is what ElastiCache signs and validates against; passing the
            host here produces a token the server rejects.
        username: The provisioned IAM-auth user name.
        region: Region of the replication group.
    """
    import botocore.session
    from botocore.model import ServiceId
    from botocore.signers import RequestSigner

    cache_key = (cache_name, username, region)
    now = time.monotonic()

    cached = _token_cache.get(cache_key)
    if cached is not None:
        token, minted_at = cached
        if now - minted_at < _TOKEN_CACHE_TTL:
            return token

    session = botocore.session.get_session()
    signer = RequestSigner(
        ServiceId("elasticache"),
        region,
        "elasticache",
        "v4",
        session.get_credentials(),
        session.get_component("event_emitter"),
    )

    url = urlunparse(
        ParseResult(
            scheme="https",
            netloc=cache_name,
            path="/",
            query=urlencode({"Action": "connect", "User": username}),
            params="",
            fragment="",
        )
    )
    signed_url = signer.generate_presigned_url(
        {"method": "GET", "url": url, "body": {}, "headers": {}, "context": {}},
        operation_name="connect",
        expires_in=_TOKEN_EXPIRY_SECONDS,
        region_name=region,
    )

    # The scheme is stripped: ElastiCache expects the signed request *without* it.
    token = signed_url.removeprefix("https://")
    _token_cache[cache_key] = (token, now)
    logger.info("Generated fresh IAM auth token for ElastiCache")
    return token


class ElastiCacheIAMCredentialProvider(CredentialProvider):
    """Supplies ``(username, iam_token)`` on every Redis connection handshake.

    ``redis.asyncio`` invokes :meth:`get_credentials` each time a connection is
    established, so this is what makes token refresh automatic — see the module
    docstring on why no pool teardown is needed.
    """

    def __init__(self, username: str, cache_name: str, region: str) -> None:
        self._username = username
        self._cache_name = cache_name
        self._region = region

    def get_credentials(self) -> tuple[str, str]:
        return (
            self._username,
            get_elasticache_auth_token(self._cache_name, self._username, self._region),
        )


def create_redis_client(redis_url: str, **kwargs) -> redis.Redis:
    """Build a Redis client, IAM-authenticated when configured.

    This is the ONE place gateway Redis clients are constructed. Call sites pass
    their own ``encoding`` / ``decode_responses`` through ``kwargs`` unchanged;
    the only thing added here is authentication.

    When ``BG_REDIS_IAM_AUTH`` is false the client is built exactly as before, so
    docker-compose, local dev, and the test suite are unaffected.

    Args:
        redis_url: Host/port/TLS template (``rediss://host:port/0``). It carries
            no credential by design — there is no password to embed, and the
            username is supplied by the credential provider.
        **kwargs: Passed through to ``redis.asyncio.from_url``.
    """
    settings = get_settings()

    if not settings.redis_iam_auth:
        return redis.from_url(redis_url, **kwargs)

    if not settings.redis_username or not settings.redis_cache_name:
        # Loud, specific, and actionable — the failure this module exists to end
        # was a generic "Authentication required" that nobody could trace back to
        # a missing setting. Callers still degrade (they catch broadly), but the
        # log now names the cause.
        raise RuntimeError(
            "BG_REDIS_IAM_AUTH is true but BG_REDIS_USERNAME / BG_REDIS_CACHE_NAME are not set; cannot generate an ElastiCache IAM auth token."
        )

    provider = ElastiCacheIAMCredentialProvider(
        username=settings.redis_username,
        cache_name=settings.redis_cache_name,
        region=settings.aws_region,
    )
    return redis.from_url(redis_url, credential_provider=provider, **kwargs)
