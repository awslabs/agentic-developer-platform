"""Unit tests for Usage API routes."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.admin.access_control import AccessControl
from src.auth.dependencies import get_current_user
from src.shared.schemas.auth import TokenContext
from src.usage.config import AggregationInterval
from src.usage.routes import get_access_control, get_usage_service, router
from src.usage.schemas import (
    UsageByModelResponse,
    UsageByOrganizationResponse,
    UsageTimelineEntry,
    UsageTimelineResponse,
)
from src.usage.service import UsageService


@pytest.fixture
def app():
    """Create a test FastAPI app."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def mock_usage_service():
    """Create a mock usage service."""
    service = MagicMock(spec=UsageService)
    return service


@pytest.fixture
def mock_access_control():
    """Create a mock access control."""
    ac = MagicMock(spec=AccessControl)
    ac.check_permission = AsyncMock(return_value=True)
    ac.get_accessible_organizations = AsyncMock(return_value=None)
    return ac


@pytest.fixture
def platform_admin_user():
    """Create a platform admin user context."""
    return TokenContext(
        user_id="admin-user",
        org_id="platform",
        team_id="platform",
        department_id="platform",
        account_type="service",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def org_user():
    """Create an org user context."""
    return TokenContext(
        user_id="org-user",
        org_id="org-001",
        team_id="team-001",
        department_id="dept-001",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def client(app, mock_usage_service, mock_access_control, platform_admin_user):
    """Create a test client with mocked dependencies."""

    async def override_usage_service():
        return mock_usage_service

    async def override_access_control():
        return mock_access_control

    async def override_current_user():
        return platform_admin_user

    app.dependency_overrides[get_usage_service] = override_usage_service
    app.dependency_overrides[get_access_control] = override_access_control
    app.dependency_overrides[get_current_user] = override_current_user

    return TestClient(app)


class TestUsageSummaryEndpoint:
    """Tests for GET /usage/summary."""

    def test_get_usage_summary(self, client, mock_usage_service):
        """Test getting usage summary."""
        mock_usage_service.get_usage_summary = AsyncMock(
            return_value={
                "org_id": "org-001",
                "start_date": datetime.now(UTC).isoformat(),
                "end_date": datetime.now(UTC).isoformat(),
                "total_requests": 100,
                "successful_requests": 95,
                "failed_requests": 5,
                "total_input_tokens": 10000,
                "total_output_tokens": 20000,
                "total_tokens": 30000,
                "total_cost_usd": 1.50,
                "average_latency_ms": 150.0,
                "error_rate_percent": 5.0,
                "unique_users": 10,
                "unique_models": 3,
            }
        )

        response = client.get("/usage/summary?org_id=org-001")

        assert response.status_code == 200
        data = response.json()
        assert data["total_requests"] == 100

    def test_get_usage_summary_with_filters(self, client, mock_usage_service):
        """Test usage summary with date filters."""
        mock_usage_service.get_usage_summary = AsyncMock(
            return_value={
                "org_id": "org-001",
                "total_requests": 50,
                "successful_requests": 48,
                "failed_requests": 2,
                "total_input_tokens": 5000,
                "total_output_tokens": 10000,
                "total_tokens": 15000,
                "total_cost_usd": 0.75,
                "average_latency_ms": 120.0,
                "error_rate_percent": 4.0,
                "unique_users": 5,
                "unique_models": 2,
                "start_date": "2024-01-01T00:00:00Z",
                "end_date": "2024-01-31T23:59:59Z",
            }
        )

        response = client.get("/usage/summary?org_id=org-001&start_date=2024-01-01T00:00:00Z&end_date=2024-01-31T23:59:59Z")

        assert response.status_code == 200


class TestUsageByOrganizationEndpoint:
    """Tests for GET /usage/organizations."""

    def test_get_usage_by_organization(self, client, mock_usage_service):
        """Test getting usage by organization."""
        mock_usage_service.get_usage_by_organization = AsyncMock(
            return_value=[
                UsageByOrganizationResponse(
                    org_id="org-001",
                    org_name="Test Org 1",
                    total_requests=100,
                    total_tokens=30000,
                    total_cost_usd=Decimal("1.50"),
                    average_latency_ms=150.0,
                    error_rate_percent=5.0,
                    period_start=datetime.now(UTC) - timedelta(days=30),
                    period_end=datetime.now(UTC),
                ),
                UsageByOrganizationResponse(
                    org_id="org-002",
                    org_name="Test Org 2",
                    total_requests=50,
                    total_tokens=15000,
                    total_cost_usd=Decimal("0.75"),
                    average_latency_ms=120.0,
                    error_rate_percent=2.0,
                    period_start=datetime.now(UTC) - timedelta(days=30),
                    period_end=datetime.now(UTC),
                ),
            ]
        )

        response = client.get("/usage/organizations")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2


class TestUsageByModelEndpoint:
    """Tests for GET /usage/models."""

    def test_get_usage_by_model(self, client, mock_usage_service):
        """Test getting usage by model."""
        mock_usage_service.get_usage_by_model = AsyncMock(
            return_value=[
                UsageByModelResponse(
                    model="claude-3-sonnet",
                    total_requests=80,
                    total_input_tokens=8000,
                    total_output_tokens=16000,
                    total_tokens=24000,
                    total_cost_usd=Decimal("1.20"),
                    average_latency_ms=150.0,
                    error_rate_percent=3.0,
                ),
                UsageByModelResponse(
                    model="claude-3-opus",
                    total_requests=20,
                    total_input_tokens=2000,
                    total_output_tokens=4000,
                    total_tokens=6000,
                    total_cost_usd=Decimal("0.30"),
                    average_latency_ms=250.0,
                    error_rate_percent=5.0,
                ),
            ]
        )

        response = client.get("/usage/models")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["model"] == "claude-3-sonnet"

    def test_get_usage_by_model_filtered(self, client, mock_usage_service):
        """Test getting usage by model with org filter."""
        mock_usage_service.get_usage_by_model = AsyncMock(return_value=[])

        response = client.get("/usage/models?org_id=org-001")

        assert response.status_code == 200


class TestUsageTimelineEndpoint:
    """Tests for GET /usage/timeline."""

    def test_get_usage_timeline(self, client, mock_usage_service):
        """Test getting usage timeline."""
        now = datetime.now(UTC)
        mock_usage_service.get_usage_timeline = AsyncMock(
            return_value=UsageTimelineResponse(
                org_id="org-001",
                start_date=now - timedelta(days=7),
                end_date=now,
                interval=AggregationInterval.DAILY,
                data=[
                    UsageTimelineEntry(
                        timestamp=now - timedelta(days=1),
                        interval=AggregationInterval.DAILY,
                        total_requests=50,
                        total_tokens=15000,
                        total_cost_usd=Decimal("0.75"),
                        average_latency_ms=150.0,
                        error_count=2,
                    ),
                    UsageTimelineEntry(
                        timestamp=now,
                        interval=AggregationInterval.DAILY,
                        total_requests=60,
                        total_tokens=18000,
                        total_cost_usd=Decimal("0.90"),
                        average_latency_ms=140.0,
                        error_count=3,
                    ),
                ],
            )
        )

        response = client.get("/usage/timeline?org_id=org-001")

        assert response.status_code == 200
        data = response.json()
        assert data["interval"] == "daily"
        assert len(data["data"]) == 2

    def test_get_usage_timeline_hourly(self, client, mock_usage_service):
        """Test getting hourly usage timeline."""
        now = datetime.now(UTC)
        mock_usage_service.get_usage_timeline = AsyncMock(
            return_value=UsageTimelineResponse(
                org_id="org-001",
                start_date=now - timedelta(hours=24),
                end_date=now,
                interval=AggregationInterval.HOURLY,
                data=[],
            )
        )

        response = client.get("/usage/timeline?interval=hourly")

        assert response.status_code == 200


class TestUsageByUserEndpoint:
    """Tests for GET /usage/users."""

    def test_get_usage_by_user(self, client, mock_usage_service):
        """Test getting usage by user."""
        mock_usage_service.get_usage_by_user = AsyncMock(
            return_value=[
                {
                    "user_id": "user-001",
                    "account_type": "human",
                    "total_requests": 50,
                    "total_tokens": 15000,
                    "total_cost_usd": 0.75,
                    "last_request_at": datetime.now(UTC).isoformat(),
                },
                {
                    "user_id": "user-002",
                    "account_type": "human",
                    "total_requests": 30,
                    "total_tokens": 9000,
                    "total_cost_usd": 0.45,
                    "last_request_at": datetime.now(UTC).isoformat(),
                },
            ]
        )

        response = client.get("/usage/users?org_id=org-001")

        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 2


class TestUsageByDepartmentEndpoint:
    """Tests for GET /usage/departments."""

    def test_get_usage_by_department(self, client, mock_usage_service):
        """Test getting usage by department."""
        mock_usage_service.get_usage_by_department = AsyncMock(
            return_value=[
                {
                    "department_id": "dept-001",
                    "total_requests": 80,
                    "total_tokens": 24000,
                    "total_cost_usd": 1.20,
                    "unique_users": 5,
                },
                {
                    "department_id": "dept-002",
                    "total_requests": 20,
                    "total_tokens": 6000,
                    "total_cost_usd": 0.30,
                    "unique_users": 2,
                },
            ]
        )

        response = client.get("/usage/departments?org_id=org-001")

        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 2


class TestUsageLogsEndpoint:
    """Tests for GET /usage/logs."""

    def test_get_usage_logs(self, client, mock_usage_service):
        """Test getting usage logs."""
        mock_usage_service.query_logs = AsyncMock(
            return_value=[
                {
                    "id": "log-001",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "org_id": "org-001",
                    "user_id": "user-001",
                    "model": "claude-3-sonnet",
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "cost_usd": 0.01,
                    "latency_ms": 150,
                    "status_code": 200,
                },
            ]
        )

        response = client.get("/usage/logs?org_id=org-001")

        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1

    def test_get_usage_logs_with_filters(self, client, mock_usage_service):
        """Test getting usage logs with filters."""
        mock_usage_service.query_logs = AsyncMock(return_value=[])

        response = client.get("/usage/logs?org_id=org-001&user_id=user-001&model=claude-3-sonnet&status_code=200")

        assert response.status_code == 200

    def test_get_usage_logs_pagination(self, client, mock_usage_service):
        """Test usage logs pagination."""
        mock_usage_service.query_logs = AsyncMock(return_value=[])

        response = client.get("/usage/logs?org_id=org-001&limit=50&offset=100")

        assert response.status_code == 200


class TestAccessControl:
    """Tests for access control on usage endpoints."""

    def test_org_user_access(self, app, mock_usage_service, mock_access_control, org_user):
        """Test org user can only access their own org."""

        async def override_usage_service():
            return mock_usage_service

        async def override_access_control():
            return mock_access_control

        async def override_current_user():
            return org_user

        app.dependency_overrides[get_usage_service] = override_usage_service
        app.dependency_overrides[get_access_control] = override_access_control
        app.dependency_overrides[get_current_user] = override_current_user

        # Mock accessible orgs to only include user's org
        mock_access_control.get_accessible_organizations = AsyncMock(return_value=["org-001"])

        mock_usage_service.get_usage_summary = AsyncMock(
            return_value={
                "org_id": "org-001",
                "total_requests": 10,
                "successful_requests": 10,
                "failed_requests": 0,
                "total_input_tokens": 1000,
                "total_output_tokens": 2000,
                "total_tokens": 3000,
                "total_cost_usd": 0.15,
                "average_latency_ms": 150.0,
                "error_rate_percent": 0.0,
                "unique_users": 1,
                "unique_models": 1,
                "start_date": "2024-01-01T00:00:00Z",
                "end_date": "2024-01-31T23:59:59Z",
            }
        )

        client = TestClient(app)
        response = client.get("/usage/summary?org_id=org-001")

        assert response.status_code == 200


class TestValidation:
    """Tests for request validation."""

    def test_invalid_pagination_limit(self, client, mock_usage_service):
        """Test invalid pagination limit."""
        response = client.get("/usage/users?org_id=org-001&limit=500")  # Max is 100

        assert response.status_code == 422

    def test_invalid_pagination_offset(self, client, mock_usage_service):
        """Test invalid pagination offset."""
        response = client.get("/usage/logs?org_id=org-001&offset=-1")

        assert response.status_code == 422
