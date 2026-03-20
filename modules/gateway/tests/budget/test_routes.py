from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.budget.routes import get_budget_service, get_current_user
from src.budget.routes import router as budget_router
from src.budget.service import BudgetService
from src.shared.schemas.budget import (
    BudgetResponse,
    BudgetStatusResponse,
    CostCalculationResponse,
    EnforcementMode,
    EntityType,
    PeriodType,
)


@pytest.fixture
def mock_budget_service():
    """Create a mock BudgetService with async methods."""
    mock = MagicMock(spec=BudgetService)
    # Make async methods return AsyncMock
    mock.create_budget = AsyncMock()
    mock.get_budget = AsyncMock()
    mock.get_budgets_for_entity = AsyncMock()
    mock.update_budget = AsyncMock()
    mock.delete_budget = AsyncMock()
    mock.get_budget_status = AsyncMock()
    mock.get_budget_usage = AsyncMock()
    mock.calculate_cost = AsyncMock()
    mock.record_cost = AsyncMock()
    mock.get_budget_summary = AsyncMock()
    mock.get_organization_budget_overview = AsyncMock()
    mock.get_budget_alerts = AsyncMock()
    return mock


@pytest.fixture
def mock_user_context():
    """Create a mock user context."""
    return MagicMock(
        user_id="user-123",
        org_id="org-123",
        team_id="team-123",
        department_id="dept-123",
        account_type="human",
        is_admin=False,
    )


@pytest.fixture
def app(mock_budget_service, mock_user_context):
    """Create a test FastAPI app with dependency overrides."""
    app = FastAPI()
    app.include_router(budget_router)

    # Override dependencies
    app.dependency_overrides[get_budget_service] = lambda: mock_budget_service
    app.dependency_overrides[get_current_user] = lambda: mock_user_context

    return app


@pytest.fixture
def sample_budget_response():
    """Create a sample budget response."""
    return BudgetResponse(
        id="budget-123",
        entity_type=EntityType.USER,
        entity_id="user-123",
        period_type=PeriodType.MONTHLY,
        budget_amount_usd=Decimal("100.00"),
        enforcement_mode=EnforcementMode.HARD,
        org_id="org-123",
        updated_at="2024-02-12T18:00:00Z",
    )


