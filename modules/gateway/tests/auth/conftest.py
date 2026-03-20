"""
Pytest configuration and fixtures for authentication module tests.

This module provides shared fixtures and configuration for testing
all authentication components with proper mocking and isolation.
"""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.auth.auth_service import AuthService
from src.auth.schemas import AWSCallerIdentity, TenantInfo
from src.auth.service_account_service import ServiceAccountService
from src.auth.sts_client import STSClient
from src.auth.tenant_resolver import TenantResolver
from src.auth.token_manager import TokenManager
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, ServiceAccount, Team, User
from src.shared.models.token import Token

# Test database URL (in-memory SQLite for fast tests)
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def test_engine():
    """Create a test database engine."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=StaticPool, connect_args={"check_same_thread": False})

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


@pytest.fixture
async def sample_organization(db_session: AsyncSession) -> Organization:
    """Create a sample organization for testing."""
    org = Organization(
        id="org-test-123",
        name="Test Organization",
        aws_accounts=["123456789012", "210987654321"],
        role_mappings={"admin_roles": ["AdminRole", "PlatformAdminRole"], "admin_groups": ["AdminGroup", "PlatformAdmins"]},
        settings={"timezone": "UTC", "default_region": "us-east-1"},
    )

    db_session.add(org)
    await db_session.commit()
    await db_session.refresh(org)
    return org


@pytest.fixture
async def sample_department(db_session: AsyncSession, sample_organization: Organization) -> Department:
    """Create a sample department for testing."""
    dept = Department(id="dept-engineering", org_id=sample_organization.id, name="Engineering", identity_center_group_id="eng-group-123")

    db_session.add(dept)
    await db_session.commit()
    await db_session.refresh(dept)
    return dept


@pytest.fixture
async def sample_team(db_session: AsyncSession, sample_organization: Organization, sample_department: Department) -> Team:
    """Create a sample team for testing."""
    team = Team(
        id="team-ml",
        org_id=sample_organization.id,
        department_id=sample_department.id,
        name="Machine Learning",
        identity_center_group_id="ml-team-456",
    )

    db_session.add(team)
    await db_session.commit()
    await db_session.refresh(team)
    return team


@pytest.fixture
async def sample_user(db_session: AsyncSession, sample_organization: Organization, sample_team: Team) -> User:
    """Create a sample user for testing."""
    user = User(
        id="user-john-doe", org_id=sample_organization.id, team_id=sample_team.id, email="john.doe@test.com", identity_center_user_id="user-123456"
    )

    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def sample_service_account(
    db_session: AsyncSession, sample_organization: Organization, sample_department: Department, sample_team: Team
) -> ServiceAccount:
    """Create a sample service account for testing."""
    service_account = ServiceAccount(
        id="sa-ml-training",
        org_id=sample_organization.id,
        department_id=sample_department.id,
        team_id=sample_team.id,
        name="ML Training Service",
        iam_role_arn="arn:aws:iam::123456789012:role/ml-training-service",
    )

    db_session.add(service_account)
    await db_session.commit()
    await db_session.refresh(service_account)
    return service_account


@pytest.fixture
def mock_sts_responses() -> dict[str, Any]:
    """Mock responses for STS client testing."""
    return {
        "get_caller_identity": {
            "UserId": "AIDACKCEVSQ6C2EXAMPLE",
            "Account": "123456789012",
            "Arn": "arn:aws:sts::123456789012:assumed-role/ml-training-service/test-session",
        }
    }


@pytest.fixture
def mock_human_user_sts_responses() -> dict[str, Any]:
    """Mock STS responses for human user testing."""
    return {
        "get_caller_identity": {
            "UserId": "AIDACKCEVSQ6C2EXAMPLE",
            "Account": "123456789012",
            "Arn": "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Developer/john.doe@test.com",
        }
    }


@pytest.fixture
def mock_unknown_org_sts_responses() -> dict[str, Any]:
    """Mock STS responses for unknown organization testing."""
    return {
        "get_caller_identity": {
            "UserId": "AIDACKCEVSQ6C2EXAMPLE",
            "Account": "999888777666",  # Unknown account
            "Arn": "arn:aws:sts::999888777666:assumed-role/some-role/session",
        }
    }


@pytest.fixture
def mock_unregistered_service_account_sts_responses() -> dict[str, Any]:
    """Mock STS responses for unregistered service account testing."""
    return {
        "get_caller_identity": {
            "UserId": "AIDACKCEVSQ6C2EXAMPLE",
            "Account": "123456789012",
            "Arn": "arn:aws:sts::123456789012:assumed-role/unregistered-role/session",
        }
    }


@pytest.fixture
def mock_sts_client(mock_sts_responses: dict[str, Any]) -> STSClient:
    """Create a mock STS client for testing."""
    return STSClient(mock_responses=mock_sts_responses)


@pytest.fixture
def human_user_sts_client(mock_human_user_sts_responses: dict[str, Any]) -> STSClient:
    """Create a mock STS client for human user testing."""
    return STSClient(mock_responses=mock_human_user_sts_responses)


@pytest.fixture
def unknown_org_sts_client(mock_unknown_org_sts_responses: dict[str, Any]) -> STSClient:
    """Create a mock STS client for unknown organization testing."""
    return STSClient(mock_responses=mock_unknown_org_sts_responses)


@pytest.fixture
def unregistered_sa_sts_client(mock_unregistered_service_account_sts_responses: dict[str, Any]) -> STSClient:
    """Create a mock STS client for unregistered service account testing."""
    return STSClient(mock_responses=mock_unregistered_service_account_sts_responses)


@pytest.fixture
def tenant_resolver() -> TenantResolver:
    """Create a tenant resolver for testing."""
    return TenantResolver()


@pytest.fixture
def token_manager() -> TokenManager:
    """Create a token manager for testing."""
    return TokenManager("test-secret-key-do-not-use-in-production")


@pytest.fixture
def auth_service(mock_sts_client: STSClient, tenant_resolver: TenantResolver, token_manager: TokenManager) -> AuthService:
    """Create an auth service with mocked dependencies."""
    return AuthService(sts_client=mock_sts_client, tenant_resolver=tenant_resolver, token_manager=token_manager)


@pytest.fixture
def human_user_auth_service(human_user_sts_client: STSClient, tenant_resolver: TenantResolver, token_manager: TokenManager) -> AuthService:
    """Create an auth service for human user testing."""
    return AuthService(sts_client=human_user_sts_client, tenant_resolver=tenant_resolver, token_manager=token_manager)


@pytest.fixture
def unknown_org_auth_service(unknown_org_sts_client: STSClient, tenant_resolver: TenantResolver, token_manager: TokenManager) -> AuthService:
    """Create an auth service for unknown organization testing."""
    return AuthService(sts_client=unknown_org_sts_client, tenant_resolver=tenant_resolver, token_manager=token_manager)


@pytest.fixture
def unregistered_sa_auth_service(unregistered_sa_sts_client: STSClient, tenant_resolver: TenantResolver, token_manager: TokenManager) -> AuthService:
    """Create an auth service for unregistered service account testing."""
    return AuthService(sts_client=unregistered_sa_sts_client, tenant_resolver=tenant_resolver, token_manager=token_manager)


@pytest.fixture
def service_account_service() -> ServiceAccountService:
    """Create a service account service for testing."""
    return ServiceAccountService()


@pytest.fixture
def sample_tenant_info() -> TenantInfo:
    """Create sample tenant information for testing."""
    return TenantInfo(
        org_id="org-test-123",
        org_name="Test Organization",
        department_id="dept-engineering",
        team_id="team-ml",
        account_type="service",
        entity_id="sa-ml-training",
        is_admin=False,
    )


@pytest.fixture
def admin_tenant_info() -> TenantInfo:
    """Create admin tenant information for testing."""
    return TenantInfo(
        org_id="org-test-123",
        org_name="Test Organization",
        department_id="dept-engineering",
        team_id="team-ml",
        account_type="human",
        entity_id="user-admin",
        is_admin=True,
    )


@pytest.fixture
def sample_aws_caller_identity() -> AWSCallerIdentity:
    """Create sample AWS caller identity for testing."""
    return AWSCallerIdentity(
        user_id="AIDACKCEVSQ6C2EXAMPLE", account="123456789012", arn="arn:aws:sts::123456789012:assumed-role/ml-training-service/test-session"
    )


@pytest.fixture
def valid_token_data() -> dict[str, Any]:
    """Create valid token data for testing."""
    future_time = datetime.now(UTC) + timedelta(hours=1)
    return {
        "sub": "user-test-123",
        "org_id": "org-test-123",
        "team_id": "team-ml",
        "department_id": "dept-engineering",
        "account_type": "human",
        "is_admin": False,
        "exp": int(future_time.timestamp()),
        "iat": int(datetime.now(UTC).timestamp()),
        "jti": "token-123456",
    }


@pytest.fixture
def expired_token_data() -> dict[str, Any]:
    """Create expired token data for testing."""
    past_time = datetime.now(UTC) - timedelta(hours=1)
    return {
        "sub": "user-test-123",
        "org_id": "org-test-123",
        "team_id": "team-ml",
        "department_id": "dept-engineering",
        "account_type": "human",
        "is_admin": False,
        "exp": int(past_time.timestamp()),
        "iat": int(datetime.now(UTC).timestamp()),
        "jti": "token-expired",
    }


# Helper functions for tests


async def create_sample_token(
    db_session: AsyncSession,
    tenant_info: TenantInfo,
    token_hash: str = "sample-token-hash",
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> Token:
    """Helper to create a sample token in the database."""
    if expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(hours=1)

    token = Token(
        token_hash=token_hash,
        entity_type=tenant_info.account_type,
        entity_id=tenant_info.entity_id,
        org_id=tenant_info.org_id,
        team_id=tenant_info.team_id,
        department_id=tenant_info.department_id,
        is_admin=tenant_info.is_admin,
        expires_at=expires_at,
        revoked_at=revoked_at,
    )

    db_session.add(token)
    await db_session.commit()
    await db_session.refresh(token)
    return token
