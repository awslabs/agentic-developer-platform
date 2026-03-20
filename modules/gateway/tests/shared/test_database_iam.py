"""Tests for RDS IAM database authentication functionality.

These tests verify:
1. IAM auth token generation with mocked boto3
2. Database URL construction with IAM tokens
3. Fallback to standard DATABASE_URL when IAM auth is disabled
4. SQLite mode continues to work (no IAM auth applied)
"""

import os
from unittest.mock import MagicMock, patch

import pytest


class TestGetRdsAuthToken:
    """Tests for get_rds_auth_token function."""

    def test_get_rds_auth_token_calls_boto3(self):
        """Test that get_rds_auth_token calls boto3 generate_db_auth_token."""
        from src.shared.database import get_rds_auth_token

        mock_client = MagicMock()
        mock_client.generate_db_auth_token.return_value = "mock-iam-token-12345"

        with patch("boto3.client", return_value=mock_client) as mock_boto3:
            token = get_rds_auth_token(
                host="test-db.abc123.us-east-1.rds.amazonaws.com",
                port=5432,
                username="bgadmin",
                region="us-east-1",
            )

            # Verify boto3.client was called with correct parameters
            mock_boto3.assert_called_once_with("rds", region_name="us-east-1")

            # Verify generate_db_auth_token was called with correct parameters
            mock_client.generate_db_auth_token.assert_called_once_with(
                DBHostname="test-db.abc123.us-east-1.rds.amazonaws.com",
                Port=5432,
                DBUsername="bgadmin",
                Region="us-east-1",
            )

            # Verify token is returned
            assert token == "mock-iam-token-12345"

    def test_get_rds_auth_token_with_different_region(self):
        """Test token generation with a different AWS region."""
        from src.shared.database import get_rds_auth_token

        mock_client = MagicMock()
        mock_client.generate_db_auth_token.return_value = "eu-west-token"

        with patch("boto3.client", return_value=mock_client) as mock_boto3:
            token = get_rds_auth_token(
                host="test-db.xyz789.eu-west-1.rds.amazonaws.com",
                port=5432,
                username="dbuser",
                region="eu-west-1",
            )

            mock_boto3.assert_called_once_with("rds", region_name="eu-west-1")
            assert token == "eu-west-token"


class TestConstructIamDatabaseUrl:
    """Tests for construct_iam_database_url function."""

    def test_construct_iam_database_url_format(self):
        """Test that the constructed URL has correct format."""
        from src.shared.database import construct_iam_database_url

        mock_client = MagicMock()
        # Token with special characters that need URL encoding
        mock_client.generate_db_auth_token.return_value = "token+with/special=chars"

        with patch("boto3.client", return_value=mock_client):
            url = construct_iam_database_url(
                host="mydb.us-east-1.rds.amazonaws.com",
                port=5432,
                username="bgadmin",
                dbname="bedrockgateway",
                region="us-east-1",
            )

            # Verify URL format
            assert url.startswith("postgresql+asyncpg://bgadmin:")
            assert "@mydb.us-east-1.rds.amazonaws.com:5432/bedrockgateway" in url
            # Note: asyncpg does not support ?ssl=require in URL.
            # SSL is enforced via connect_args in get_engine().
            # Verify URL encoding of special characters
            assert "token%2Bwith%2Fspecial%3Dchars" in url

    def test_construct_iam_database_url_uses_asyncpg(self):
        """Test that URL uses asyncpg dialect."""
        from src.shared.database import construct_iam_database_url

        mock_client = MagicMock()
        mock_client.generate_db_auth_token.return_value = "simple-token"

        with patch("boto3.client", return_value=mock_client):
            url = construct_iam_database_url(
                host="test.rds.amazonaws.com",
                port=5432,
                username="user",
                dbname="testdb",
                region="us-east-1",
            )

            # SSL is enforced via connect_args in get_engine(), not in URL
            # asyncpg doesn't support ?ssl=require as a query parameter
            assert "postgresql+asyncpg://" in url


class TestGetDatabaseUrl:
    """Tests for get_database_url function."""

    def test_get_database_url_with_iam_auth_enabled(self):
        """Test that IAM auth URL is returned when enabled."""
        from src.shared.database import get_database_url

        mock_client = MagicMock()
        mock_client.generate_db_auth_token.return_value = "iam-token"

        mock_settings = MagicMock()
        mock_settings.rds_iam_auth = True
        mock_settings.rds_host = "prod-db.rds.amazonaws.com"
        mock_settings.rds_port = 5432
        mock_settings.rds_username = "bgadmin"
        mock_settings.rds_dbname = "bedrockgateway"
        mock_settings.aws_region = "us-east-1"

        with patch("boto3.client", return_value=mock_client):
            with patch("src.shared.database.get_settings", return_value=mock_settings):
                url = get_database_url()

                assert "postgresql+asyncpg" in url
                assert "prod-db.rds.amazonaws.com" in url
                # Note: SSL is enforced via connect_args in get_engine(), not in URL.
                # asyncpg doesn't support ?ssl=require as a query parameter.

    def test_get_database_url_with_iam_auth_disabled(self):
        """Test that standard DATABASE_URL is returned when IAM auth disabled."""
        from src.shared.database import get_database_url

        mock_settings = MagicMock()
        mock_settings.rds_iam_auth = False
        mock_settings.rds_host = ""
        mock_settings.database_url = "postgresql+asyncpg://user:pass@localhost:5432/testdb"

        with patch("src.shared.database.get_settings", return_value=mock_settings):
            url = get_database_url()

            assert url == "postgresql+asyncpg://user:pass@localhost:5432/testdb"

    def test_get_database_url_fallback_when_no_rds_host(self):
        """Test fallback to DATABASE_URL when rds_host is empty."""
        from src.shared.database import get_database_url

        mock_settings = MagicMock()
        mock_settings.rds_iam_auth = True  # IAM auth enabled but no host
        mock_settings.rds_host = ""  # Empty host
        mock_settings.database_url = "sqlite+aiosqlite:///./test.db"

        with patch("src.shared.database.get_settings", return_value=mock_settings):
            url = get_database_url()

            # Should fall back to DATABASE_URL
            assert url == "sqlite+aiosqlite:///./test.db"


