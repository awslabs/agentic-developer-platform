"""Pytest fixtures for usage module tests."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.interfaces.budget import IBudgetService
from src.shared.interfaces.ratelimit import IRateLimitService
from src.shared.models.base import Base
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.common import BudgetCheckResult, RateLimitCheckResult
from src.usage.service import UsageService

# Use SQLite for testing
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def db_engine():
    """Create a test database engine."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create a test database session."""
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest.fixture
def mock_budget_service() -> IBudgetService:
    """Create a mock budget service."""
    service = MagicMock(spec=IBudgetService)
    service.check_budget = AsyncMock(return_value=BudgetCheckResult(allowed=True, budget_usd=1000.0, spent_usd=100.0))
    return service


@pytest.fixture
def mock_ratelimit_service() -> IRateLimitService:
    """Create a mock rate limit service."""
    service = MagicMock(spec=IRateLimitService)
    service.check_rate_limit = AsyncMock(return_value=RateLimitCheckResult(allowed=True, remaining=100))
    return service


@pytest.fixture
def platform_admin_context() -> TokenContext:
    """Create a token context for a platform admin."""
    return TokenContext(
        user_id="admin-user-001",
        org_id="platform",
        team_id="platform",
        department_id="platform",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def org_user_context() -> TokenContext:
    """Create a token context for a regular org user."""
    return TokenContext(
        user_id="user-001",
        org_id="org-001",
        team_id="team-001",
        department_id="dept-001",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def service_account_context() -> TokenContext:
    """Create a token context for a service account."""
    return TokenContext(
        user_id="service-001",
        org_id="org-001",
        team_id="team-001",
        department_id="dept-001",
        account_type="service",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
async def usage_service(db_session: AsyncSession) -> UsageService:
    """Create a usage service instance."""
    return UsageService(db=db_session)


@pytest.fixture
async def sample_usage_logs(db_session: AsyncSession) -> list[UsageLog]:
    """Create sample usage logs in the database."""
    now = datetime.now(UTC)
    logs = [
        # Org 1, User 1, various models
        UsageLog(
            id="usage-001",
            org_id="org-001",
            department_id="dept-001",
            team_id="team-001",
            user_id="user-001",
            account_type="human",
            model="claude-3-sonnet",
            input_tokens=100,
            output_tokens=200,
            cost_usd=Decimal("0.01"),
            latency_ms=150,
            status_code=200,
            timestamp=now - timedelta(hours=1),
        ),
        UsageLog(
            id="usage-002",
            org_id="org-001",
            department_id="dept-001",
            team_id="team-001",
            user_id="user-001",
            account_type="human",
            model="claude-3-opus",
            input_tokens=500,
            output_tokens=1000,
            cost_usd=Decimal("0.10"),
            latency_ms=500,
            status_code=200,
            timestamp=now - timedelta(hours=2),
        ),
        # Org 1, User 2
        UsageLog(
            id="usage-003",
            org_id="org-001",
            department_id="dept-001",
            team_id="team-002",
            user_id="user-002",
            account_type="human",
            model="claude-3-sonnet",
            input_tokens=200,
            output_tokens=300,
            cost_usd=Decimal("0.02"),
            latency_ms=200,
            status_code=200,
            timestamp=now - timedelta(hours=3),
        ),
        # Org 1, Error request
        UsageLog(
            id="usage-004",
            org_id="org-001",
            department_id="dept-001",
            team_id="team-001",
            user_id="user-001",
            account_type="human",
            model="claude-3-sonnet",
            input_tokens=50,
            output_tokens=0,
            cost_usd=Decimal("0.005"),
            latency_ms=50,
            status_code=400,
            timestamp=now - timedelta(hours=4),
        ),
        # Org 2, different org
        UsageLog(
            id="usage-005",
            org_id="org-002",
            department_id="dept-002",
            team_id="team-003",
            user_id="user-003",
            account_type="service",
            model="claude-3-haiku",
            input_tokens=1000,
            output_tokens=2000,
            cost_usd=Decimal("0.005"),
            latency_ms=100,
            status_code=200,
            timestamp=now - timedelta(days=1),
        ),
        # Org 1, Dept 2
        UsageLog(
            id="usage-006",
            org_id="org-001",
            department_id="dept-002",
            team_id="team-004",
            user_id="user-004",
            account_type="human",
            model="claude-3-sonnet",
            input_tokens=300,
            output_tokens=400,
            cost_usd=Decimal("0.03"),
            latency_ms=180,
            status_code=200,
            timestamp=now - timedelta(hours=5),
        ),
        # Old log for time range testing
        UsageLog(
            id="usage-007",
            org_id="org-001",
            department_id="dept-001",
            team_id="team-001",
            user_id="user-001",
            account_type="human",
            model="claude-3-sonnet",
            input_tokens=150,
            output_tokens=250,
            cost_usd=Decimal("0.015"),
            latency_ms=160,
            status_code=200,
            timestamp=now - timedelta(days=7),
        ),
    ]

    for log in logs:
        db_session.add(log)

    await db_session.commit()
    return logs
