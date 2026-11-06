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

# Path to the AWS RDS CA bundle downloaded in the Dockerfile.
# This bundle covers all AWS regions and is required for TLS verification
# against RDS instances (the system trust store does NOT validate the chain).
RDS_CA_BUNDLE_PATH = "/etc/ssl/certs/rds-global-bundle.pem"

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_accounting_engine: AsyncEngine | None = None
_accounting_session_factory: async_sessionmaker[AsyncSession] | None = None

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


def _split_enabled(settings, database_url: str) -> bool:
    return settings.rds_split_pool_enabled is True and settings.rds_pool_enabled is True and not _is_sqlite_url(database_url)


def _create_engine(database_url: str, settings, *, accounting: bool = False) -> AsyncEngine:
    if _is_sqlite_url(database_url):
        from sqlalchemy.pool import StaticPool

        return create_async_engine(
            database_url,
            echo=False,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )

    engine_kwargs: dict = {"echo": False, "pool_pre_ping": True}
    split = _split_enabled(settings, database_url)
    if split:
        engine_kwargs.update(
            pool_size=settings.rds_accounting_pool_size if accounting else settings.rds_request_pool_size,
            max_overflow=0,
            pool_timeout=settings.rds_pool_timeout_seconds,
            pool_recycle=settings.rds_pool_recycle_seconds,
        )

    if settings.rds_iam_auth and settings.rds_host:
        if not split:
            if settings.rds_pool_enabled is True:
                engine_kwargs.update(
                    pool_size=settings.rds_pool_size,
                    max_overflow=settings.rds_pool_max_overflow,
                    pool_timeout=settings.rds_pool_timeout_seconds,
                    pool_recycle=settings.rds_pool_recycle_seconds,
                )
            else:
                from sqlalchemy.pool import NullPool

                engine_kwargs["poolclass"] = NullPool

        if settings.rds_tls_verify:
            ssl_ctx = ssl.create_default_context(cafile=RDS_CA_BUNDLE_PATH)
        else:
            logger.warning("RDS TLS verification disabled (BG_RDS_TLS_VERIFY=false). MITM risk!")
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        engine_kwargs["connect_args"] = {
            "ssl": ssl_ctx,
            "password": lambda: get_rds_auth_token(settings.rds_host, settings.rds_port, settings.rds_username, settings.aws_region),
        }
    elif not split:
        engine_kwargs["pool_size"] = 20
        engine_kwargs["max_overflow"] = 10

    if split:
        engine_kwargs.setdefault("connect_args", {})["timeout"] = settings.rds_connect_timeout_seconds

    engine = create_async_engine(database_url, **engine_kwargs)
    if split:
        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "begin")
        def set_transaction_limits(connection):
            connection.exec_driver_sql(f"SET LOCAL lock_timeout = '{settings.rds_lock_timeout_ms}ms'")
            connection.exec_driver_sql(f"SET LOCAL statement_timeout = '{settings.rds_statement_timeout_ms}ms'")

    return engine


def get_engine() -> AsyncEngine:
    """Get or create the request-serving engine."""
    global _engine
    if _engine is None:
        _engine = _create_engine(get_database_url(), get_settings())
    return _engine


def get_accounting_engine() -> AsyncEngine:
    """Keep settlement off request connections only when split mode is enabled."""
    global _accounting_engine
    settings = get_settings()
    if settings.rds_split_pool_enabled is not True:
        return get_engine()
    if not _split_enabled(settings, get_database_url()):
        return get_engine()
    if _accounting_engine is None:
        _accounting_engine = _create_engine(get_database_url(), settings, accounting=True)
    return _accounting_engine


def reset_engine() -> None:
    """Reset cached engines and session factories (useful for testing and token refresh)."""
    global _engine, _session_factory, _accounting_engine, _accounting_session_factory
    _engine = None
    _session_factory = None
    _accounting_engine = None
    _accounting_session_factory = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the request session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


def get_accounting_session_factory() -> async_sessionmaker[AsyncSession]:
    """Use the original factory unless split mode is explicitly enabled."""
    global _accounting_session_factory
    if get_settings().rds_split_pool_enabled is not True:
        return get_session_factory()
    if not _split_enabled(get_settings(), get_database_url()):
        return get_session_factory()
    if _accounting_session_factory is None:
        _accounting_session_factory = async_sessionmaker(get_accounting_engine(), expire_on_commit=False)
    return _accounting_session_factory


async def get_db() -> AsyncSession:
    """FastAPI dependency that releases failed or cancelled transactions."""
    async with get_session_factory()() as session:
        session.info["http_request_session"] = True
        try:
            yield session
        except BaseException:
            try:
                await session.rollback()
            except BaseException:
                await session.invalidate()
            raise
