"""
Shared pytest configuration and fixtures for BedrockGateway tests.

This module provides common test infrastructure including:
- Database session fixtures with SQLite for fast testing
- HTTP client fixtures for API testing
- Mock service fixtures for external dependencies
- Redis fixtures for rate limiting tests
- Environment setup for test mode
"""

import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.base import Base
from src.shared.schemas.auth import TokenContext

# Set test environment variables before importing app modules
os.environ["TESTING"] = "1"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["AWS_REGION"] = "us-east-1"
os.environ["JWT_SECRET_KEY"] = "test-secret-key-do-not-use-in-production"
os.environ["BG_TOKEN_SECRET_KEY"] = "test-secret-key-do-not-use-in-production"
os.environ["REDIS_URL"] = ""  # Disable Redis in tests by default

# Test database URL
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# =============================================================================
# Pytest Configuration
# =============================================================================


def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line("markers", "integration: mark test as integration test")
    config.addinivalue_line("markers", "e2e: mark test as end-to-end test")
    config.addinivalue_line("markers", "slow: mark test as slow running")
    config.addinivalue_line("markers", "unit: mark test as unit test")


# =============================================================================
# Event Loop Configuration
# =============================================================================

# Use session-scoped event loop via pytest-asyncio configuration
# instead of deprecated event_loop fixture override


# =============================================================================
# Database Fixtures
# =============================================================================


@pytest.fixture
async def test_engine():
    """Create a test database engine with SQLite."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    # Clean up
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create a test database session."""
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async with session_factory() as session:
        yield session
        # Rollback any uncommitted changes after each test
        await session.rollback()


@pytest.fixture
async def db_session_factory(test_engine) -> async_sessionmaker[AsyncSession]:
    """Create a session factory for tests that need multiple sessions."""
    return async_sessionmaker(test_engine, expire_on_commit=False)


# =============================================================================
# Application Client Fixtures
# =============================================================================


@pytest.fixture
async def app():
    """Create the FastAPI application for testing."""
    # Import here to ensure environment is set up first
    from src.app import create_app

    application = create_app()
    yield application


@pytest.fixture
async def test_client(app, test_engine) -> AsyncGenerator[AsyncClient, None]:
    """Create an async HTTP client for testing the application."""
    # Override the database dependency
    from src.shared.database import get_db

    async def override_get_db():
        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture
async def authenticated_client(
    test_client: AsyncClient,
    valid_token_context: TokenContext,
) -> AsyncClient:
    """Create an authenticated client with valid token headers."""
    test_client.headers["Authorization"] = "Bearer bg-test-valid-token"
    return test_client


@pytest.fixture
async def admin_client(
    test_client: AsyncClient,
    admin_token_context: TokenContext,
) -> AsyncClient:
    """Create an authenticated client with admin privileges."""
    test_client.headers["Authorization"] = "Bearer bg-test-admin-token"
    return test_client


# =============================================================================
# Token Context Fixtures
# =============================================================================


