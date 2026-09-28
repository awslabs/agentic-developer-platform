"""Unit tests for door/db.py — IAM-auth-aware connection pool.

Tests cover:
- _get_iam_auth_token: calls boto3 correctly
- IAMConnectionPool: mints fresh token per new connection
- create_db_pool: dispatches IAM vs static DSN based on env vars
- Fallback behavior when DB_USE_IAM_AUTH=false

Uses mocks for psycopg2 and boto3 (no live DB or AWS required).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _get_iam_auth_token
# ---------------------------------------------------------------------------


class TestGetIAMAuthToken:
    """Verify the IAM token generation calls boto3 correctly."""

    def test_calls_generate_db_auth_token(self):
        """Should call rds.generate_db_auth_token with correct params."""
        mock_client = MagicMock()
        mock_client.generate_db_auth_token.return_value = "iam-token-abc123"

        with patch("boto3.client", return_value=mock_client) as mock_boto:
            from door.db import _get_iam_auth_token

            token = _get_iam_auth_token(
                host="mydb.cluster-xyz.us-east-1.rds.amazonaws.com",
                port=5432,
                user="agent_context_rw",
                region="us-east-1",
            )

        mock_boto.assert_called_once_with("rds", region_name="us-east-1")
        mock_client.generate_db_auth_token.assert_called_once_with(
            DBHostname="mydb.cluster-xyz.us-east-1.rds.amazonaws.com",
            Port=5432,
            DBUsername="agent_context_rw",
            Region="us-east-1",
        )
        assert token == "iam-token-abc123"


# ---------------------------------------------------------------------------
# IAMConnectionPool
# ---------------------------------------------------------------------------


class TestIAMConnectionPool:
    """Verify the IAM pool mints fresh tokens per connection."""

    @patch("door.db._get_iam_auth_token", return_value="fresh-token-1")
    @patch("psycopg2.connect")
    def test_creates_connection_with_iam_token(self, mock_connect, mock_token):
        """Pool._create_connection should mint a fresh IAM token."""
        from door.db import IAMConnectionPool

        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        pool = IAMConnectionPool(
            minconn=0,
            maxconn=5,
            host="mydb.rds.amazonaws.com",
            port=5432,
            dbname="agent_context",
            user="agent_context_rw",
            region="us-east-1",
        )
        conn = pool.getconn()

        mock_token.assert_called_with(
            "mydb.rds.amazonaws.com", 5432, "agent_context_rw", "us-east-1"
        )
        mock_connect.assert_called_with(
            host="mydb.rds.amazonaws.com",
            port=5432,
            dbname="agent_context",
            user="agent_context_rw",
            password="fresh-token-1",
            sslmode="require",
            connect_timeout=10,
        )
        assert conn is mock_conn

    @patch("door.db._get_iam_auth_token")
    @patch("psycopg2.connect")
    def test_fresh_token_per_new_connection(self, mock_connect, mock_token):
        """Each new connection should get a freshly-minted token."""
        from door.db import IAMConnectionPool

        # Return different tokens on successive calls
        mock_token.side_effect = ["token-1", "token-2", "token-3"]

        mock_conn1 = MagicMock()
        mock_conn2 = MagicMock()
        mock_conn3 = MagicMock()
        mock_connect.side_effect = [mock_conn1, mock_conn2, mock_conn3]

        pool = IAMConnectionPool(
            minconn=0,
            maxconn=5,
            host="mydb.rds.amazonaws.com",
            port=5432,
            dbname="agent_context",
            user="agent_context_rw",
            region="us-east-1",
        )

        # Get three connections — each should mint a new token
        c1 = pool.getconn()
        c2 = pool.getconn()
        c3 = pool.getconn()

        assert mock_token.call_count == 3
        assert c1 is mock_conn1
        assert c2 is mock_conn2
        assert c3 is mock_conn3

    @patch("door.db._get_iam_auth_token", return_value="token-1")
    @patch("psycopg2.connect")
    def test_putconn_returns_connection_to_pool(self, mock_connect, mock_token):
        """Returned connections should be reusable without minting a new token."""
        from door.db import IAMConnectionPool

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        pool = IAMConnectionPool(
            minconn=0,
            maxconn=5,
            host="mydb.rds.amazonaws.com",
            port=5432,
            dbname="agent_context",
            user="agent_context_rw",
            region="us-east-1",
        )

        conn = pool.getconn()
        pool.putconn(conn)

        # Re-get should reuse the idle connection (no new token minted)
        mock_token.reset_mock()
        conn2 = pool.getconn()

        # The pool checks liveness with SELECT 1 — no new token needed
        mock_token.assert_not_called()
        assert conn2 is mock_conn

    @patch("door.db._get_iam_auth_token")
    @patch("psycopg2.connect")
    def test_dead_connection_triggers_fresh_token(self, mock_connect, mock_token):
        """If a pooled connection is dead, a fresh token is minted for the replacement."""
        from door.db import IAMConnectionPool

        mock_token.side_effect = ["token-1", "token-2"]

        # First connection works, then dies when checked
        mock_conn_dead = MagicMock()
        mock_cursor_dead = MagicMock()
        mock_cursor_dead.execute.side_effect = Exception("connection reset")
        mock_conn_dead.cursor.return_value = mock_cursor_dead

        mock_conn_new = MagicMock()
        mock_connect.side_effect = [mock_conn_dead, mock_conn_new]

        pool = IAMConnectionPool(
            minconn=0,
            maxconn=5,
            host="mydb.rds.amazonaws.com",
            port=5432,
            dbname="agent_context",
            user="agent_context_rw",
            region="us-east-1",
        )

        # Get first connection, then return it
        conn1 = pool.getconn()  # token-1
        pool.putconn(conn1)

        # Next getconn should find dead conn, discard it, and create fresh one
        conn2 = pool.getconn()  # token-2 (fresh mint)
        assert conn2 is mock_conn_new
        assert mock_token.call_count == 2

    @patch("door.db._get_iam_auth_token", return_value="token-1")
    @patch("psycopg2.connect")
    def test_pool_exhausted_raises(self, mock_connect, mock_token):
        """Should raise RuntimeError when maxconn is reached."""
        from door.db import IAMConnectionPool

        # Each call must return a distinct object so id() differs
        mock_connect.side_effect = [MagicMock(), MagicMock(), MagicMock()]

        pool = IAMConnectionPool(
            minconn=0,
            maxconn=2,
            host="mydb.rds.amazonaws.com",
            port=5432,
            dbname="agent_context",
            user="agent_context_rw",
            region="us-east-1",
        )

        pool.getconn()
        pool.getconn()

        with pytest.raises(RuntimeError, match="exhausted"):
            pool.getconn()

    @patch("door.db._get_iam_auth_token", return_value="token-1")
    @patch("psycopg2.connect")
    def test_closeall_closes_idle_connections(self, mock_connect, mock_token):
        """closeall() should close all pooled connections."""
        from door.db import IAMConnectionPool

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        pool = IAMConnectionPool(
            minconn=1,
            maxconn=5,
            host="mydb.rds.amazonaws.com",
            port=5432,
            dbname="agent_context",
            user="agent_context_rw",
            region="us-east-1",
        )

        pool.closeall()
        mock_conn.close.assert_called()

    @patch("door.db._get_iam_auth_token", return_value="token-1")
    @patch("psycopg2.connect")
    def test_minconn_prefill(self, mock_connect, mock_token):
        """Pool should pre-create minconn connections at init."""
        from door.db import IAMConnectionPool

        mock_connect.return_value = MagicMock()

        IAMConnectionPool(
            minconn=3,
            maxconn=5,
            host="mydb.rds.amazonaws.com",
            port=5432,
            dbname="agent_context",
            user="agent_context_rw",
            region="us-east-1",
        )

        # 3 connections pre-created
        assert mock_connect.call_count == 3
        assert mock_token.call_count == 3


# ---------------------------------------------------------------------------
# create_db_pool
# ---------------------------------------------------------------------------


class TestCreateDBPool:
    """Verify factory dispatches correctly based on environment."""

    @patch("door.db._get_iam_auth_token", return_value="iam-token")
    @patch("psycopg2.connect")
    def test_iam_mode_returns_iam_pool(self, mock_connect, mock_token, monkeypatch):
        """DB_USE_IAM_AUTH=true + DB_HOST set -> IAMConnectionPool."""
        monkeypatch.setenv("DB_USE_IAM_AUTH", "true")
        monkeypatch.setenv("DB_HOST", "mydb.rds.amazonaws.com")
        monkeypatch.setenv("DB_PORT", "5432")
        monkeypatch.setenv("DB_NAME", "agent_context")
        monkeypatch.setenv("DB_USER", "agent_context_rw")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_connect.return_value = MagicMock()

        from door.db import IAMConnectionPool, create_db_pool

        mock_config = MagicMock()
        mock_config.database_url = ""

        pool = create_db_pool(mock_config)
        assert isinstance(pool, IAMConnectionPool)

    def test_static_dsn_fallback(self, monkeypatch):
        """DB_USE_IAM_AUTH=false + DATABASE_URL set -> SimpleConnectionPool."""
        monkeypatch.setenv("DB_USE_IAM_AUTH", "false")
        monkeypatch.delenv("DB_HOST", raising=False)

        mock_config = MagicMock()
        mock_config.database_url = "postgresql://user:pass@localhost:5432/testdb"

        with patch("psycopg2.pool.SimpleConnectionPool") as mock_pool_cls:
            mock_pool_cls.return_value = MagicMock()

            from door.db import create_db_pool

            pool = create_db_pool(mock_config)

            mock_pool_cls.assert_called_once_with(
                1, 5, "postgresql://user:pass@localhost:5432/testdb"
            )
            assert pool is mock_pool_cls.return_value

    def test_no_config_returns_none(self, monkeypatch):
        """No IAM and no DATABASE_URL -> None."""
        monkeypatch.setenv("DB_USE_IAM_AUTH", "false")
        monkeypatch.delenv("DB_HOST", raising=False)

        mock_config = MagicMock()
        mock_config.database_url = ""

        from door.db import create_db_pool

        pool = create_db_pool(mock_config)
        assert pool is None

    @patch("door.db._get_iam_auth_token", return_value="iam-token")
    @patch("psycopg2.connect")
    def test_iam_takes_precedence_over_database_url(self, mock_connect, mock_token, monkeypatch):
        """When DB_USE_IAM_AUTH=true AND DATABASE_URL set, IAM wins."""
        monkeypatch.setenv("DB_USE_IAM_AUTH", "true")
        monkeypatch.setenv("DB_HOST", "mydb.rds.amazonaws.com")
        monkeypatch.setenv("DB_PORT", "5432")
        monkeypatch.setenv("DB_NAME", "agent_context")
        monkeypatch.setenv("DB_USER", "agent_context_rw")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        mock_connect.return_value = MagicMock()

        mock_config = MagicMock()
        mock_config.database_url = "postgresql://user:pass@localhost:5432/testdb"

        from door.db import IAMConnectionPool, create_db_pool

        pool = create_db_pool(mock_config)
        assert isinstance(pool, IAMConnectionPool)

    @patch("door.db._get_iam_auth_token", return_value="iam-token")
    @patch("psycopg2.connect")
    def test_iam_without_host_falls_back_to_dsn(self, mock_connect, mock_token, monkeypatch):
        """DB_USE_IAM_AUTH=true but DB_HOST empty -> fallback to DATABASE_URL."""
        monkeypatch.setenv("DB_USE_IAM_AUTH", "true")
        monkeypatch.setenv("DB_HOST", "")

        mock_config = MagicMock()
        mock_config.database_url = "postgresql://user:pass@localhost:5432/testdb"

        with patch("psycopg2.pool.SimpleConnectionPool") as mock_pool_cls:
            mock_pool_cls.return_value = MagicMock()

            from door.db import create_db_pool

            pool = create_db_pool(mock_config)
            mock_pool_cls.assert_called_once()
            assert pool is mock_pool_cls.return_value
