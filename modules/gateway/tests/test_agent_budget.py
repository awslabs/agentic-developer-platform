"""
Tests for Per-Agent Budget Assignment and Usage Dashboard.

Issue #249: Epic 11 - US 2 - Unit 2

Tests cover:
1. Agent budget config creation during agent registration
2. Budget config validation on agent creation
3. Cross-database consistency (Postgres first, DynamoDB second with rollback)
4. Agent-level budget enforcement (most specific wins)
5. Fallback to team/org budget when no agent budget
6. Agent usage endpoint aggregation
"""

import inspect
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.admin.agent_registry_schemas import (
    AgentBudgetStatus,
    AgentRegistryCreateRequest,
    AgentUsageByModel,
    AgentUsageResponse,
)
from src.shared.schemas.budget import (
    EnforcementMode,
    EntityType,
)

# ==============================================================================
# Test: EntityType enum includes AGENT
# ==============================================================================


def test_entity_type_has_agent():
    """Verify EntityType enum includes AGENT value."""
    assert EntityType.AGENT == "agent"
    assert "agent" in [e.value for e in EntityType]


# ==============================================================================
# Test: AgentRegistryCreateRequest has budget_monthly_usd field
# ==============================================================================


def test_agent_create_request_has_budget_field():
    """Verify AgentRegistryCreateRequest includes budget_monthly_usd field."""
    request = AgentRegistryCreateRequest(
        agent_name="test-agent",
        role_arn="arn:aws:iam::123456789012:role/test-role",
        org_id="org-123",
        owner="user-123",
        budget_monthly_usd=Decimal("50.00"),
    )
    assert request.budget_monthly_usd == Decimal("50.00")


def test_agent_create_request_budget_field_optional():
    """Verify budget_monthly_usd is optional."""
    request = AgentRegistryCreateRequest(
        agent_name="test-agent",
        role_arn="arn:aws:iam::123456789012:role/test-role",
        org_id="org-123",
        owner="user-123",
    )
    assert request.budget_monthly_usd is None


def test_agent_create_request_budget_must_be_positive():
    """Verify budget_monthly_usd must be positive."""
    with pytest.raises(ValueError):
        AgentRegistryCreateRequest(
            agent_name="test-agent",
            role_arn="arn:aws:iam::123456789012:role/test-role",
            org_id="org-123",
            owner="user-123",
            budget_monthly_usd=Decimal("-10.00"),
        )


# ==============================================================================
# Test: Agent Usage Response Schemas
# ==============================================================================


def test_agent_usage_response_schema():
    """Test AgentUsageResponse schema structure."""
    response = AgentUsageResponse(
        agent_id="agent-123",
        agent_name="test-agent",
        period="monthly",
        total_requests=100,
        total_input_tokens=50000,
        total_output_tokens=20000,
        total_cost_usd=Decimal("12.50"),
        by_model=[
            AgentUsageByModel(
                model_id="claude-sonnet",
                requests=80,
                input_tokens=40000,
                output_tokens=16000,
                cost_usd=Decimal("10.00"),
            ),
            AgentUsageByModel(
                model_id="claude-haiku",
                requests=20,
                input_tokens=10000,
                output_tokens=4000,
                cost_usd=Decimal("2.50"),
            ),
        ],
        budget=AgentBudgetStatus(
            monthly_limit_usd=Decimal("50.00"),
            used_usd=Decimal("12.50"),
            remaining_usd=Decimal("37.50"),
            utilization_pct=25.0,
        ),
    )

    assert response.agent_id == "agent-123"
    assert response.total_requests == 100
    assert response.total_cost_usd == Decimal("12.50")
    assert len(response.by_model) == 2
    assert response.budget.utilization_pct == 25.0


def test_agent_usage_response_without_budget():
    """Test AgentUsageResponse when no budget is configured."""
    response = AgentUsageResponse(
        agent_id="agent-123",
        agent_name="test-agent",
        period="monthly",
        total_requests=0,
        total_input_tokens=0,
        total_output_tokens=0,
        total_cost_usd=Decimal("0"),
        by_model=[],
        budget=None,
    )

    assert response.budget is None