class TestIsSqliteUrl:
    """Tests for _is_sqlite_url function."""

    def test_sqlite_url_detection(self):
        """Test SQLite URL detection."""
        from src.shared.database import _is_sqlite_url

        assert _is_sqlite_url("sqlite:///test.db") is True
        assert _is_sqlite_url("sqlite+aiosqlite:///test.db") is True
        assert _is_sqlite_url("postgresql://localhost/db") is False
        assert _is_sqlite_url("postgresql+asyncpg://localhost/db") is False


class TestGetEngine:
    """Tests for get_engine function."""

    def test_get_engine_sqlite_config(self):
        """Test that SQLite engine has appropriate configuration."""
        from sqlalchemy.pool import StaticPool

        from src.shared.database import get_engine, reset_engine

        # Reset any existing engine
        reset_engine()

        mock_settings = MagicMock()
        mock_settings.rds_iam_auth = False
        mock_settings.rds_host = ""
        mock_settings.database_url = "sqlite+aiosqlite:///./test.db"

        with patch("src.shared.database.get_settings", return_value=mock_settings):
            engine = get_engine()

            # SQLite should use StaticPool (single connection)
            assert isinstance(engine.pool, StaticPool)

        # Clean up
        reset_engine()

    def test_get_engine_caches_result(self):
        """Test that get_engine returns the same engine on subsequent calls."""
        from src.shared.database import get_engine, reset_engine

        reset_engine()

        mock_settings = MagicMock()
        mock_settings.rds_iam_auth = False
        mock_settings.rds_host = ""
        mock_settings.database_url = "sqlite+aiosqlite:///./test.db"

        with patch("src.shared.database.get_settings", return_value=mock_settings):
            engine1 = get_engine()
            engine2 = get_engine()

            assert engine1 is engine2

        reset_engine()


class TestResetEngine:
    """Tests for reset_engine function."""

    def test_reset_engine_clears_cached_engine(self):
        """Test that reset_engine clears the cached engine."""
        from src.shared import database
        from src.shared.database import get_engine, reset_engine

        mock_settings = MagicMock()
        mock_settings.rds_iam_auth = False
        mock_settings.rds_host = ""
        mock_settings.database_url = "sqlite+aiosqlite:///./test.db"

        with patch("src.shared.database.get_settings", return_value=mock_settings):
            # Create initial engine
            engine1 = get_engine()
            assert database._engine is not None

            # Reset
            reset_engine()
            assert database._engine is None
            assert database._session_factory is None

            # Get new engine
            engine2 = get_engine()

            # Should be different engine instances
            assert engine1 is not engine2

        reset_engine()


class TestSqliteFallback:
    """Tests to ensure SQLite mode works correctly (local dev/testing)."""

    @pytest.fixture(autouse=True)
    def reset_engine_before_each(self):
        """Reset engine before each test."""
        from src.shared.database import reset_engine

        reset_engine()
        yield
        reset_engine()

    def test_sqlite_url_used_when_iam_auth_disabled(self):
        """Test that SQLite URL is used when IAM auth is disabled."""
        from src.shared.database import get_database_url

        mock_settings = MagicMock()
        mock_settings.rds_iam_auth = False
        mock_settings.rds_host = ""
        mock_settings.database_url = "sqlite+aiosqlite:///./local.db"

        with patch("src.shared.database.get_settings", return_value=mock_settings):
            url = get_database_url()
            assert url == "sqlite+aiosqlite:///./local.db"

    def test_sqlite_engine_creation(self):
        """Test SQLite engine can be created successfully."""
        from src.shared.database import get_engine

        mock_settings = MagicMock()
        mock_settings.rds_iam_auth = False
        mock_settings.rds_host = ""
        mock_settings.database_url = "sqlite+aiosqlite:///:memory:"

        with patch("src.shared.database.get_settings", return_value=mock_settings):
            engine = get_engine()
            assert engine is not None
            assert "sqlite" in str(engine.url)


class TestEnvironmentVariableIntegration:
    """Tests for environment variable handling."""

    @pytest.fixture(autouse=True)
    def clean_env(self):
        """Clean environment variables before and after tests."""
        env_vars = ["BG_RDS_IAM_AUTH", "BG_RDS_HOST", "BG_DATABASE_URL"]
        original = {k: os.environ.get(k) for k in env_vars}

        # Clean before test
        for k in env_vars:
            os.environ.pop(k, None)

        yield

        # Restore after test
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_default_settings_use_sqlite_fallback(self):
        """Test that default settings allow SQLite for local dev."""
        # Clear any cached settings

        from src.shared.config import Settings

        # Create fresh settings
        settings = Settings()

        # By default, IAM auth should be disabled
        assert settings.rds_iam_auth is False
        assert settings.rds_host == ""
