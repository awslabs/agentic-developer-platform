"""
Test data seeding utilities for integration and E2E tests.

This module provides functions to populate the test database with
realistic test data for various test scenarios.
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.organization import Department, Organization, ServiceAccount, Team, User
from src.shared.models.token import Token
from src.shared.models.usage import BedrockPoolAccount, ModelPricing, RateLimitConfig, UsageLog

from .factories import (
    create_budget_config,
    create_budget_usage,
    create_department,
    create_model_pricing,
    create_org,
    create_pool_account,
    create_rate_limit_config,
    create_service_account,
    create_team,
    create_token,
    create_usage_log,
    create_user,
)


async def seed_test_database(session: AsyncSession) -> dict:
    """
    Seed the database with a complete set of test data.

    Creates organizations, departments, teams, users, service accounts,
    and tokens for comprehensive testing scenarios.

    Args:
        session: Database session

    Returns:
        Dictionary containing all created entities for test reference
    """
    data = {}

    # Create primary test organization
    org1 = await create_org(
        session,
        id="org-acme",
        name="Acme Corporation",
        aws_accounts=["123456789012", "123456789013"],
        role_mappings={
            "admin_roles": ["AWSReservedSSO_Administrator", "PlatformAdminRole"],
            "admin_groups": ["Administrators", "PlatformAdmins"],
            "role_to_department": {
                "AWSReservedSSO_Developer": "engineering",
                "AWSReservedSSO_DataScientist": "data-science",
                "AWSReservedSSO_Marketing": "marketing",
            },
        },
        settings={"timezone": "America/New_York", "default_region": "us-east-1"},
    )
    data["org1"] = org1

    # Create secondary test organization
    org2 = await create_org(
        session,
        id="org-contoso",
        name="Contoso Ltd",
        aws_accounts=["234567890123"],
        role_mappings={
            "admin_roles": ["AdminRole"],
            "admin_groups": ["Admins"],
            "role_to_department": {
                "AWSReservedSSO_Engineer": "engineering",
            },
        },
        settings={"timezone": "Europe/London", "default_region": "eu-west-1"},
    )
    data["org2"] = org2

    # Create departments for org1
    dept_eng = await create_department(
        session,
        org1.id,
        id="dept-engineering",
        name="Engineering",
    )
    data["dept_eng"] = dept_eng

    dept_ds = await create_department(
        session,
        org1.id,
        id="dept-data-science",
        name="Data Science",
    )
    data["dept_ds"] = dept_ds

    dept_mkt = await create_department(
        session,
        org1.id,
        id="dept-marketing",
        name="Marketing",
    )
    data["dept_mkt"] = dept_mkt

    # Create teams
    team_backend = await create_team(
        session,
        org1.id,
        dept_eng.id,
        id="team-backend",
        name="Backend Team",
    )
    data["team_backend"] = team_backend

    team_frontend = await create_team(
        session,
        org1.id,
        dept_eng.id,
        id="team-frontend",
        name="Frontend Team",
    )
    data["team_frontend"] = team_frontend

    team_ml = await create_team(
        session,
        org1.id,
        dept_ds.id,
        id="team-ml",
        name="ML Team",
    )
    data["team_ml"] = team_ml

    team_analytics = await create_team(
        session,
        org1.id,
        dept_ds.id,
        id="team-analytics",
        name="Analytics Team",
    )
    data["team_analytics"] = team_analytics

    team_content = await create_team(
        session,
        org1.id,
        dept_mkt.id,
        id="team-content",
        name="Content Team",
    )
    data["team_content"] = team_content

    # Create users
    user_alice = await create_user(
        session,
        org1.id,
        team_backend.id,
        id="user-alice",
        email="alice@acme.com",
    )
    data["user_alice"] = user_alice

    user_bob = await create_user(
        session,
        org1.id,
        team_ml.id,
        id="user-bob",
        email="bob@acme.com",
    )
    data["user_bob"] = user_bob

    user_charlie = await create_user(
        session,
        org1.id,
        team_content.id,
        id="user-charlie",
        email="charlie@acme.com",
    )
    data["user_charlie"] = user_charlie

    # Create admin user
    user_admin = await create_user(
        session,
        org1.id,
        team_backend.id,
        id="user-admin",
        email="admin@acme.com",
    )
    data["user_admin"] = user_admin

    # Create service accounts
    sa_cicd = await create_service_account(
        session,
        org1.id,
        dept_eng.id,
        team_backend.id,
        id="sa-cicd-pipeline",
        name="CI/CD Pipeline",
        iam_role_arn="arn:aws:iam::123456789012:role/cicd-pipeline-role",
    )
    data["sa_cicd"] = sa_cicd

    sa_ml_training = await create_service_account(
        session,
        org1.id,
        dept_ds.id,
        team_ml.id,
        id="sa-ml-training",
        name="ML Training Service",
        iam_role_arn="arn:aws:iam::123456789012:role/ml-training-role",
    )
    data["sa_ml_training"] = sa_ml_training

    sa_analytics = await create_service_account(
        session,
        org1.id,
        dept_ds.id,
        team_analytics.id,
        id="sa-analytics-batch",
        name="Analytics Batch Job",
        iam_role_arn="arn:aws:iam::123456789012:role/analytics-batch-role",
    )
    data["sa_analytics"] = sa_analytics

    # Create tokens
    token_alice, raw_token_alice = await create_token(
        session,
        org1.id,
        team_backend.id,
        dept_eng.id,
        user_alice.id,
        entity_type="human",
        is_admin=False,
    )
    data["token_alice"] = token_alice
    data["raw_token_alice"] = raw_token_alice

    token_bob, raw_token_bob = await create_token(
        session,
        org1.id,
        team_ml.id,
        dept_ds.id,
        user_bob.id,
        entity_type="human",
        is_admin=False,
    )
    data["token_bob"] = token_bob
    data["raw_token_bob"] = raw_token_bob

    token_admin, raw_token_admin = await create_token(
        session,
        org1.id,
        team_backend.id,
        dept_eng.id,
        user_admin.id,
        entity_type="human",
        is_admin=True,
    )
    data["token_admin"] = token_admin
    data["raw_token_admin"] = raw_token_admin

    token_sa_cicd, raw_token_sa_cicd = await create_token(
        session,
        org1.id,
        team_backend.id,
        dept_eng.id,
        sa_cicd.id,
        entity_type="service",
        is_admin=False,
    )
    data["token_sa_cicd"] = token_sa_cicd
    data["raw_token_sa_cicd"] = raw_token_sa_cicd

    token_sa_ml, raw_token_sa_ml = await create_token(
        session,
        org1.id,
        team_ml.id,
        dept_ds.id,
        sa_ml_training.id,
        entity_type="service",
        is_admin=False,
    )
    data["token_sa_ml"] = token_sa_ml
    data["raw_token_sa_ml"] = raw_token_sa_ml

    # Create expired token for testing
    token_expired, raw_token_expired = await create_token(
        session,
        org1.id,
        team_backend.id,
        dept_eng.id,
        user_alice.id,
        entity_type="human",
        expires_in_hours=-1.0,  # Already expired
    )
    data["token_expired"] = token_expired
    data["raw_token_expired"] = raw_token_expired

    # Create revoked token for testing
    token_revoked, raw_token_revoked = await create_token(
        session,
        org1.id,
        team_backend.id,
        dept_eng.id,
        user_alice.id,
        entity_type="human",
        revoked=True,
    )
    data["token_revoked"] = token_revoked
    data["raw_token_revoked"] = raw_token_revoked

    await session.commit()
    return data


async def seed_budget_data(session: AsyncSession, base_data: dict | None = None) -> dict:
    """
    Seed budget configurations and usage records for testing.

    Args:
        session: Database session
        base_data: Optional base data from seed_test_database

    Returns:
        Dictionary containing budget-related entities
    """
    data = {}

    # Use provided base data or create minimal set
    if base_data:
        org1 = base_data["org1"]
        dept_eng = base_data["dept_eng"]
        dept_ds = base_data["dept_ds"]
        team_backend = base_data["team_backend"]
        team_ml = base_data["team_ml"]
        user_alice = base_data["user_alice"]
        user_bob = base_data["user_bob"]
    else:
        # Create minimal set of entities
        org1 = await create_org(session, id="org-budget-test")
        dept_eng = await create_department(session, org1.id, id="dept-eng-budget")
        dept_ds = await create_department(session, org1.id, id="dept-ds-budget")
        team_backend = await create_team(session, org1.id, dept_eng.id, id="team-backend-budget")
        team_ml = await create_team(session, org1.id, dept_ds.id, id="team-ml-budget")
        user_alice = await create_user(session, org1.id, team_backend.id, id="user-alice-budget")
        user_bob = await create_user(session, org1.id, team_ml.id, id="user-bob-budget")

        data["org1"] = org1
        data["dept_eng"] = dept_eng
        data["dept_ds"] = dept_ds
        data["team_backend"] = team_backend
        data["team_ml"] = team_ml
        data["user_alice"] = user_alice
        data["user_bob"] = user_bob

    # Organization level budget
    budget_org = await create_budget_config(
        session,
        org1.id,
        "org",
        org1.id,
        period_type="monthly",
        budget_amount_usd=Decimal("10000.00"),
        enforcement_mode="hard",
    )
    data["budget_org"] = budget_org

    # Department level budgets
    budget_dept_eng = await create_budget_config(
        session,
        org1.id,
        "department",
        dept_eng.id,
        period_type="monthly",
        budget_amount_usd=Decimal("5000.00"),
        enforcement_mode="hard",
    )
    data["budget_dept_eng"] = budget_dept_eng

    budget_dept_ds = await create_budget_config(
        session,
        org1.id,
        "department",
        dept_ds.id,
        period_type="monthly",
        budget_amount_usd=Decimal("3000.00"),
        enforcement_mode="hard",
    )
    data["budget_dept_ds"] = budget_dept_ds

    # Team level budgets
    budget_team_backend = await create_budget_config(
        session,
        org1.id,
        "team",
        team_backend.id,
        period_type="monthly",
        budget_amount_usd=Decimal("2000.00"),
        enforcement_mode="hard",
    )
    data["budget_team_backend"] = budget_team_backend

    budget_team_ml = await create_budget_config(
        session,
        org1.id,
        "team",
        team_ml.id,
        period_type="monthly",
        budget_amount_usd=Decimal("1500.00"),
        enforcement_mode="hard",
    )
    data["budget_team_ml"] = budget_team_ml

    # User level budgets
    budget_user_alice = await create_budget_config(
        session,
        org1.id,
        "user",
        user_alice.id,
        period_type="monthly",
        budget_amount_usd=Decimal("500.00"),
        enforcement_mode="hard",
    )
    data["budget_user_alice"] = budget_user_alice

    budget_user_bob = await create_budget_config(
        session,
        org1.id,
        "user",
        user_bob.id,
        period_type="monthly",
        budget_amount_usd=Decimal("300.00"),
        enforcement_mode="soft",  # Soft limit for testing warnings
    )
    data["budget_user_bob"] = budget_user_bob

    # Create some usage records
    now = datetime.now(UTC)
    period_start = datetime(now.year, now.month, 1, tzinfo=UTC)

    # Alice has used some budget
    usage_alice = await create_budget_usage(
        session,
        org1.id,
        "user",
        user_alice.id,
        period_start=period_start,
        period_type="monthly",
        total_cost_usd=Decimal("150.00"),
        total_tokens=50000,
        request_count=100,
    )
    data["usage_alice"] = usage_alice

    # Bob has exceeded soft limit
    usage_bob = await create_budget_usage(
        session,
        org1.id,
        "user",
        user_bob.id,
        period_start=period_start,
        period_type="monthly",
        total_cost_usd=Decimal("350.00"),  # Over $300 soft limit
        total_tokens=116000,
        request_count=200,
    )
    data["usage_bob"] = usage_bob

    # Team usage aggregates
    usage_team_backend = await create_budget_usage(
        session,
        org1.id,
        "team",
        team_backend.id,
        period_start=period_start,
        period_type="monthly",
        total_cost_usd=Decimal("500.00"),
        total_tokens=166000,
        request_count=350,
    )
    data["usage_team_backend"] = usage_team_backend

    usage_team_ml = await create_budget_usage(
        session,
        org1.id,
        "team",
        team_ml.id,
        period_start=period_start,
        period_type="monthly",
        total_cost_usd=Decimal("1200.00"),
        total_tokens=400000,
        request_count=800,
    )
    data["usage_team_ml"] = usage_team_ml

    await session.commit()
    return data


async def seed_rate_limit_data(session: AsyncSession, base_data: dict | None = None) -> dict:
    """
    Seed rate limit configurations for testing.

    Args:
        session: Database session
        base_data: Optional base data from seed_test_database

    Returns:
        Dictionary containing rate limit entities
    """
    data = {}

    # Use provided base data or create minimal set
    if base_data:
        org1 = base_data["org1"]
        dept_eng = base_data["dept_eng"]
        team_backend = base_data["team_backend"]
        user_alice = base_data["user_alice"]
        sa_cicd = base_data.get("sa_cicd")
    else:
        org1 = await create_org(session, id="org-ratelimit-test")
        dept_eng = await create_department(session, org1.id, id="dept-eng-rl")
        team_backend = await create_team(session, org1.id, dept_eng.id, id="team-backend-rl")
        user_alice = await create_user(session, org1.id, team_backend.id, id="user-alice-rl")
        sa_cicd = await create_service_account(session, org1.id, dept_eng.id, team_backend.id, id="sa-cicd-rl")

        data["org1"] = org1
        data["dept_eng"] = dept_eng
        data["team_backend"] = team_backend
        data["user_alice"] = user_alice
        data["sa_cicd"] = sa_cicd

    # Organization level rate limits
    rl_org = await create_rate_limit_config(
        session,
        org1.id,
        "org",
        org1.id,
        rpm=1000,
        tpm=1000000,
        concurrent_requests=100,
    )
    data["rl_org"] = rl_org

    # Department level rate limits
    rl_dept_eng = await create_rate_limit_config(
        session,
        org1.id,
        "department",
        dept_eng.id,
        rpm=500,
        tpm=500000,
        concurrent_requests=50,
    )
    data["rl_dept_eng"] = rl_dept_eng

    # Team level rate limits
    rl_team_backend = await create_rate_limit_config(
        session,
        org1.id,
        "team",
        team_backend.id,
        rpm=200,
        tpm=200000,
        concurrent_requests=20,
    )
    data["rl_team_backend"] = rl_team_backend

    # User level rate limits
    rl_user_alice = await create_rate_limit_config(
        session,
        org1.id,
        "user",
        user_alice.id,
        rpm=60,
        tpm=60000,
        concurrent_requests=5,
    )
    data["rl_user_alice"] = rl_user_alice

    # Service account with higher limits
    if sa_cicd:
        rl_sa_cicd = await create_rate_limit_config(
            session,
            org1.id,
            "service_account",
            sa_cicd.id,
            rpm=300,  # Higher than user
            tpm=300000,
            concurrent_requests=30,
        )
        data["rl_sa_cicd"] = rl_sa_cicd

    await session.commit()
    return data


async def seed_pool_accounts(session: AsyncSession, num_accounts: int = 3) -> list[BedrockPoolAccount]:
    """
    Seed Bedrock pool accounts for testing.

    Args:
        session: Database session
        num_accounts: Number of pool accounts to create

    Returns:
        List of created pool accounts
    """
    accounts = []

    for i in range(num_accounts):
        account_id = f"11111111111{i}"
        account = await create_pool_account(
            session,
            id=f"pool-{i}",
            account_id=account_id,
            role_arn=f"arn:aws:iam::{account_id}:role/BedrockPoolRole",
            region="us-east-1",
            is_healthy=True,
        )
        accounts.append(account)

    await session.commit()
    return accounts


async def seed_model_pricing(session: AsyncSession) -> list[ModelPricing]:
    """
    Seed model pricing data for cost calculations.

    Args:
        session: Database session

    Returns:
        List of created model pricing entries
    """
    models = [
        # Claude 3.5 Sonnet
        {
            "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "input_price_per_1k": Decimal("0.003"),
            "output_price_per_1k": Decimal("0.015"),
        },
        # Claude 3 Sonnet
        {
            "model_id": "anthropic.claude-3-sonnet-20240229-v1:0",
            "input_price_per_1k": Decimal("0.003"),
            "output_price_per_1k": Decimal("0.015"),
        },
        # Claude 3 Haiku
        {
            "model_id": "anthropic.claude-3-haiku-20240307-v1:0",
            "input_price_per_1k": Decimal("0.00025"),
            "output_price_per_1k": Decimal("0.00125"),
        },
        # Claude 3 Opus
        {
            "model_id": "anthropic.claude-3-opus-20240229-v1:0",
            "input_price_per_1k": Decimal("0.015"),
            "output_price_per_1k": Decimal("0.075"),
        },
        # Amazon Titan Text
        {
            "model_id": "amazon.titan-text-express-v1",
            "input_price_per_1k": Decimal("0.0002"),
            "output_price_per_1k": Decimal("0.0006"),
        },
    ]

    pricing_entries = []
    for model in models:
        pricing = await create_model_pricing(
            session,
            model["model_id"],
            input_price_per_1k=model["input_price_per_1k"],
            output_price_per_1k=model["output_price_per_1k"],
        )
        pricing_entries.append(pricing)

    await session.commit()
    return pricing_entries


async def seed_usage_logs(
    session: AsyncSession,
    base_data: dict,
    num_logs: int = 50,
) -> list[UsageLog]:
    """
    Seed usage log entries for testing.

    Args:
        session: Database session
        base_data: Base data from seed_test_database
        num_logs: Number of log entries to create

    Returns:
        List of created usage log entries
    """
    import random

    logs = []
    models = [
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "anthropic.claude-3-haiku-20240307-v1:0",
    ]

    users = [
        (base_data["user_alice"], base_data["team_backend"], base_data["dept_eng"], "human"),
        (base_data["user_bob"], base_data["team_ml"], base_data["dept_ds"], "human"),
    ]

    if "sa_cicd" in base_data:
        users.append((base_data["sa_cicd"], base_data["team_backend"], base_data["dept_eng"], "service"))

    for _ in range(num_logs):
        user, team, dept, account_type = random.choice(users)
        model = random.choice(models)

        log = await create_usage_log(
            session,
            base_data["org1"].id,
            dept.id,
            team.id,
            user.id,
            account_type=account_type,
            model=model,
            input_tokens=random.randint(50, 500),
            output_tokens=random.randint(100, 1000),
            cost_usd=Decimal(str(round(random.uniform(0.001, 0.1), 6))),
            latency_ms=random.randint(100, 2000),
            status_code=random.choices([200, 200, 200, 429, 500], weights=[0.9, 0.03, 0.03, 0.02, 0.02])[0],
        )
        logs.append(log)

    await session.commit()
    return logs


async def clear_test_data(session: AsyncSession) -> None:
    """
    Clear all test data from the database.

    This function removes all data from test tables in the correct order
    to respect foreign key constraints.

    Args:
        session: Database session
    """
    # Delete in order of dependencies (leaf tables first)
    await session.execute(delete(UsageLog))
    await session.execute(delete(BudgetUsage))
    await session.execute(delete(BudgetConfig))
    await session.execute(delete(RateLimitConfig))
    await session.execute(delete(Token))
    await session.execute(delete(ServiceAccount))
    await session.execute(delete(User))
    await session.execute(delete(Team))
    await session.execute(delete(Department))
    await session.execute(delete(BedrockPoolAccount))
    await session.execute(delete(ModelPricing))
    await session.execute(delete(Organization))

    await session.commit()


async def reset_database(session: AsyncSession) -> None:
    """
    Reset database to a clean state with fresh test data.

    Args:
        session: Database session
    """
    await clear_test_data(session)


async def create_full_test_environment(session: AsyncSession) -> dict:
    """
    Create a complete test environment with all necessary data.

    This is a convenience function that calls all seed functions
    to set up a full test environment.

    Args:
        session: Database session

    Returns:
        Dictionary containing all created entities
    """
    data = {}

    # Seed base data
    base_data = await seed_test_database(session)
    data.update(base_data)

    # Seed budget data
    budget_data = await seed_budget_data(session, base_data)
    data.update(budget_data)

    # Seed rate limit data
    rate_limit_data = await seed_rate_limit_data(session, base_data)
    data.update(rate_limit_data)

    # Seed pool accounts
    pool_accounts = await seed_pool_accounts(session)
    data["pool_accounts"] = pool_accounts

    # Seed model pricing
    model_pricing = await seed_model_pricing(session)
    data["model_pricing"] = model_pricing

    # Seed usage logs
    usage_logs = await seed_usage_logs(session, base_data, num_logs=20)
    data["usage_logs"] = usage_logs

    return data