class TestBudgetRoutes:
    """Test suite for budget routes."""

    @pytest.mark.asyncio
    async def test_create_budget_success(self, app, mock_budget_service, sample_budget_response):
        """Test successful budget creation via API."""
        mock_budget_service.create_budget.return_value = sample_budget_response

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/budgets/",
                json={
                    "entity_type": "user",
                    "entity_id": "user-123",
                    "period_type": "monthly",
                    "budget_amount_usd": "100.00",
                    "enforcement_mode": "hard",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "budget-123"
        assert data["entity_type"] == "user"
        assert Decimal(str(data["budget_amount_usd"])) == Decimal("100.00")

    @pytest.mark.asyncio
    async def test_create_budget_validation_error(self, app, mock_budget_service):
        """Test budget creation with validation error."""
        from src.shared.exceptions import ValidationError

        mock_budget_service.create_budget.side_effect = ValidationError("Budget already exists")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/budgets/",
                json={
                    "entity_type": "user",
                    "entity_id": "user-123",
                    "period_type": "monthly",
                    "budget_amount_usd": "100.00",
                    "enforcement_mode": "hard",
                },
            )

        assert response.status_code == 400
        assert "Budget already exists" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_get_budget_success(self, app, mock_budget_service, sample_budget_response):
        """Test successful budget retrieval via API."""
        mock_budget_service.get_budget.return_value = sample_budget_response

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/budgets/budget-123")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "budget-123"
        assert data["entity_type"] == "user"

    @pytest.mark.asyncio
    async def test_get_budget_not_found(self, app, mock_budget_service):
        """Test budget retrieval when budget doesn't exist."""
        mock_budget_service.get_budget.return_value = None

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/budgets/non-existent")

        assert response.status_code == 404
        assert "Budget not found" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_get_budgets_for_entity(self, app, mock_budget_service, sample_budget_response):
        """Test retrieving all budgets for an entity."""
        mock_budget_service.get_budgets_for_entity.return_value = [sample_budget_response]

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/budgets/entity/user/user-123")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == "budget-123"

    @pytest.mark.asyncio
    async def test_update_budget_success(self, app, mock_budget_service, sample_budget_response):
        """Test successful budget update via API."""
        updated_budget = sample_budget_response.model_copy()
        updated_budget.budget_amount_usd = Decimal("200.00")
        mock_budget_service.update_budget.return_value = updated_budget

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put(
                "/budgets/budget-123",
                json={
                    "budget_amount_usd": "200.00",
                    "enforcement_mode": "soft",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert Decimal(str(data["budget_amount_usd"])) == Decimal("200.00")

    @pytest.mark.asyncio
    async def test_update_budget_not_found(self, app, mock_budget_service):
        """Test budget update when budget doesn't exist."""
        mock_budget_service.update_budget.return_value = None

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put("/budgets/non-existent", json={"budget_amount_usd": "200.00"})

        assert response.status_code == 404
        assert "Budget not found" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_delete_budget_success(self, app, mock_budget_service):
        """Test successful budget deletion via API."""
        mock_budget_service.delete_budget.return_value = True

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete("/budgets/budget-123")

        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_budget_not_found(self, app, mock_budget_service):
        """Test budget deletion when budget doesn't exist."""
        mock_budget_service.delete_budget.return_value = False

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete("/budgets/non-existent")

        assert response.status_code == 404
        assert "Budget not found" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_get_budget_status(self, app, mock_budget_service):
        """Test getting budget status via API."""
        status_response = BudgetStatusResponse(
            budget_amount_usd=Decimal("100.00"),
            current_spend_usd=Decimal("75.00"),
            remaining_budget_usd=Decimal("25.00"),
            budget_utilization_percent=75.0,
            period_start="2024-02-01",
            period_end="2024-02-29",
            period_type=PeriodType.MONTHLY,
            enforcement_mode=EnforcementMode.HARD,
            budget_exceeded=False,
            warnings=[],
        )
        mock_budget_service.get_budget_status.return_value = status_response

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/budgets/status/user/user-123?period_type=monthly")

        assert response.status_code == 200
        data = response.json()
        assert Decimal(str(data["budget_amount_usd"])) == Decimal("100.00")
        assert Decimal(str(data["current_spend_usd"])) == Decimal("75.00")
        assert data["budget_utilization_percent"] == 75.0

    @pytest.mark.asyncio
    async def test_get_budget_status_not_found(self, app, mock_budget_service):
        """Test getting budget status when budget doesn't exist."""
        mock_budget_service.get_budget_status.return_value = None

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/budgets/status/user/user-123?period_type=monthly")

        assert response.status_code == 404
        assert "Budget not found" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_calculate_cost(self, app, mock_budget_service):
        """Test cost calculation via API."""
        cost_response = CostCalculationResponse(
            model_name="claude-3-5-sonnet-20241022",
            tokens_in=1000,
            tokens_out=500,
            cost_usd=Decimal("0.0105"),
            input_cost_per_1k_tokens=Decimal("0.003"),
            output_cost_per_1k_tokens=Decimal("0.015"),
        )
        mock_budget_service.calculate_cost.return_value = cost_response

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/budgets/calculate-cost",
                json={
                    "model_name": "claude-3-5-sonnet-20241022",
                    "tokens_in": 1000,
                    "tokens_out": 500,
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["model_name"] == "claude-3-5-sonnet-20241022"
        assert Decimal(str(data["cost_usd"])) == Decimal("0.0105")

    @pytest.mark.asyncio
    async def test_record_cost(self, app, mock_budget_service):
        """Test cost recording via API."""
        mock_budget_service.record_cost.return_value = None

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/budgets/record-cost",
                json={
                    "entity_type": "user",
                    "entity_id": "user-123",
                    "model_name": "claude-3-5-sonnet-20241022",
                    "tokens_in": 1000,
                    "tokens_out": 500,
                    "request_cost_usd": "10.50",
                },
            )

        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_get_budget_summary(self, app, mock_budget_service):
        """Test getting budget summary via API."""
        summary = {
            "entity_type": "user",
            "entity_id": "user-123",
            "budgets": [
                {
                    "id": "budget-123",
                    "period_type": "monthly",
                    "budget_amount_usd": 100.0,
                    "utilization_percent": 75.0,
                }
            ],
        }
        mock_budget_service.get_budget_summary.return_value = summary

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/budgets/summary/user/user-123")

        assert response.status_code == 200
        data = response.json()
        assert data["entity_type"] == "user"
        assert len(data["budgets"]) == 1

    @pytest.mark.asyncio
    async def test_get_organization_budget_overview(self, app, mock_budget_service):
        """Test getting organization budget overview via API."""
        overview = {
            "org_id": "org-123",
            "total_budgets": 5,
            "total_spend_current_month": 1250.0,
            "entities": {
                "organization": [{"entity_id": "org-123", "budget_amount_usd": 5000.0}],
                "department": [{"entity_id": "dept-123", "budget_amount_usd": 2000.0}],
            },
        }
        mock_budget_service.get_organization_budget_overview.return_value = overview

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/budgets/organization/overview")

        assert response.status_code == 200
        data = response.json()
        assert data["org_id"] == "org-123"
        assert data["total_budgets"] == 5

    @pytest.mark.asyncio
    async def test_get_budget_alerts(self, app, mock_budget_service):
        """Test getting budget alerts via API."""
        alerts = [
            {
                "entity_type": "user",
                "entity_id": "user-123",
                "utilization_percent": 90.0,
                "alert_level": "warning",
                "budget_amount_usd": 100.0,
                "current_spend_usd": 90.0,
            }
        ]
        mock_budget_service.get_budget_alerts.return_value = alerts

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/budgets/organization/alerts?threshold_percent=85.0")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["alert_level"] == "warning"
        assert data[0]["utilization_percent"] == 90.0

    @pytest.mark.asyncio
    async def test_invalid_entity_type(self, app, mock_budget_service):
        """Test API with invalid entity type."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/budgets/entity/invalid_type/user-123")

        assert response.status_code == 422  # Validation error

    @pytest.mark.asyncio
    async def test_invalid_period_type(self, app, mock_budget_service):
        """Test API with invalid period type."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/budgets/status/user/user-123?period_type=invalid_period")

        assert response.status_code == 422  # Validation error
