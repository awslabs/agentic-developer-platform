"""Pytest fixtures for admin module tests."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.admin.access_control import AccessControl
from src.admin.service import AdminService
from src.shared.interfaces.budget import IBudgetService
from src.shared.interfaces.ratelimit import IRateLimitService
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.usage import BedrockPoolAccount
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.common import BudgetCheckResult, RateLimitCheckResult

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

    # Set up async mocks
    service.check_budget = AsyncMock(
        return_value=BudgetCheckResult(
            allowed=True,
            budget_usd=1000.0,
            spent_usd=100.0,
        )
    )
    service.get_budgets_for_entity = AsyncMock(return_value=[])
    service.create_budget = AsyncMock()
    service.update_budget = AsyncMock()
    service.delete_budget = AsyncMock(return_value=True)

    return service


@pytest.fixture
def mock_ratelimit_service() -> IRateLimitService:
    """Create a mock rate limit service."""
    service = MagicMock(spec=IRateLimitService)

    # Set up async mocks
    service.check_rate_limit = AsyncMock(
        return_value=RateLimitCheckResult(
            allowed=True,
            remaining=100,
        )
    )
    service.release_concurrent = AsyncMock()

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
def org_admin_context() -> TokenContext:
    """Create a token context for an org admin."""
    return TokenContext(
        user_id="org-admin-001",
        org_id="org-001",
        team_id="team-001",
        department_id="dept-001",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def dept_admin_context() -> TokenContext:
    """Create a token context for a department admin."""
    return TokenContext(
        user_id="dept-admin-001",
        org_id="org-001",
        team_id="team-001",
        department_id="dept-001",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def regular_user_context() -> TokenContext:
    """Create a token context for a regular user."""
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
async def sample_organizations(db_session: AsyncSession) -> list[Organization]:
    """Create sample organizations in the database."""
    orgs = [
        Organization(
            id="org-001",
            name="Test Organization 1",
            aws_accounts=["111111111111"],
            role_mappings={"admin": "admin-role"},
            settings={"feature_x": True},
        ),
        Organization(
            id="org-002",
            name="Test Organization 2",
            aws_accounts=["222222222222"],
            role_mappings={},
            settings={},
        ),
        Organization(
            id="org-003",
            name="Test Organization 3",
            aws_accounts=["333333333333", "444444444444"],
            role_mappings={"viewer": "viewer-role"},
            settings={"feature_y": False},
        ),
    ]

    for org in orgs:
        db_session.add(org)

    await db_session.commit()
    return orgs


@pytest.fixture
async def sample_pool_accounts(db_session: AsyncSession) -> list[BedrockPoolAccount]:
    """Create sample pool accounts in the database."""
    accounts = [
        BedrockPoolAccount(
            id="pool-001",
            account_id="555555555555",
            role_arn="arn:aws:iam::555555555555:role/BedrockRole",
            region="us-east-1",
            is_healthy=True,
            last_health_check=datetime.now(UTC),
        ),
        BedrockPoolAccount(
            id="pool-002",
            account_id="666666666666",
            role_arn="arn:aws:iam::666666666666:role/BedrockRole",
            region="us-west-2",
            is_healthy=True,
            last_health_check=datetime.now(UTC),
        ),
        BedrockPoolAccount(
            id="pool-003",
            account_id="777777777777",
            role_arn="arn:aws:iam::777777777777:role/BedrockRole",
            region="us-east-1",
            is_healthy=False,
            last_health_check=datetime.now(UTC) - timedelta(hours=1),
        ),
    ]

    for account in accounts:
        db_session.add(account)

    await db_session.commit()
    return accounts


@pytest.fixture
async def admin_service(
    db_session: AsyncSession,
    mock_budget_service: IBudgetService,
    mock_ratelimit_service: IRateLimitService,
) -> AdminService:
    """Create an admin service instance with mocked dependencies."""
    return AdminService(
        db=db_session,
        budget_service=mock_budget_service,
        ratelimit_service=mock_ratelimit_service,
    )


@pytest.fixture
async def access_control(db_session: AsyncSession) -> AccessControl:
    """Create an access control instance."""
    return AccessControl(db=db_session)


@pytest.fixture
async def org_admin_membership(db_session: AsyncSession, org_admin_context: TokenContext) -> None:
    """Give ``org_admin_context`` the membership row that makes it a real org admin.

    Issue #3987: ``tenant_memberships.role`` — not a token claim — is the authority
    for org-level role, and PR 2 flipped the no-row fallback from ORG_ADMIN to
    MEMBER. A token that merely *says* org admin no longer resolves to one, so any
    spec asserting org-admin authority for this context must seed the row.

    ``TenantMembership.user_id`` is ``users.id``, while the token carries the
    Cognito sub, so a ``users`` row bridging the two is required as well.
    """
    db_session.add(Organization(id="org-001", name="org-001-name"))
    await db_session.flush()
    db_session.add(
        User(
            id="pg-org-admin-001",
            org_id="org-001",
            team_id="team-001",
            email="org-admin-001@example.test",
            name="org-admin-001",
            cognito_sub=org_admin_context.user_id,
            role="org_admin",
        )
    )
    await db_session.flush()
    db_session.add(
        TenantMembership(
            user_id="pg-org-admin-001",
            tenant_id="org-001",
            role="org_admin",
            is_active=True,
            joined_via="org_membership",
        )
    )
    await db_session.commit()


# Issue #133: Auth override fixtures for testing with real routes


@pytest.fixture
def override_auth_platform_admin(platform_admin_context: TokenContext):
    """
    Issue #133: Provides override for get_current_user to return platform admin.
    """
    from src.auth.dependencies import get_current_user

    async def _override():
        return platform_admin_context

    return _override, get_current_user


@pytest.fixture
def override_auth_org_admin(org_admin_context: TokenContext):
    """
    Issue #133: Provides override for get_current_user to return org admin.
    """
    from src.auth.dependencies import get_current_user

    async def _override():
        return org_admin_context

    return _override, get_current_user


@pytest.fixture
def override_auth_regular_user(regular_user_context: TokenContext):
    """
    Issue #133: Provides override for get_current_user to return regular user.
    """
    from src.auth.dependencies import get_current_user

    async def _override():
        return regular_user_context

    return _override, get_current_user