# ==============================================================================
# Test: Budget Helper Service
# ==============================================================================


@pytest.mark.asyncio
async def test_budget_helper_create_agent_budget_config():
    """Test creating a budget config for an agent."""
    from src.admin.budget_helper import BudgetHelperService

    # Mock the database session
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    service = BudgetHelperService(db_session=mock_session)

    budget_config_id = await service.create_agent_budget_config(
        agent_id="agent-123",
        org_id="org-123",
        monthly_limit_usd=Decimal("50.00"),
        enforcement_mode="hard",
    )

    assert budget_config_id is not None
    assert isinstance(budget_config_id, str)
    mock_session.add.assert_called_once()
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_budget_helper_validate_budget_config_exists():
    """Test validating budget config existence with org_id."""
    from src.admin.budget_helper import BudgetHelperService

    # Mock the database session
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = "existing-config-id"
    mock_session.execute = AsyncMock(return_value=mock_result)

    service = BudgetHelperService(db_session=mock_session)

    exists = await service.validate_budget_config_exists("existing-config-id", "org-123")
    assert exists is True


@pytest.mark.asyncio
async def test_budget_helper_validate_budget_config_not_exists():
    """Test validating budget config that doesn't exist or wrong org."""
    from src.admin.budget_helper import BudgetHelperService

    # Mock the database session
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    service = BudgetHelperService(db_session=mock_session)

    exists = await service.validate_budget_config_exists("nonexistent-config-id", "org-123")
    assert exists is False


@pytest.mark.asyncio
async def test_budget_helper_delete_budget_config():
    """Test deleting a budget config with org_id for tenant isolation."""
    from src.admin.budget_helper import BudgetHelperService

    # Mock the database session
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.rowcount = 1
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    service = BudgetHelperService(db_session=mock_session)

    deleted = await service.delete_budget_config("config-to-delete", "org-123")
    assert deleted is True
    mock_session.commit.assert_called_once()


# ==============================================================================
# Test: Budget Enforcement Service - Agent Budget Check
# ==============================================================================


@pytest.mark.asyncio
async def test_check_agent_budget_allowed():
    """Test agent budget check when within limits."""
    from src.budget.enforcement_service import BudgetEnforcementService
    from src.shared.models.budget import BudgetConfig, BudgetUsage

    # Create mock budget config
    mock_budget = MagicMock(spec=BudgetConfig)
    mock_budget.id = "budget-123"
    mock_budget.org_id = "org-123"
    mock_budget.entity_type = "agent"
    mock_budget.entity_id = "agent-123"
    mock_budget.period_type = "monthly"
    mock_budget.budget_amount_usd = Decimal("50.00")
    mock_budget.enforcement_mode = "hard"

    # Create mock usage (within limits)
    mock_usage = MagicMock(spec=BudgetUsage)
    mock_usage.total_cost_usd = Decimal("10.00")

    # Mock session
    mock_session = AsyncMock()

    # First execute returns budget config
    mock_budget_result = MagicMock()
    mock_budget_result.scalar_one_or_none.return_value = mock_budget

    # Second execute returns usage
    mock_usage_result = MagicMock()
    mock_usage_result.scalar_one_or_none.return_value = mock_usage

    mock_session.execute = AsyncMock(side_effect=[mock_budget_result, mock_usage_result])

    service = BudgetEnforcementService(db_session=mock_session)

    # Patch budget_config to enable budget check
    with patch("src.budget.enforcement_service.budget_config") as mock_config:
        mock_config.budget_check_enabled = True
        mock_config.budget_warning_threshold_percent = 80
        mock_config.budget_critical_threshold_percent = 90

        result = await service.check_agent_budget("budget-123", Decimal("0.05"))

    assert result.allowed is True
    assert len(result.warnings) == 0


