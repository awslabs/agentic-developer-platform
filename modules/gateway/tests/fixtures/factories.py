"""
Factory functions for creating test entities.

These factories create instances of ORM models for use in integration and E2E tests.
Each factory function creates realistic test data with sensible defaults that can be overridden.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import new_uuid
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.organization import Department, Organization, ServiceAccount, Team, User
from src.shared.models.token import Token
from src.shared.models.usage import BedrockPoolAccount, ModelPricing, RateLimitConfig, UsageLog
from src.shared.utils import generate_token, hash_token


async def create_org(
    session: AsyncSession,
    *,
    id: str | None = None,
    name: str | None = None,
    aws_accounts: list[str] | None = None,
    role_mappings: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> Organization:
    """
    Create an organization for testing.

    Args:
        session: Database session
        id: Organization ID (auto-generated if not provided)
        name: Organization name (auto-generated if not provided)
        aws_accounts: List of AWS account IDs associated with this org
        role_mappings: IAM role to department/team/admin mappings
        settings: Organization-level settings

    Returns:
        Created Organization instance
    """
    org_id = id or f"org-{new_uuid()[:8]}"
    org = Organization(
        id=org_id,
        name=name or f"Test Organization {org_id}",
        aws_accounts=aws_accounts or ["123456789012"],
        role_mappings=role_mappings
        or {
            "admin_roles": ["AdminRole", "OrgAdminRole"],
            "admin_groups": ["Administrators"],
            "role_to_department": {
                "AWSReservedSSO_Developer": "engineering",
                "AWSReservedSSO_DataScientist": "data-science",
            },
        },
        settings=settings or {"timezone": "UTC", "default_region": "us-east-1"},
    )

    session.add(org)
    await session.flush()
    return org


async def create_department(
    session: AsyncSession,
    org_id: str,
    *,
    id: str | None = None,
    name: str | None = None,
    identity_center_group_id: str | None = None,
) -> Department:
    """
    Create a department for testing.

    Args:
        session: Database session
        org_id: Parent organization ID
        id: Department ID (auto-generated if not provided)
        name: Department name (auto-generated if not provided)
        identity_center_group_id: AWS Identity Center group ID

    Returns:
        Created Department instance
    """
    dept_id = id or f"dept-{new_uuid()[:8]}"
    dept = Department(
        id=dept_id,
        org_id=org_id,
        name=name or f"Test Department {dept_id}",
        identity_center_group_id=identity_center_group_id or f"group-{dept_id}",
    )

    session.add(dept)
    await session.flush()
    return dept


async def create_team(
    session: AsyncSession,
    org_id: str,
    department_id: str,
    *,
    id: str | None = None,
    name: str | None = None,
    identity_center_group_id: str | None = None,
) -> Team:
    """
    Create a team for testing.

    Args:
        session: Database session
        org_id: Parent organization ID
        department_id: Parent department ID
        id: Team ID (auto-generated if not provided)
        name: Team name (auto-generated if not provided)
        identity_center_group_id: AWS Identity Center group ID

    Returns:
        Created Team instance
    """
    team_id = id or f"team-{new_uuid()[:8]}"
    team = Team(
        id=team_id,
        org_id=org_id,
        department_id=department_id,
        name=name or f"Test Team {team_id}",
        identity_center_group_id=identity_center_group_id or f"group-{team_id}",
    )

    session.add(team)
    await session.flush()
    return team


async def create_user(
    session: AsyncSession,
    org_id: str,
    team_id: str,
    *,
    id: str | None = None,
    email: str | None = None,
    identity_center_user_id: str | None = None,
) -> User:
    """
    Create a user for testing.

    Args:
        session: Database session
        org_id: Parent organization ID
        team_id: Parent team ID
        id: User ID (auto-generated if not provided)
        email: User email address
        identity_center_user_id: AWS Identity Center user ID

    Returns:
        Created User instance
    """
    user_id = id or f"user-{new_uuid()[:8]}"
    user = User(
        id=user_id,
        org_id=org_id,
        team_id=team_id,
        email=email or f"{user_id}@test.example.com",
        identity_center_user_id=identity_center_user_id or f"ic-user-{user_id}",
    )

    session.add(user)
    await session.flush()
    return user


async def create_service_account(
    session: AsyncSession,
    org_id: str,
    department_id: str,
    team_id: str,
    *,
    id: str | None = None,
    name: str | None = None,
    iam_role_arn: str | None = None,
) -> ServiceAccount:
    """
    Create a service account for testing.

    Args:
        session: Database session
        org_id: Parent organization ID
        department_id: Parent department ID
        team_id: Parent team ID
        id: Service account ID (auto-generated if not provided)
        name: Service account name
        iam_role_arn: IAM role ARN for this service account

    Returns:
        Created ServiceAccount instance
    """
    sa_id = id or f"sa-{new_uuid()[:8]}"
    service_account = ServiceAccount(
        id=sa_id,
        org_id=org_id,
        department_id=department_id,
        team_id=team_id,
        name=name or f"Test Service Account {sa_id}",
        iam_role_arn=iam_role_arn or f"arn:aws:iam::123456789012:role/{sa_id}",
    )

    session.add(service_account)
    await session.flush()
    return service_account


async def create_token(
    session: AsyncSession,
    org_id: str,
    team_id: str,
    department_id: str,
    entity_id: str,
    *,
    entity_type: str = "human",
    is_admin: bool = False,
    expires_in_hours: float = 12.0,
    revoked: bool = False,
) -> tuple[Token, str]:
    """
    Create a token for testing.

    Args:
        session: Database session
        org_id: Organization ID
        team_id: Team ID
        department_id: Department ID
        entity_id: User or service account ID
        entity_type: Either "human" or "service"
        is_admin: Whether this token has admin privileges
        expires_in_hours: Token expiration time in hours
        revoked: Whether to create a revoked token

    Returns:
        Tuple of (Token instance, raw token string)
    """
    raw_token = generate_token()
    token_hash = hash_token(raw_token)
    expires_at = datetime.now(UTC) + timedelta(hours=expires_in_hours)

    token = Token(
        token_hash=token_hash,
        entity_type=entity_type,
        entity_id=entity_id,
        org_id=org_id,
        team_id=team_id,
        department_id=department_id,
        is_admin=is_admin,
        expires_at=expires_at,
        revoked_at=datetime.now(UTC) if revoked else None,
    )

    session.add(token)
    await session.flush()
    return token, raw_token


async def create_budget_config(
    session: AsyncSession,
    org_id: str,
    entity_type: str,
    entity_id: str,
    *,
    id: str | None = None,
    period_type: str = "monthly",
    budget_amount_usd: Decimal | None = None,
    enforcement_mode: str = "hard",
) -> BudgetConfig:
    """
    Create a budget configuration for testing.

    Args:
        session: Database session
        org_id: Organization ID
        entity_type: Entity type (org/department/team/user/service_account)
        entity_id: Entity ID
        id: Budget config ID (auto-generated if not provided)
        period_type: Budget period (daily/weekly/monthly)
        budget_amount_usd: Budget amount in USD
        enforcement_mode: Enforcement mode (soft/hard)

    Returns:
        Created BudgetConfig instance
    """
    budget = BudgetConfig(
        id=id or f"budget-{new_uuid()[:8]}",
        org_id=org_id,
        entity_type=entity_type,
        entity_id=entity_id,
        period_type=period_type,
        budget_amount_usd=budget_amount_usd or Decimal("1000.00"),
        enforcement_mode=enforcement_mode,
    )

    session.add(budget)
    await session.flush()
    return budget


async def create_budget_usage(
    session: AsyncSession,
    org_id: str,
    entity_type: str,
    entity_id: str,
    *,
    period_start: datetime | None = None,
    period_type: str = "monthly",
    total_cost_usd: Decimal | None = None,
    total_tokens: int = 0,
    request_count: int = 0,
) -> BudgetUsage:
    """
    Create a budget usage record for testing.

    Args:
        session: Database session
        org_id: Organization ID
        entity_type: Entity type
        entity_id: Entity ID
        period_start: Period start date
        period_type: Budget period type
        total_cost_usd: Total cost in USD
        total_tokens: Total tokens used
        request_count: Total request count

    Returns:
        Created BudgetUsage instance
    """
    if period_start is None:
        now = datetime.now(UTC)
        period_start = datetime(now.year, now.month, 1, tzinfo=UTC)

    usage = BudgetUsage(
        id=f"usage-{new_uuid()[:8]}",
        org_id=org_id,
        entity_type=entity_type,
        entity_id=entity_id,
        period_start=period_start.date(),
        period_type=period_type,
        total_cost_usd=total_cost_usd or Decimal("0.00"),
        total_tokens=total_tokens,
        request_count=request_count,
    )

    session.add(usage)
    await session.flush()
    return usage


async def create_rate_limit_config(
    session: AsyncSession,
    org_id: str,
    entity_type: str,
    entity_id: str,
    *,
    id: str | None = None,
    rpm: int | None = None,
    tpm: int | None = None,
    concurrent_requests: int | None = None,
) -> RateLimitConfig:
    """
    Create a rate limit configuration for testing.

    Args:
        session: Database session
        org_id: Organization ID
        entity_type: Entity type (org/department/team/user)
        entity_id: Entity ID
        id: Rate limit config ID (auto-generated if not provided)
        rpm: Requests per minute limit
        tpm: Tokens per minute limit
        concurrent_requests: Maximum concurrent requests

    Returns:
        Created RateLimitConfig instance
    """
    rate_limit = RateLimitConfig(
        id=id or f"rl-{new_uuid()[:8]}",
        org_id=org_id,
        entity_type=entity_type,
        entity_id=entity_id,
        rpm=rpm or 100,
        tpm=tpm or 100000,
        concurrent_requests=concurrent_requests or 10,
    )

    session.add(rate_limit)
    await session.flush()
    return rate_limit


async def create_pool_account(
    session: AsyncSession,
    *,
    id: str | None = None,
    account_id: str | None = None,
    role_arn: str | None = None,
    region: str = "us-east-1",
    is_healthy: bool = True,
) -> BedrockPoolAccount:
    """
    Create a Bedrock pool account for testing.

    Args:
        session: Database session
        id: Pool account ID (auto-generated if not provided)
        account_id: AWS account ID
        role_arn: IAM role ARN for cross-account access
        region: AWS region
        is_healthy: Whether the account is healthy

    Returns:
        Created BedrockPoolAccount instance
    """
    pool_id = id or f"pool-{new_uuid()[:8]}"
    aws_account = account_id or f"{new_uuid()[:12].replace('-', '')}".ljust(12, "0")[:12]

    pool_account = BedrockPoolAccount(
        id=pool_id,
        account_id=aws_account,
        role_arn=role_arn or f"arn:aws:iam::{aws_account}:role/BedrockPoolRole",
        region=region,
        is_healthy=is_healthy,
        last_health_check=datetime.now(UTC) if is_healthy else None,
    )

    session.add(pool_account)
    await session.flush()
    return pool_account


async def create_model_pricing(
    session: AsyncSession,
    model_id: str,
    *,
    input_price_per_1k: Decimal | None = None,
    output_price_per_1k: Decimal | None = None,
) -> ModelPricing:
    """
    Create model pricing configuration for testing.

    Args:
        session: Database session
        model_id: Model identifier
        input_price_per_1k: Price per 1000 input tokens
        output_price_per_1k: Price per 1000 output tokens

    Returns:
        Created ModelPricing instance
    """
    pricing = ModelPricing(
        model_id=model_id,
        input_price_per_1k=input_price_per_1k or Decimal("0.003"),
        output_price_per_1k=output_price_per_1k or Decimal("0.015"),
    )

    session.add(pricing)
    await session.flush()
    return pricing


async def create_usage_log(
    session: AsyncSession,
    org_id: str,
    department_id: str,
    team_id: str,
    user_id: str,
    *,
    account_type: str = "human",
    model: str = "anthropic.claude-3-5-sonnet-20241022-v2:0",
    input_tokens: int = 100,
    output_tokens: int = 200,
    cost_usd: Decimal | None = None,
    latency_ms: int = 500,
    status_code: int = 200,
    request_id: str | None = None,
    bedrock_account_id: str | None = None,
) -> UsageLog:
    """
    Create a usage log entry for testing.

    Args:
        session: Database session
        org_id: Organization ID
        department_id: Department ID
        team_id: Team ID
        user_id: User ID
        account_type: Account type (human/service)
        model: Model identifier
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        cost_usd: Request cost in USD
        latency_ms: Request latency in milliseconds
        status_code: HTTP status code
        request_id: Request ID
        bedrock_account_id: Bedrock pool account ID used

    Returns:
        Created UsageLog instance
    """
    usage_log = UsageLog(
        id=f"log-{new_uuid()[:8]}",
        org_id=org_id,
        department_id=department_id,
        team_id=team_id,
        user_id=user_id,
        account_type=account_type,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd or Decimal("0.003300"),
        latency_ms=latency_ms,
        status_code=status_code,
        request_id=request_id or f"req-{new_uuid()[:8]}",
        bedrock_account_id=bedrock_account_id,
    )

    session.add(usage_log)
    await session.flush()
    return usage_log
