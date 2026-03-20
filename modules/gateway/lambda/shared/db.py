"""
RDS Database Connection Helper for Lambda Functions.

Provides IAM-authenticated PostgreSQL connections for Lambda functions.
This module is shared between the usage-tracker and pricing-refresh Lambdas.

Issue #234: Budget Usage Tracking Lambda
"""

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager

import boto3
import psycopg2
from psycopg2.extensions import connection as PostgresConnection  # noqa: N812

logger = logging.getLogger(__name__)

# Environment variables
DB_HOST = os.environ.get("DB_HOST", "")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "bedrockgateway")
DB_USERNAME = os.environ.get("DB_USERNAME", "bgadmin")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


def get_rds_auth_token(host: str, port: int, username: str, region: str) -> str:
    """
    Generate an IAM authentication token for RDS.

    Args:
        host: RDS hostname
        port: RDS port
        username: Database username
        region: AWS region

    Returns:
        IAM auth token string
    """
    client = boto3.client("rds", region_name=region)
    token = client.generate_db_auth_token(
        DBHostname=host,
        Port=port,
        DBUsername=username,
        Region=region,
    )
    logger.info("Generated IAM auth token for RDS")
    return token


def get_connection(
    host: str | None = None,
    port: int | None = None,
    dbname: str | None = None,
    username: str | None = None,
    region: str | None = None,
    use_iam_auth: bool = True,
) -> PostgresConnection:
    """
    Create a PostgreSQL connection with IAM authentication.

    Args:
        host: RDS hostname (defaults to DB_HOST env var)
        port: RDS port (defaults to DB_PORT env var)
        dbname: Database name (defaults to DB_NAME env var)
        username: Database username (defaults to DB_USERNAME env var)
        region: AWS region (defaults to AWS_REGION env var)
        use_iam_auth: Whether to use IAM auth (defaults to True)

    Returns:
        psycopg2 connection object
    """
    host = host or DB_HOST
    port = port or DB_PORT
    dbname = dbname or DB_NAME
    username = username or DB_USERNAME
    region = region or AWS_REGION

    if not host:
        raise ValueError("DB_HOST environment variable is required")

    if use_iam_auth:
        password = get_rds_auth_token(host, port, username, region)
    else:
        password = os.environ.get("DB_PASSWORD", "")

    # Connect with SSL required for RDS
    conn = psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=username,
        password=password,
        sslmode="require",
    )

    logger.info(f"Connected to RDS at {host}:{port}/{dbname}")
    return conn


@contextmanager
def get_db_connection(**kwargs) -> Generator[PostgresConnection, None, None]:
    """
    Context manager for database connections.

    Ensures connections are properly closed after use.

    Example:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    """
    conn = get_connection(**kwargs)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        logger.info("Database connection closed")