@pytest.fixture
def valid_token_context() -> TokenContext:
    """Create a valid token context for authenticated requests."""
    return TokenContext(
        user_id="user-test-123",
        org_id="org-test-123",
        team_id="team-test-123",
        department_id="dept-test-123",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def admin_token_context() -> TokenContext:
    """Create a token context with admin privileges."""
    return TokenContext(
        user_id="user-admin-123",
        org_id="org-test-123",
        team_id="team-test-123",
        department_id="dept-test-123",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def service_account_token_context() -> TokenContext:
    """Create a token context for a service account."""
    return TokenContext(
        user_id="sa-test-123",
        org_id="org-test-123",
        team_id="team-test-123",
        department_id="dept-test-123",
        account_type="service",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def expired_token_context() -> TokenContext:
    """Create an expired token context for testing auth failures."""
    return TokenContext(
        user_id="user-expired-123",
        org_id="org-test-123",
        team_id="team-test-123",
        department_id="dept-test-123",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )


# =============================================================================
# Mock Service Fixtures
# =============================================================================


@pytest.fixture
def mock_auth_service() -> AsyncMock:
    """Create a mock authentication service."""
    mock = AsyncMock()
    mock.validate_token.return_value = TokenContext(
        user_id="user-test-123",
        org_id="org-test-123",
        team_id="team-test-123",
        department_id="dept-test-123",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return mock


@pytest.fixture
def mock_budget_service() -> AsyncMock:
    """Create a mock budget service."""
    from src.shared.schemas.common import BudgetCheckResult

    mock = AsyncMock()
    mock.check_budget.return_value = BudgetCheckResult(
        allowed=True,
        warnings=[],
    )
    mock.record_usage.return_value = None
    return mock


@pytest.fixture
def mock_rate_limit_service() -> AsyncMock:
    """Create a mock rate limit service."""
    from src.shared.schemas.common import RateLimitCheckResult

    mock = AsyncMock()
    mock.check_rate_limit.return_value = RateLimitCheckResult(
        allowed=True,
        limit=100,
        remaining=99,
    )
    mock.release_concurrent.return_value = None
    return mock


@pytest.fixture
def mock_pool_service() -> AsyncMock:
    """Create a mock pool service."""
    from tests.fixtures.mock_aws import MockBedrockClient

    mock = AsyncMock()
    mock.get_client.return_value = MockBedrockClient()
    mock.report_error.return_value = None
    mock.get_pool_status.return_value = [
        {"account_id": "111111111111", "is_healthy": True, "request_count": 100},
        {"account_id": "222222222222", "is_healthy": True, "request_count": 95},
    ]
    return mock


@pytest.fixture
def mock_proxy_service() -> AsyncMock:
    """Create a mock proxy service."""
    mock = AsyncMock()
    mock.invoke.return_value = {
        "id": "msg-test-123",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Mock response"}],
        "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 150},
    }
    return mock


@pytest.fixture
def mock_usage_service() -> AsyncMock:
    """Create a mock usage service."""
    mock = AsyncMock()
    mock.log_request.return_value = None
    mock.query_logs.return_value = []
    mock.get_usage_summary.return_value = {
        "total_requests": 1000,
        "total_tokens": 500000,
        "total_cost_usd": 150.00,
    }
    return mock


# =============================================================================
# Redis Fixtures
# =============================================================================


@pytest.fixture
def mock_redis():
    """Create a mock Redis client using fakeredis."""
    try:
        import fakeredis.aioredis

        return fakeredis.aioredis.FakeRedis()
    except ImportError:
        # Fall back to MagicMock if fakeredis not available
        mock = MagicMock()
        mock.get = AsyncMock(return_value=None)
        mock.set = AsyncMock(return_value=True)
        mock.incr = AsyncMock(return_value=1)
        mock.expire = AsyncMock(return_value=True)
        mock.ttl = AsyncMock(return_value=60)
        return mock


@pytest.fixture
def redis_client(mock_redis):
    """Alias for mock_redis fixture."""
    return mock_redis


# =============================================================================
# AWS Mock Fixtures
# =============================================================================


@pytest.fixture
def mock_sts_client():
    """Create a mock STS client."""
    from tests.fixtures.mock_aws import MockSTSClient

    return MockSTSClient()


@pytest.fixture
def mock_bedrock_client():
    """Create a mock Bedrock client."""
    from tests.fixtures.mock_aws import MockBedrockClient

    return MockBedrockClient()


@pytest.fixture
def mock_bedrock_pool():
    """Create a pool of mock Bedrock clients."""
    from tests.fixtures.mock_aws import create_mock_bedrock_pool

    return create_mock_bedrock_pool(num_accounts=3)


# =============================================================================
# Test Data Fixtures
# =============================================================================


@pytest.fixture
async def seeded_database(db_session: AsyncSession) -> dict[str, Any]:
    """Seed the database with test data and return references."""
    from tests.fixtures.seed_data import seed_test_database

    return await seed_test_database(db_session)


@pytest.fixture
async def seeded_budget_data(db_session: AsyncSession, seeded_database: dict) -> dict[str, Any]:
    """Seed budget-related test data."""
    from tests.fixtures.seed_data import seed_budget_data

    budget_data = await seed_budget_data(db_session, seeded_database)
    return {**seeded_database, **budget_data}


@pytest.fixture
async def seeded_rate_limit_data(db_session: AsyncSession, seeded_database: dict) -> dict[str, Any]:
    """Seed rate limit test data."""
    from tests.fixtures.seed_data import seed_rate_limit_data

    rate_limit_data = await seed_rate_limit_data(db_session, seeded_database)
    return {**seeded_database, **rate_limit_data}


@pytest.fixture
async def full_test_environment(db_session: AsyncSession) -> dict[str, Any]:
    """Create a complete test environment with all data."""
    from tests.fixtures.seed_data import create_full_test_environment

    return await create_full_test_environment(db_session)


# =============================================================================
# Helper Fixtures
# =============================================================================


@pytest.fixture
def sample_chat_completion_request() -> dict[str, Any]:
    """Create a sample OpenAI-format chat completion request."""
    return {
        "model": "claude-3.5-sonnet",
        "messages": [
            {"role": "user", "content": "Hello, how are you?"},
        ],
        "max_tokens": 1024,
        "stream": False,
    }


@pytest.fixture
def sample_streaming_request() -> dict[str, Any]:
    """Create a sample streaming chat completion request."""
    return {
        "model": "claude-3.5-sonnet",
        "messages": [
            {"role": "user", "content": "Tell me a short story."},
        ],
        "max_tokens": 2048,
        "stream": True,
    }


@pytest.fixture
def sample_bedrock_request() -> dict[str, Any]:
    """Create a sample Bedrock InvokeModel request."""
    return {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "What is the meaning of life?"},
        ],
    }


@pytest.fixture
def sample_anthropic_request() -> dict[str, Any]:
    """Create a sample Anthropic Messages API request."""
    return {
        "model": "claude-3-5-sonnet-20241022",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "Explain quantum computing."},
        ],
    }


