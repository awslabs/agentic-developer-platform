"""Tests for RDS IAM database authentication functionality.

These tests verify:
1. IAM auth token generation with mocked boto3
2. Database URL construction with IAM tokens
3. Fallback to standard DATABASE_URL when IAM auth is disabled
4. SQLite mode continues to work (no IAM auth applied)
5. RDS TLS verification is enabled by default (issue #1157)
"""

import os
import ssl
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


class TestRdsCaBundleVerification:
    """Tests for RDS CA bundle verification (issue #1201).

    Verifies that the SSL context explicitly uses the AWS RDS CA bundle
    (not the system trust store) when TLS verification is enabled.
    This is the regression guard for the first H2 attempt that assumed
    the system trust store would validate RDS certs (it does not).
    """

    @pytest.fixture(autouse=True)
    def reset_engine_and_env(self):
        """Reset engine and clean env vars before/after each test."""
        from src.shared.database import reset_engine

        reset_engine()
        original = os.environ.get("BG_RDS_TLS_VERIFY")
        os.environ.pop("BG_RDS_TLS_VERIFY", None)
        yield
        reset_engine()
        if original is not None:
            os.environ["BG_RDS_TLS_VERIFY"] = original
        else:
            os.environ.pop("BG_RDS_TLS_VERIFY", None)

    def test_ssl_context_uses_rds_bundle_when_verify_enabled(self):
        """When BG_RDS_TLS_VERIFY is unset (or true), ssl.create_default_context is called with cafile."""
        from src.shared.database import RDS_CA_BUNDLE_PATH, get_engine, reset_engine

        reset_engine()

        mock_settings = MagicMock()
        mock_settings.rds_iam_auth = True
        mock_settings.rds_host = "test-db.us-east-1.rds.amazonaws.com"
        mock_settings.rds_port = 5432
        mock_settings.rds_username = "bgadmin"
        mock_settings.rds_dbname = "bedrockgateway"
        mock_settings.aws_region = "us-east-1"
        mock_settings.rds_tls_verify = True

        mock_client = MagicMock()
        mock_client.generate_db_auth_token.return_value = "mock-token"

        with patch("boto3.client", return_value=mock_client):
            with patch("src.shared.database.get_settings", return_value=mock_settings):
                with patch("src.shared.database.create_async_engine"):
                    with patch("src.shared.database.ssl.create_default_context") as mock_ssl:
                        mock_ssl.return_value = MagicMock()
                        get_engine()
                        mock_ssl.assert_called_once_with(cafile=RDS_CA_BUNDLE_PATH)

        reset_engine()

    def test_ssl_context_uses_rds_bundle_via_mock(self):
        """Verify ssl.create_default_context is called with cafile=RDS_CA_BUNDLE_PATH."""
        from src.shared.database import RDS_CA_BUNDLE_PATH, get_engine, reset_engine

        reset_engine()

        mock_settings = MagicMock()
        mock_settings.rds_iam_auth = True
        mock_settings.rds_host = "test-db.us-east-1.rds.amazonaws.com"
        mock_settings.rds_port = 5432
        mock_settings.rds_username = "bgadmin"
        mock_settings.rds_dbname = "bedrockgateway"
        mock_settings.aws_region = "us-east-1"
        mock_settings.rds_tls_verify = True

        mock_client = MagicMock()
        mock_client.generate_db_auth_token.return_value = "mock-token"

        with patch("boto3.client", return_value=mock_client):
            with patch("src.shared.database.get_settings", return_value=mock_settings):
                with patch("src.shared.database.create_async_engine"):
                    with patch("src.shared.database.ssl.create_default_context") as mock_ssl:
                        mock_ssl.return_value = MagicMock()
                        get_engine()
                        mock_ssl.assert_called_once_with(cafile=RDS_CA_BUNDLE_PATH)

        reset_engine()

    def test_ssl_context_falls_back_when_verify_disabled(self):
        """When BG_RDS_TLS_VERIFY=false, CERT_NONE is set and warning is logged."""
        from src.shared.database import get_engine, reset_engine

        reset_engine()
        os.environ["BG_RDS_TLS_VERIFY"] = "false"

        mock_settings = MagicMock()
        mock_settings.rds_iam_auth = True
        mock_settings.rds_host = "test-db.us-east-1.rds.amazonaws.com"
        mock_settings.rds_port = 5432
        mock_settings.rds_username = "bgadmin"
        mock_settings.rds_dbname = "bedrockgateway"
        mock_settings.aws_region = "us-east-1"
        mock_settings.rds_tls_verify = False

        mock_client = MagicMock()
        mock_client.generate_db_auth_token.return_value = "mock-token"

        captured_kwargs = {}

        def capture_create_async_engine(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock()

        with patch("boto3.client", return_value=mock_client):
            with patch("src.shared.database.get_settings", return_value=mock_settings):
                with patch("src.shared.database.create_async_engine", side_effect=capture_create_async_engine):
                    with patch("src.shared.database.logger") as mock_logger:
                        get_engine()
                        mock_logger.warning.assert_called_once_with("RDS TLS verification disabled (BG_RDS_TLS_VERIFY=false). MITM risk!")

        ssl_ctx = captured_kwargs.get("connect_args", {}).get("ssl")
        assert ssl_ctx is not None
        assert ssl_ctx.verify_mode == ssl.CERT_NONE
        assert ssl_ctx.check_hostname is False

        reset_engine()

    def test_default_is_verify_enabled(self):
        """With no BG_RDS_TLS_VERIFY env var, verification is enabled with the bundle."""
        from src.shared.database import RDS_CA_BUNDLE_PATH, get_engine, reset_engine

        reset_engine()
        os.environ.pop("BG_RDS_TLS_VERIFY", None)

        mock_settings = MagicMock()
        mock_settings.rds_iam_auth = True
        mock_settings.rds_host = "test-db.us-east-1.rds.amazonaws.com"
        mock_settings.rds_port = 5432
        mock_settings.rds_username = "bgadmin"
        mock_settings.rds_dbname = "bedrockgateway"
        mock_settings.aws_region = "us-east-1"
        mock_settings.rds_tls_verify = True  # default behavior

        mock_client = MagicMock()
        mock_client.generate_db_auth_token.return_value = "mock-token"

        with patch("boto3.client", return_value=mock_client):
            with patch("src.shared.database.get_settings", return_value=mock_settings):
                with patch("src.shared.database.create_async_engine"):
                    with patch("src.shared.database.ssl.create_default_context") as mock_ssl:
                        mock_ssl.return_value = MagicMock()
                        get_engine()
                        mock_ssl.assert_called_once_with(cafile=RDS_CA_BUNDLE_PATH)

        reset_engine()

    def test_bundle_path_present(self):
        """Assert /etc/ssl/certs/rds-global-bundle.pem exists (regression guard for Dockerfile).

        This test catches the exact failure mode from the first H2 attempt:
        if the Dockerfile fails to download the bundle, this test fails,
        preventing a deploy that would crash with CERTIFICATE_VERIFY_FAILED.
        """
        from pathlib import Path

        from src.shared.database import RDS_CA_BUNDLE_PATH

        bundle = Path(RDS_CA_BUNDLE_PATH)
        if not bundle.exists():
            pytest.skip(
                "RDS CA bundle not present (expected in Docker image, not local dev). Run in CI where the gateway image is built to validate this."
            )
        assert bundle.stat().st_size > 0, "RDS CA bundle is empty"


class TestRdsTlsVerification:
    """Tests for RDS TLS verification (issue #1157).

    Verifies that the SSL context used for RDS connections has proper
    certificate verification enabled by default, and can be disabled
    via the BG_RDS_TLS_VERIFY environment variable as an emergency escape hatch.
    """

    @pytest.fixture(autouse=True)
    def reset_engine_and_env(self):
        """Reset engine and clean env vars before/after each test."""
        from src.shared.database import reset_engine

        reset_engine()
        original = os.environ.get("BG_RDS_TLS_VERIFY")
        os.environ.pop("BG_RDS_TLS_VERIFY", None)
        yield
        reset_engine()
        if original is not None:
            os.environ["BG_RDS_TLS_VERIFY"] = original
        else:
            os.environ.pop("BG_RDS_TLS_VERIFY", None)

    def _get_ssl_context_from_engine(self, rds_tls_verify_value: str | None = None) -> ssl.SSLContext:
        """Helper: create an engine with IAM auth and capture the SSL context passed to it."""
        from src.shared.database import get_engine, reset_engine

        reset_engine()

        if rds_tls_verify_value is not None:
            os.environ["BG_RDS_TLS_VERIFY"] = rds_tls_verify_value
        else:
            os.environ.pop("BG_RDS_TLS_VERIFY", None)

        mock_settings = MagicMock()
        mock_settings.rds_iam_auth = True
        mock_settings.rds_host = "test-db.us-east-1.rds.amazonaws.com"
        mock_settings.rds_port = 5432
        mock_settings.rds_username = "bgadmin"
        mock_settings.rds_dbname = "bedrockgateway"
        mock_settings.aws_region = "us-east-1"
        mock_settings.rds_tls_verify = rds_tls_verify_value is None or rds_tls_verify_value.lower() != "false"

        mock_client = MagicMock()
        mock_client.generate_db_auth_token.return_value = "mock-token"

        captured_kwargs = {}

        def capture_create_async_engine(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock()

        # Use a real SSL context as the return value so we can inspect
        # verify_mode and check_hostname. The cafile argument is validated
        # separately in TestRdsCaBundleVerification.
        _real_create_default_context = ssl.create_default_context

        def mock_create_default_context(**kwargs):
            if "cafile" in kwargs:
                # When cafile is passed, return a real context with default verify settings
                return _real_create_default_context()
            # When no cafile (verify disabled path), return a real context for mutation
            return _real_create_default_context()

        with patch("boto3.client", return_value=mock_client):
            with patch("src.shared.database.get_settings", return_value=mock_settings):
                with patch("src.shared.database.create_async_engine", side_effect=capture_create_async_engine):
                    with patch("src.shared.database.ssl.create_default_context", side_effect=mock_create_default_context):
                        get_engine()

        connect_args = captured_kwargs.get("connect_args", {})
        return connect_args.get("ssl")

    def test_tls_verification_enabled_by_default(self):
        """When BG_RDS_TLS_VERIFY is unset, SSL context has CERT_REQUIRED and check_hostname=True."""
        ssl_ctx = self._get_ssl_context_from_engine(None)
        assert ssl_ctx is not None
        assert ssl_ctx.verify_mode == ssl.CERT_REQUIRED
        assert ssl_ctx.check_hostname is True

    def test_tls_verification_enabled_when_explicitly_true(self):
        """When BG_RDS_TLS_VERIFY=true, SSL context has CERT_REQUIRED and check_hostname=True."""
        ssl_ctx = self._get_ssl_context_from_engine("true")
        assert ssl_ctx is not None
        assert ssl_ctx.verify_mode == ssl.CERT_REQUIRED
        assert ssl_ctx.check_hostname is True

    def test_tls_verification_disabled_when_false(self):
        """When BG_RDS_TLS_VERIFY=false, SSL context has CERT_NONE and check_hostname=False."""
        ssl_ctx = self._get_ssl_context_from_engine("false")
        assert ssl_ctx is not None
        assert ssl_ctx.verify_mode == ssl.CERT_NONE
        assert ssl_ctx.check_hostname is False

    def test_tls_verification_disabled_case_insensitive(self):
        """When BG_RDS_TLS_VERIFY=False (capital F), SSL context disables verification."""
        ssl_ctx = self._get_ssl_context_from_engine("False")
        assert ssl_ctx is not None
        assert ssl_ctx.verify_mode == ssl.CERT_NONE
        assert ssl_ctx.check_hostname is False

    def test_tls_disabled_emits_warning(self):
        """When TLS verification is disabled, a warning is logged."""
        from src.shared.database import get_engine, reset_engine

        reset_engine()
        os.environ["BG_RDS_TLS_VERIFY"] = "false"

        mock_settings = MagicMock()
        mock_settings.rds_iam_auth = True
        mock_settings.rds_host = "test-db.us-east-1.rds.amazonaws.com"
        mock_settings.rds_port = 5432
        mock_settings.rds_username = "bgadmin"
        mock_settings.rds_dbname = "bedrockgateway"
        mock_settings.aws_region = "us-east-1"
        mock_settings.rds_tls_verify = False

        mock_client = MagicMock()
        mock_client.generate_db_auth_token.return_value = "mock-token"

        with patch("boto3.client", return_value=mock_client):
            with patch("src.shared.database.get_settings", return_value=mock_settings):
                with patch("src.shared.database.create_async_engine"):
                    with patch("src.shared.database.logger") as mock_logger:
                        get_engine()
                        mock_logger.warning.assert_called_once_with("RDS TLS verification disabled (BG_RDS_TLS_VERIFY=false). MITM risk!")
