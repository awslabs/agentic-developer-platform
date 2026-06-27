"""Database module for the agent_context database (Knowledge Layer registry).

Issue #2182: The knowledge_assets table lives in the `agent_context` database,
not `bedrockgateway`. This module provides a dedicated async engine and session
factory targeting `agent_context` so the gateway's knowledge routes can
read/write the registry directly.

Mirrors the IAM auth pattern from database.py but uses `agent_context_dbname`
from config instead of `rds_dbname`.
"""

from __future__ import annotations

import logging
import ssl
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.config import get_settings
from src.shared.database import RDS_CA_BUNDLE_PATH, get_rds_auth_token

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

_ac_engine: AsyncEngine | None = None
_ac_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_agent_context_database_url() -> str:
    """Construct the database URL for the agent_context database.

    Uses the same host/port/user as the gateway DB but targets agent_context_dbname.
    """
    from urllib.parse import quote_plus

    settings = get_settings()

    if settings.rds_iam_auth and settings.rds_host:
        token = get_rds_auth_token(settings.rds_host, settings.rds_port, settings.rds_username, settings.aws_region)
        encoded_token = quote_plus(token)
        return f"postgresql+asyncpg://{settings.rds_username}:{encoded_token}@{settings.rds_host}:{settings.rds_port}/{settings.agent_context_dbname}"
    else:
        # Local dev: swap dbname in the fallback URL
        base = settings.database_url
        # Replace the last path segment (dbname) with agent_context_dbname
        if "/" in base:
            base_prefix = base.rsplit("/", 1)[0]
            return f"{base_prefix}/{settings.agent_context_dbname}"
        return base


def _get_agent_context_engine() -> AsyncEngine:
    """Get or create the async engine for agent_context database."""
    global _ac_engine
    if _ac_engine is None:
        settings = get_settings()
        database_url = _get_agent_context_database_url()

        if database_url.startswith("sqlite"):
            from sqlalchemy.pool import StaticPool

            _ac_engine = create_async_engine(
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
                from sqlalchemy.pool import NullPool

                engine_kwargs["poolclass"] = NullPool

                if settings.rds_tls_verify:
                    ssl_ctx = ssl.create_default_context(cafile=RDS_CA_BUNDLE_PATH)
                else:
                    ssl_ctx = ssl.create_default_context()
                    ssl_ctx.check_hostname = False
                    ssl_ctx.verify_mode = ssl.CERT_NONE
                engine_kwargs["connect_args"] = {"ssl": ssl_ctx}
            else:
                engine_kwargs["pool_size"] = 10
                engine_kwargs["max_overflow"] = 5

            _ac_engine = create_async_engine(database_url, **engine_kwargs)

    return _ac_engine


def _reset_agent_context_engine() -> None:
    """Reset the agent_context engine (for token refresh)."""
    global _ac_engine, _ac_session_factory
    _ac_engine = None
    _ac_session_factory = None


def _get_agent_context_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the session factory for agent_context database."""
    global _ac_session_factory
    if _ac_session_factory is None:
        _ac_session_factory = async_sessionmaker(_get_agent_context_engine(), expire_on_commit=False)
    return _ac_session_factory


async def get_agent_context_db() -> AsyncSession:
    """FastAPI dependency that yields a session connected to agent_context DB.

    Used by knowledge routes and status-callback endpoints that need to
    read/write the knowledge_assets table (which lives in agent_context).
    """
    settings = get_settings()

    # For IAM auth, recreate engine periodically to pick up fresh tokens
    if settings.rds_iam_auth and settings.rds_host:
        _reset_agent_context_engine()

    factory = _get_agent_context_session_factory()
    async with factory() as session:
        yield session