# =============================================================================
# Auth Override Fixtures (Issue #133)
# =============================================================================


@pytest.fixture
def override_get_current_user(admin_token_context: TokenContext):
    """
    Issue #133: Provides a function to override get_current_user dependency.

    Use this fixture to override the Cognito JWT validation in tests:

        from src.auth.dependencies import get_current_user

        def test_something(app, override_get_current_user):
            app.dependency_overrides[get_current_user] = override_get_current_user
            # ... your test code
            app.dependency_overrides.clear()
    """

    async def _override():
        return admin_token_context

    return _override


@pytest.fixture
def override_get_current_user_regular(valid_token_context: TokenContext):
    """
    Issue #133: Override get_current_user with a non-admin user.
    """

    async def _override():
        return valid_token_context

    return _override


# =============================================================================
# Cleanup Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_github_app_provider():
    """Reset the GitHubAppCredsProvider singleton between tests.

    Issue #2594: The provider caches credentials in-process. Without this
    reset, tests that change BG_GITHUB_APP_* env vars see stale cached values
    from previous tests.
    """
    from src.admin.connections.github_app_provider import _reset_provider_for_testing

    _reset_provider_for_testing(None)
    yield
    _reset_provider_for_testing(None)


@pytest.fixture
async def cleanup_after_test(db_session: AsyncSession):
    """Clean up database after each test.

    Note: This fixture is NOT autouse to avoid running for tests that don't
    use the local database (e.g., live API tests). Tests that need database
    cleanup should explicitly request this fixture.
    """
    yield
    # Rollback is handled by db_session fixture


@pytest.fixture
async def clean_database(db_session: AsyncSession):
    """Explicitly clean the database before a test."""
    from tests.fixtures.seed_data import clear_test_data

    await clear_test_data(db_session)
    yield db_session