@pytest.mark.asyncio
async def test_check_agent_budget_exceeded_hard():
    """Test agent budget check when hard limit exceeded."""
    from src.budget.enforcement_service import BudgetEnforcementService
    from src.shared.models.budget import BudgetConfig, BudgetUsage

    # Create mock budget config
    mock_budget = MagicMock(spec=BudgetConfig)
    mock_budget.id = "budget-123"
    mock_budget.org_id = "org-123"
    mock_budget.entity_type = "agent"
    mock_budget.entity_id = "agent-123"
    mock_budget.period_type = "monthly"
    mock_budget.budget_amount_usd = Decimal("50.00")
    mock_budget.enforcement_mode = "hard"

    # Create mock usage (at limit)
    mock_usage = MagicMock(spec=BudgetUsage)
    mock_usage.total_cost_usd = Decimal("50.00")

    # Mock session
    mock_session = AsyncMock()

    mock_budget_result = MagicMock()
    mock_budget_result.scalar_one_or_none.return_value = mock_budget

    mock_usage_result = MagicMock()
    mock_usage_result.scalar_one_or_none.return_value = mock_usage

    mock_session.execute = AsyncMock(side_effect=[mock_budget_result, mock_usage_result])

    service = BudgetEnforcementService(db_session=mock_session)

    with patch("src.budget.enforcement_service.budget_config") as mock_config:
        mock_config.budget_check_enabled = True
        mock_config.budget_warning_threshold_percent = 80
        mock_config.budget_critical_threshold_percent = 90

        result = await service.check_agent_budget("budget-123", Decimal("0.05"))

    assert result.allowed is False
    assert result.exceeded_entity_type == EntityType.AGENT
    assert result.exceeded_entity_id == "agent-123"
    assert result.enforcement_mode == EnforcementMode.HARD


@pytest.mark.asyncio
async def test_check_agent_budget_exceeded_soft():
    """Test agent budget check when soft limit exceeded (allows with warning)."""
    from src.budget.enforcement_service import BudgetEnforcementService
    from src.shared.models.budget import BudgetConfig, BudgetUsage

    # Create mock budget config with SOFT enforcement
    mock_budget = MagicMock(spec=BudgetConfig)
    mock_budget.id = "budget-123"
    mock_budget.org_id = "org-123"
    mock_budget.entity_type = "agent"
    mock_budget.entity_id = "agent-123"
    mock_budget.period_type = "monthly"
    mock_budget.budget_amount_usd = Decimal("50.00")
    mock_budget.enforcement_mode = "soft"

    # Create mock usage (at limit)
    mock_usage = MagicMock(spec=BudgetUsage)
    mock_usage.total_cost_usd = Decimal("50.00")

    # Mock session
    mock_session = AsyncMock()

    mock_budget_result = MagicMock()
    mock_budget_result.scalar_one_or_none.return_value = mock_budget

    mock_usage_result = MagicMock()
    mock_usage_result.scalar_one_or_none.return_value = mock_usage

    mock_session.execute = AsyncMock(side_effect=[mock_budget_result, mock_usage_result])

    service = BudgetEnforcementService(db_session=mock_session)

    with patch("src.budget.enforcement_service.budget_config") as mock_config:
        mock_config.budget_check_enabled = True
        mock_config.budget_warning_threshold_percent = 80
        mock_config.budget_critical_threshold_percent = 90

        result = await service.check_agent_budget("budget-123", Decimal("0.05"))

    # Soft limit - should allow but with warnings
    assert result.allowed is True
    assert len(result.warnings) > 0


@pytest.mark.asyncio
async def test_check_agent_budget_no_config():
    """Test agent budget check when budget config doesn't exist."""
    from src.budget.enforcement_service import BudgetEnforcementService

    # Mock session
    mock_session = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None  # No budget config found

    mock_session.execute = AsyncMock(return_value=mock_result)

    service = BudgetEnforcementService(db_session=mock_session)

    with patch("src.budget.enforcement_service.budget_config") as mock_config:
        mock_config.budget_check_enabled = True

        result = await service.check_agent_budget("nonexistent-budget", Decimal("0.05"))

    # No budget = allow
    assert result.allowed is True


