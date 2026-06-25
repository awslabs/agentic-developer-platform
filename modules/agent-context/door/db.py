"""Database connection helpers for the Door (context-mcp).

Supports two modes:
  1. IAM auth (production): mints a fresh RDS IAM token per new connection.
     This is required because the agent_context RDS instance is IAM-auth-only
     (no static password). Tokens expire ~15 min so they MUST NOT be baked
     into a long-lived pool at startup.
  2. Static DSN (local/CI): uses DATABASE_URL or DB_PASSWORD directly.

Pattern reused from images/ingestion/db.py — same env vars, same boto3 call.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

log = logging.getLogger(__name__)


def _get_iam_auth_token(host: str, port: int, user: str, region: str) -> str:
    """Generate an RDS IAM authentication token.

    Same implementation as images/ingestion/db.py:_get_iam_auth_token.
    """
    import boto3

    client = boto3.client("rds", region_name=region)
    return client.generate_db_auth_token(
        DBHostname=host,
        Port=port,
        DBUsername=user,
        Region=region,
    )


class IAMConnectionPool:
    """A psycopg2-compatible connection pool that mints fresh IAM tokens.

    Unlike SimpleConnectionPool (which bakes credentials at construction),
    this pool generates a new IAM auth token each time a connection is
    created. Existing connections remain valid until they are closed/broken;
    only new connections need a fresh token.

    Implements the same interface as psycopg2.pool.SimpleConnectionPool:
      - getconn() -> connection
      - putconn(conn)
      - closeall()
    """

    def __init__(
        self,
        minconn: int,
        maxconn: int,
        host: str,
        port: int,
        dbname: str,
        user: str,
        region: str,
    ) -> None:
        self._host = host
        self._port = port
        self._dbname = dbname
        self._user = user
        self._region = region
        self._minconn = minconn
        self._maxconn = maxconn
        self._lock = threading.Lock()
        self._pool: list[Any] = []
        self._used: set[int] = set()  # id(conn) of checked-out connections

        # Pre-fill minconn connections
        for _ in range(minconn):
            conn = self._create_connection()
            self._pool.append(conn)

    def _create_connection(self) -> Any:
        """Create a new connection with a freshly-minted IAM token."""
        import psycopg2

        password = _get_iam_auth_token(self._host, self._port, self._user, self._region)
        conn = psycopg2.connect(
            host=self._host,
            port=self._port,
            dbname=self._dbname,
            user=self._user,
            password=password,
            sslmode="require",
            connect_timeout=10,
        )
        conn.autocommit = False
        return conn

    def getconn(self) -> Any:
        """Get a connection from the pool (or create one if under maxconn)."""
        with self._lock:
            # Try to reuse an existing idle connection
            while self._pool:
                conn = self._pool.pop(0)
                try:
                    # Verify the connection is still alive
                    conn.cursor().execute("SELECT 1")
                    conn.rollback()  # clean state
                    self._used.add(id(conn))
                    return conn
                except Exception:
                    # Connection is dead — discard and try next
                    try:
                        conn.close()
                    except Exception:
                        pass

            # No idle connection available — create a new one if under limit
            if len(self._used) < self._maxconn:
                conn = self._create_connection()
                self._used.add(id(conn))
                return conn

            raise RuntimeError(f"IAMConnectionPool exhausted (max={self._maxconn})")

    def putconn(self, conn: Any, close: bool = False) -> None:
        """Return a connection to the pool."""
        with self._lock:
            self._used.discard(id(conn))
            if close:
                try:
                    conn.close()
                except Exception:
                    pass
            else:
                try:
                    conn.rollback()  # reset transaction state
                    self._pool.append(conn)
                except Exception:
                    # Connection broken — discard
                    try:
                        conn.close()
                    except Exception:
                        pass

    def closeall(self) -> None:
        """Close all connections in the pool."""
        with self._lock:
            for conn in self._pool:
                try:
                    conn.close()
                except Exception:
                    pass
            self._pool.clear()
            self._used.clear()


def create_db_pool(config: Any) -> Any | None:
    """Create a database connection pool based on configuration.

    Returns:
        IAMConnectionPool when DB_USE_IAM_AUTH=true (production).
        psycopg2.pool.SimpleConnectionPool when using static DSN (local/CI).
        None if no database configuration is available.
    """
    use_iam = os.environ.get("DB_USE_IAM_AUTH", "false").lower() in (
        "true",
        "1",
        "yes",
    )
    host = os.environ.get("DB_HOST", "")

    if use_iam and host:
        port = int(os.environ.get("DB_PORT", "5432"))
        dbname = os.environ.get("DB_NAME", "agent_context")
        user = os.environ.get("DB_USER", "agent_context_rw")
        region = os.environ.get("AWS_REGION", "us-east-1")

        log.info(
            "Creating IAM-auth connection pool (host=%s, db=%s, user=%s)",
            host,
            dbname,
            user,
        )
        return IAMConnectionPool(
            minconn=1,
            maxconn=5,
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            region=region,
        )

    # Fallback: static DSN (local dev / CI)
    if config.database_url:
        import psycopg2.pool

        log.info("Creating static-DSN connection pool")
        return psycopg2.pool.SimpleConnectionPool(1, 5, config.database_url)

    log.warning("No database configuration available (DB_USE_IAM_AUTH=false, DATABASE_URL empty)")
    return None
