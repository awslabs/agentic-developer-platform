"""Alembic environment for agent_context database.

This is a SEPARATE migration environment from the gateway's Alembic.
It targets the `agent_context` database on the shared RDS instance,
using the `agent_context_svc` login.

Environment variables (AC_ prefix to avoid collision with gateway's BG_ prefix):
    AC_RDS_IAM_AUTH   - "true" to use IAM token auth (production)
    AC_RDS_HOST       - RDS instance hostname
    AC_RDS_PORT       - RDS port (default: 5432)
    AC_RDS_USERNAME   - DB username (default: agent_context_svc)
    AC_RDS_DBNAME     - Database name (default: agent_context)
    AC_AWS_REGION     - AWS region (default: us-east-1)
    AC_RDS_TLS_VERIFY - "false" to skip TLS verification (default: true)
    AC_DATABASE_URL   - Fallback URL for local dev (overrides everything)
"""

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No shared target_metadata — agent-context migrations use raw DDL (op.execute)
# to avoid coupling to a model layer that doesn't exist yet.
target_metadata = None


def _generate_iam_token(host: str, port: int, username: str, region: str) -> str:
    """Generate an IAM auth token for RDS connection."""
    import boto3

    client = boto3.client("rds", region_name=region)
    return client.generate_db_auth_token(
        DBHostname=host,
        Port=port,
        DBUsername=username,
        Region=region,
    )


def get_database_url() -> str:
    """Build the database URL for migrations.

    Priority:
    1. AC_DATABASE_URL (local dev override)
    2. IAM auth URL (when AC_RDS_IAM_AUTH=true and AC_RDS_HOST is set)
    3. alembic.ini sqlalchemy.url (SQLite fallback)
    """
    # Local dev override
    override = os.getenv("AC_DATABASE_URL")
    if override:
        return override

    # IAM auth for production
    rds_iam_auth = os.getenv("AC_RDS_IAM_AUTH", "false").lower() == "true"
    rds_host = os.getenv("AC_RDS_HOST", "")

    if rds_iam_auth and rds_host:
        rds_port = int(os.getenv("AC_RDS_PORT", "5432"))
        rds_username = os.getenv("AC_RDS_USERNAME", "agent_context_svc")
        rds_dbname = os.getenv("AC_RDS_DBNAME", "agent_context")
        region = os.getenv("AC_AWS_REGION", "us-east-1")

        token = _generate_iam_token(rds_host, rds_port, rds_username, region)
        # asyncpg driver for async migrations
        from urllib.parse import quote_plus

        return (
            f"postgresql+asyncpg://{rds_username}:{quote_plus(token)}"
            f"@{rds_host}:{rds_port}/{rds_dbname}"
        )

    # Fallback to alembic.ini
    return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (SQL script generation)."""
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    """Run migrations using the given connection."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations using async engine (PostgreSQL with asyncpg)."""
    url = get_database_url()

    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = url

    # SSL for IAM auth
    connect_args = {}
    rds_iam_auth = os.getenv("AC_RDS_IAM_AUTH", "false").lower() == "true"
    if rds_iam_auth:
        import ssl

        rds_ca_bundle_path = "/etc/ssl/certs/rds-global-bundle.pem"
        rds_tls_verify = os.getenv("AC_RDS_TLS_VERIFY", "true").lower() != "false"
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
    """Run migrations using sync engine (SQLite for local dev)."""
    url = get_database_url()
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        do_run_migrations(connection)
    connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Uses sync engine for SQLite, async for PostgreSQL.
    """
    url = get_database_url()
    if url and url.startswith("sqlite"):
        run_sync_migrations()
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
