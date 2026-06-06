"""Alembic environment configuration with IAM database authentication support.

This module supports two database authentication modes:
1. IAM Authentication: For production RDS instances with IAM auth enabled
2. Standard Authentication: For local development (SQLite or password-based PostgreSQL)

The authentication mode is determined by the BG_RDS_IAM_AUTH environment variable.
"""

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from src.shared.models.base import Base
from src.shared.models.budget import BudgetConfig, BudgetUsage  # noqa: F401
from src.shared.models.organization import Department, Organization, ServiceAccount, Team, User  # noqa: F401
from src.shared.models.token import Token  # noqa: F401
from src.shared.models.usage import BedrockPoolAccount, ModelAlias, ModelPricing, RateLimitConfig, UsageLog  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_database_url_for_alembic() -> str:
    """Get the database URL for Alembic migrations.

    Supports IAM auth when BG_RDS_IAM_AUTH=true and RDS host is configured.
    Falls back to sqlalchemy.url from alembic.ini or DATABASE_URL env var.

    Returns:
        Database connection URL for migrations
    """
    # Check if IAM auth is enabled
    rds_iam_auth = os.getenv("BG_RDS_IAM_AUTH", "false").lower() == "true"
    rds_host = os.getenv("BG_RDS_HOST", "")

    if rds_iam_auth and rds_host:
        # Import here to avoid circular imports and allow mocking in tests
        from src.shared.database import construct_iam_database_url

        rds_port = int(os.getenv("BG_RDS_PORT", "5432"))
        rds_username = os.getenv("BG_RDS_USERNAME", "bgadmin")
        rds_dbname = os.getenv("BG_RDS_DBNAME", "bedrockgateway")
        aws_region = os.getenv("BG_AWS_REGION", "us-east-1")

        # Get URL with IAM token (keep asyncpg driver for async migrations)
        url = construct_iam_database_url(
            host=rds_host,
            port=rds_port,
            username=rds_username,
            dbname=rds_dbname,
            region=aws_region,
        )
        return url
    else:
        # Use standard DATABASE_URL or alembic.ini URL
        return os.getenv("BG_DATABASE_URL") or config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well. By skipping the Engine creation
    we don't even need a DBAPI to be available.
    """
    url = get_database_url_for_alembic()
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    """Run migrations using the given connection."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations using async engine (for PostgreSQL with asyncpg)."""
    url = get_database_url_for_alembic()

    # Create config dict with the URL
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = url

    # Build connect_args for SSL when using IAM auth
    connect_args = {}
    rds_iam_auth = os.getenv("BG_RDS_IAM_AUTH", "false").lower() == "true"
    if rds_iam_auth:
        import ssl

        rds_ca_bundle_path = "/etc/ssl/certs/rds-global-bundle.pem"
        rds_tls_verify = os.getenv("BG_RDS_TLS_VERIFY", "true").lower() != "false"
        if rds_tls_verify:
            ssl_ctx = ssl.create_default_context(cafile=rds_ca_bundle_path)
        else:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = ssl_ctx

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_sync_migrations() -> None:
    """Run migrations using sync engine (for SQLite and other sync dialects)."""
    url = get_database_url_for_alembic()
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        do_run_migrations(connection)
    connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Uses sync engine for SQLite, async for PostgreSQL.
    Supports IAM authentication when configured.
    """
    url = get_database_url_for_alembic()
    # Use sync engine for SQLite, async for PostgreSQL
    if url and url.startswith("sqlite"):
        run_sync_migrations()
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
