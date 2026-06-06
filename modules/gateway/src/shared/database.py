"""Database module with support for IAM database authentication.

This module provides async database engine creation with two modes:
1. IAM Authentication (production): Uses boto3 to generate short-lived auth tokens
2. Password Authentication (local dev): Uses standard DATABASE_URL with embedded password

IAM auth tokens are generated fresh for each new connection to avoid expiry issues.
"""

from __future__ import annotations

import logging
import ssl
import time
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.config import get_settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# Cache IAM token for 10 minutes (tokens valid for 15 min)
_iam_token_cache: dict[str, tuple[str, float]] = {}
_IAM_TOKEN_TTL = 600  # 10 minutes


def get_rds_auth_token(host: str, port: int, username: str, region: str) -> str:
    """Generate an IAM authentication token for RDS with caching.

    Tokens are cached for 10 minutes (they expire after 15).
    """
    import boto3

    cache_key = f"{host}:{port}:{username}:{region}"
    now = time.monotonic()

    if cache_key in _iam_token_cache:
        token, created_at = _iam_token_cache[cache_key]
        if now - created_at < _IAM_TOKEN_TTL:
            return token

    client = boto3.client("rds", region_name=region)
    token = client.generate_db_auth_token(
        DBHostname=host,
        Port=port,
        DBUsername=username,
        Region=region,
    )
    _iam_token_cache[cache_key] = (token, now)
    logger.info("Generated fresh IAM auth token for RDS")
    return token


def construct_iam_database_url(
    host: str,
    port: int,
    username: str,
    dbname: str,
    region: str,
) -> str:
    """Construct a PostgreSQL connection URL with a fresh IAM auth token."""
    token = get_rds_auth_token(host, port, username, region)
    encoded_token = quote_plus(token)
    url = f"postgresql+asyncpg://{username}:{encoded_token}@{host}:{port}/{dbname}"
    return url


def get_database_url() -> str:
    """Get the database URL based on configuration.

    If RDS_IAM_AUTH is True, generates a URL with IAM auth token.
    Otherwise, returns the configured DATABASE_URL.
    """
    settings = get_settings()

    if settings.rds_iam_auth and settings.rds_host:
        return construct_iam_database_url(
            host=settings.rds_host,
            port=settings.rds_port,
            username=settings.rds_username,
            dbname=settings.rds_dbname,
            region=settings.aws_region,
        )
    else:
        return settings.database_url


def _is_sqlite_url(url: str) -> bool:
    """Check if the database URL is for SQLite."""
    return url.startswith("sqlite")


def get_engine() -> AsyncEngine:
    """Get or create the async database engine.

    For IAM auth, uses NullPool so each request gets a fresh connection
    with a fresh IAM token. This avoids the token expiry issue where
    pooled connections use stale tokens.
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        database_url = get_database_url()

        if _is_sqlite_url(database_url):
            from sqlalchemy.pool import StaticPool

            _engine = create_async_engine(
                database_url,
                echo=False,
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
        else:
            engine_kwargs: dict = {
                "echo": False,
                "pool_pre_ping": True,
            }

            if settings.rds_iam_auth and settings.rds_host:
                # Use NullPool — no connection reuse. Each request gets a fresh
                # connection with a fresh IAM token from get_database_url().
                from sqlalchemy.pool import NullPool

                engine_kwargs["poolclass"] = NullPool

                ssl_ctx = ssl.create_default_context()  # loads system CAs (/etc/ssl/certs)
                if not settings.rds_tls_verify:
                    logger.warning("RDS TLS verification disabled (BG_RDS_TLS_VERIFY=false). MITM risk!")
                    ssl_ctx.check_hostname = False
                    ssl_ctx.verify_mode = ssl.CERT_NONE
                # else: default context already has CERT_REQUIRED + check_hostname=True
                engine_kwargs["connect_args"] = {"ssl": ssl_ctx}
            else:
                engine_kwargs["pool_size"] = 20
                engine_kwargs["max_overflow"] = 10

            _engine = create_async_engine(database_url, **engine_kwargs)

    return _engine


def reset_engine() -> None:
    """Reset the database engine (useful for testing and token refresh)."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the async session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db() -> AsyncSession:
    """FastAPI dependency that yields a database session."""
    settings = get_settings()

    # For IAM auth, recreate engine periodically to pick up fresh tokens
    if settings.rds_iam_auth and settings.rds_host:
        reset_engine()

    factory = get_session_factory()
    async with factory() as session:
        yield session