@pytest.mark.asyncio
async def test_check_agent_budget_disabled():
    """Test agent budget check when budget checking is disabled."""
    from src.budget.enforcement_service import BudgetEnforcementService

    service = BudgetEnforcementService()

    with patch("src.budget.enforcement_service.budget_config") as mock_config:
        mock_config.budget_check_enabled = False

        result = await service.check_agent_budget("any-budget-id", Decimal("100.00"))

    assert result.allowed is True


# ==============================================================================
# Test: Budget Enforcement Middleware - Agent Header Handling
# ==============================================================================


def test_middleware_does_not_read_agent_budget_config_header():
    """Issue #3985: X-Agent-BudgetConfigId is no longer trusted.

    Replaces test_middleware_get_agent_budget_config_id_{with,without}_trust,
    which asserted the header WAS read when BG_TRUST_APIGW_HEADERS=true. The
    header was trusted on presence alone, so a caller could name any budget
    config — including a fresh, unspent one — and bypass its own agent budget.
    The org/team hierarchy check (derived from the authenticated token_context)
    is unaffected.

    Re-adding per-agent enforcement must resolve the config id server-side from
    the agent registry entry, keyed off the authenticated identity.
    """
    from src.budget.enforcement_middleware import BudgetEnforcementMiddleware

    assert not hasattr(BudgetEnforcementMiddleware, "_get_agent_budget_config_id")

    # Strip comments before scanning: the removal is documented in a comment that
    # names the header, and the point of this assertion is that no *code* reads it.
    code_lines = [line.split("#", 1)[0] for line in inspect.getsource(BudgetEnforcementMiddleware).splitlines()]
    code = "\n".join(code_lines).lower()
    assert "budgetconfigid" not in code, "BudgetEnforcementMiddleware must not read the X-Agent-BudgetConfigId header"


# ==============================================================================
# Test: Lambda Handler - Agent Usage Attribution
# ==============================================================================


def test_lambda_parse_chat_log_with_agent_fields():
    """Test Lambda parser includes agent fields."""
    import importlib

    # Skip if psycopg2 not available (Lambda dependency)
    pytest.importorskip("psycopg2")

    # ``tests.lambda`` can't be a dotted import (lambda is a keyword), so reach
    # the loader via importlib. It loads the handler under a unique module name,
    # avoiding the ``handler`` collision with other lambdas in the same run.
    load_handler = importlib.import_module("tests.lambda._handler_loader").load_handler
    parse_chat_log = load_handler("budget-usage-tracker").parse_chat_log

    chat_log = {
        "org_id": "org-123",
        "user_id": "agent-user",
        "model": "claude-sonnet",
        "response": {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
            }
        },
        "timestamp": "2026-02-01T12:00:00Z",
        "account_type": "service",
        "agent_id": "agent-uuid-123",
        "budget_config_id": "budget-config-123",
    }

    result = parse_chat_log(chat_log)

    assert result is not None
    assert result["account_type"] == "service"
    assert result["agent_id"] == "agent-uuid-123"
    assert result["budget_config_id"] == "budget-config-123"


def test_lambda_parse_chat_log_without_agent_fields():
    """Test Lambda parser handles missing agent fields gracefully."""
    import importlib

    pytest.importorskip("psycopg2")

    load_handler = importlib.import_module("tests.lambda._handler_loader").load_handler
    parse_chat_log = load_handler("budget-usage-tracker").parse_chat_log

    chat_log = {
        "org_id": "org-123",
        "user_id": "human-user",
        "model": "claude-sonnet",
        "response": {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
            }
        },
        "timestamp": "2026-02-01T12:00:00Z",
    }

    result = parse_chat_log(chat_log)

    assert result is not None
    assert result.get("account_type") is None
    assert result.get("agent_id") is None


# ==============================================================================
# Test: Cross-Database Consistency (Postgres first, DynamoDB second)
# ==============================================================================


@pytest.mark.asyncio
async def test_agent_creation_with_budget_auto_creation():
    """Test agent creation with automatic budget config creation."""
    from src.admin.agent_registry_schemas import AgentRegistryCreateRequest
    from src.admin.agent_registry_service import AgentRegistryService

    # Mock DynamoDB client
    mock_dynamodb = MagicMock()
    mock_dynamodb.put_item = MagicMock()

    service = AgentRegistryService(dynamodb_client=mock_dynamodb, table_name="test-table")

    # Mock get_agent_by_role to return None (no existing agent)
    with patch.object(service, "get_agent_by_role", new_callable=AsyncMock) as mock_get_by_role:
        mock_get_by_role.return_value = None

        # Mock budget_helper_service (imported inside the method)
        with patch("src.admin.budget_helper.budget_helper_service") as mock_budget_helper:
            mock_budget_helper.create_agent_budget_config = AsyncMock(return_value="auto-created-budget-id")

            # Create request with budget_monthly_usd
            request = AgentRegistryCreateRequest(
                agent_name="test-agent",
                role_arn="arn:aws:iam::123456789012:role/test-role",
                org_id="org-123",
                owner="user-123",
                budget_monthly_usd=Decimal("50.00"),
            )

            result = await service.create_agent(request)

    # Verify budget was auto-created
    mock_budget_helper.create_agent_budget_config.assert_called_once()
    assert result.budget_config_id == "auto-created-budget-id"


@pytest.mark.asyncio
async def test_agent_creation_rollback_on_dynamodb_failure():
    """Test that budget config is rolled back if DynamoDB write fails."""
    from botocore.exceptions import ClientError

    from src.admin.agent_registry_schemas import AgentRegistryCreateRequest
    from src.admin.agent_registry_service import AgentRegistryService

    # Mock DynamoDB client that fails
    mock_dynamodb = MagicMock()
    mock_dynamodb.put_item = MagicMock(side_effect=ClientError({"Error": {"Code": "InternalServerError"}}, "PutItem"))

    service = AgentRegistryService(dynamodb_client=mock_dynamodb, table_name="test-table")

    with patch.object(service, "get_agent_by_role", new_callable=AsyncMock) as mock_get_by_role:
        mock_get_by_role.return_value = None

        with patch("src.admin.budget_helper.budget_helper_service") as mock_budget_helper:
            mock_budget_helper.create_agent_budget_config = AsyncMock(return_value="auto-created-budget-id")
            mock_budget_helper.delete_budget_config = AsyncMock(return_value=True)

            request = AgentRegistryCreateRequest(
                agent_name="test-agent",
                role_arn="arn:aws:iam::123456789012:role/test-role",
                org_id="org-123",
                owner="user-123",
                budget_monthly_usd=Decimal("50.00"),
            )

            with pytest.raises(ClientError):
                await service.create_agent(request)

            # Verify budget was rolled back
            mock_budget_helper.delete_budget_config.assert_called_once_with("auto-created-budget-id", "org-123")


@pytest.mark.asyncio
async def test_agent_creation_validates_existing_budget_config():
    """Test that agent creation validates budget_config_id if provided."""
    from src.admin.agent_registry_schemas import AgentRegistryCreateRequest
    from src.admin.agent_registry_service import AgentRegistryService
    from src.shared.exceptions import ValidationError

    mock_dynamodb = MagicMock()
    service = AgentRegistryService(dynamodb_client=mock_dynamodb, table_name="test-table")

    with patch.object(service, "get_agent_by_role", new_callable=AsyncMock) as mock_get_by_role:
        mock_get_by_role.return_value = None

        with patch("src.admin.budget_helper.budget_helper_service") as mock_budget_helper:
            # Budget config doesn't exist
            mock_budget_helper.validate_budget_config_exists = AsyncMock(return_value=False)

            request = AgentRegistryCreateRequest(
                agent_name="test-agent",
                role_arn="arn:aws:iam::123456789012:role/test-role",
                org_id="org-123",
                owner="user-123",
                budget_config_id="nonexistent-budget-id",
            )

            with pytest.raises(ValidationError):
                await service.create_agent(request)
