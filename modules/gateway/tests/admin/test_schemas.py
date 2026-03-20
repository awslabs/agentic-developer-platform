"""Unit tests for admin module Pydantic schemas."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.admin.config import AdminRole, Permission
from src.admin.schemas import (
    BudgetConfigResponse,
    BudgetConfigUpdateRequest,
    HealthCheckComponent,
    HealthCheckResponse,
    LogEntryResponse,
    LogQueryRequest,
    LogQueryResponse,
    OrganizationCreateRequest,
    OrganizationListResponse,
    OrganizationResponse,
    OrganizationUpdateRequest,
    OrgDashboardResponse,
    PlatformDashboardResponse,
    PoolAccountCreateRequest,
    PoolAccountResponse,
    PoolAccountStatus,
    PoolStatusResponse,
    RateLimitConfigResponse,
    RateLimitConfigUpdateRequest,
    ReadinessCheckResponse,
    UserRoleAssignRequest,
    UserRoleResponse,
)


class TestOrganizationCreateRequest:
    """Tests for OrganizationCreateRequest schema."""

    def test_valid_minimal(self):
        """Test valid minimal request."""
        request = OrganizationCreateRequest(name="Test Org")

        assert request.name == "Test Org"
        assert request.aws_accounts == []
        assert request.role_mappings == {}
        assert request.settings == {}

    def test_valid_full(self):
        """Test valid request with all fields."""
        request = OrganizationCreateRequest(
            name="Full Org",
            aws_accounts=["123456789012", "234567890123"],
            role_mappings={"admin": "role-id-1"},
            settings={"feature_x": True},
        )

        assert request.name == "Full Org"
        assert len(request.aws_accounts) == 2
        assert request.role_mappings["admin"] == "role-id-1"
        assert request.settings["feature_x"] is True

    def test_invalid_empty_name(self):
        """Test validation fails for empty name."""
        with pytest.raises(ValidationError) as exc_info:
            OrganizationCreateRequest(name="")

        errors = exc_info.value.errors()
        assert any("name" in str(e["loc"]) for e in errors)

    def test_invalid_name_too_long(self):
        """Test validation fails for name exceeding max length."""
        with pytest.raises(ValidationError) as exc_info:
            OrganizationCreateRequest(name="x" * 256)

        errors = exc_info.value.errors()
        assert any("name" in str(e["loc"]) for e in errors)

    def test_name_max_length(self):
        """Test name at max length (255) is valid."""
        request = OrganizationCreateRequest(name="x" * 255)
        assert len(request.name) == 255


class TestOrganizationUpdateRequest:
    """Tests for OrganizationUpdateRequest schema."""

    def test_all_fields_optional(self):
        """Test all fields are optional."""
        request = OrganizationUpdateRequest()

        assert request.name is None
        assert request.aws_accounts is None
        assert request.role_mappings is None
        assert request.settings is None

    def test_partial_update(self):
        """Test partial update with only some fields."""
        request = OrganizationUpdateRequest(name="New Name")

        assert request.name == "New Name"
        assert request.aws_accounts is None

    def test_invalid_empty_name(self):
        """Test validation fails for empty name."""
        with pytest.raises(ValidationError):
            OrganizationUpdateRequest(name="")


class TestOrganizationResponse:
    """Tests for OrganizationResponse schema."""

    def test_valid_response(self):
        """Test valid response creation."""
        now = datetime.now(UTC)
        response = OrganizationResponse(
            id="org-001",
            name="Test Org",
            aws_accounts=["123456789012"],
            role_mappings={"admin": "role-1"},
            settings={"enabled": True},
            created_at=now,
        )

        assert response.id == "org-001"
        assert response.name == "Test Org"
        assert response.created_at == now

    def test_from_attributes_config(self):
        """Test from_attributes config is set."""
        assert OrganizationResponse.model_config.get("from_attributes") is True

    def test_serialization(self):
        """Test response can be serialized."""
        response = OrganizationResponse(
            id="org-001",
            name="Test",
            aws_accounts=[],
            role_mappings={},
            settings={},
            created_at=datetime.now(UTC),
        )

        data = response.model_dump()
        assert data["id"] == "org-001"
        assert data["name"] == "Test"
        assert isinstance(data["aws_accounts"], list)


class TestOrganizationListResponse:
    """Tests for OrganizationListResponse schema."""

    def test_valid_response(self):
        """Test valid list response."""
        now = datetime.now(UTC)
        response = OrganizationListResponse(
            items=[OrganizationResponse(id="org-1", name="Org 1", aws_accounts=[], role_mappings={}, settings={}, created_at=now)],
            total=10,
            page=1,
            page_size=20,
            has_more=False,
        )

        assert len(response.items) == 1
        assert response.total == 10
        assert response.has_more is False


class TestPoolAccountCreateRequest:
    """Tests for PoolAccountCreateRequest schema."""

    def test_valid_request(self):
        """Test valid request."""
        request = PoolAccountCreateRequest(
            account_id="123456789012",
            role_arn="arn:aws:iam::123456789012:role/MyRole",
        )

        assert request.account_id == "123456789012"
        assert request.region == "us-east-1"  # Default

    def test_custom_region(self):
        """Test request with custom region."""
        request = PoolAccountCreateRequest(
            account_id="123456789012",
            role_arn="arn:aws:iam::123456789012:role/MyRole",
            region="eu-west-1",
        )

        assert request.region == "eu-west-1"

    def test_invalid_account_id_format(self):
        """Test validation fails for invalid account ID."""
        with pytest.raises(ValidationError) as exc_info:
            PoolAccountCreateRequest(
                account_id="invalid",
                role_arn="arn:aws:iam::123456789012:role/MyRole",
            )

        errors = exc_info.value.errors()
        assert any("account_id" in str(e["loc"]) for e in errors)

    def test_invalid_account_id_too_short(self):
        """Test validation fails for too short account ID."""
        with pytest.raises(ValidationError):
            PoolAccountCreateRequest(
                account_id="123",
                role_arn="arn:aws:iam::123456789012:role/MyRole",
            )

    def test_invalid_account_id_non_numeric(self):
        """Test validation fails for non-numeric account ID."""
        with pytest.raises(ValidationError):
            PoolAccountCreateRequest(
                account_id="12345678901a",
                role_arn="arn:aws:iam::123456789012:role/MyRole",
            )

    def test_invalid_role_arn_format(self):
        """Test validation fails for invalid role ARN."""
        with pytest.raises(ValidationError) as exc_info:
            PoolAccountCreateRequest(
                account_id="123456789012",
                role_arn="invalid-arn",
            )

        errors = exc_info.value.errors()
        assert any("role_arn" in str(e["loc"]) for e in errors)

    def test_valid_account_ids(self):
        """Test various valid account IDs."""
        valid_ids = ["123456789012", "000000000000", "999999999999"]
        for acc_id in valid_ids:
            request = PoolAccountCreateRequest(
                account_id=acc_id,
                role_arn=f"arn:aws:iam::{acc_id}:role/Role",
            )
            assert request.account_id == acc_id


class TestPoolAccountResponse:
    """Tests for PoolAccountResponse schema."""

    def test_valid_response(self):
        """Test valid response."""
        now = datetime.now(UTC)
        response = PoolAccountResponse(
            id="pool-001",
            account_id="123456789012",
            role_arn="arn:aws:iam::123456789012:role/Role",
            region="us-east-1",
            is_healthy=True,
            last_health_check=now,
            created_at=now,
        )

        assert response.id == "pool-001"
        assert response.is_healthy is True

    def test_nullable_last_health_check(self):
        """Test last_health_check can be None."""
        response = PoolAccountResponse(
            id="pool-001",
            account_id="123456789012",
            role_arn="arn:aws:iam::123456789012:role/Role",
            region="us-east-1",
            is_healthy=True,
            last_health_check=None,
            created_at=datetime.now(UTC),
        )

        assert response.last_health_check is None


class TestPoolStatusResponse:
    """Tests for PoolStatusResponse schema."""

    def test_valid_response(self):
        """Test valid response."""
        response = PoolStatusResponse(
            total_accounts=3,
            healthy_accounts=2,
            unhealthy_accounts=1,
            accounts=[],
        )

        assert response.total_accounts == 3
        assert response.healthy_accounts == 2
        assert response.unhealthy_accounts == 1


class TestPoolAccountStatus:
    """Tests for PoolAccountStatus enum."""

    def test_enum_values(self):
        """Test enum values."""
        assert PoolAccountStatus.HEALTHY.value == "healthy"
        assert PoolAccountStatus.UNHEALTHY.value == "unhealthy"
        assert PoolAccountStatus.UNKNOWN.value == "unknown"


class TestBudgetConfigUpdateRequest:
    """Tests for BudgetConfigUpdateRequest schema."""

    def test_all_fields_optional(self):
        """Test all fields are optional."""
        request = BudgetConfigUpdateRequest()
        assert request.budget_amount_usd is None
        assert request.enforcement_mode is None

    def test_valid_budget_amount(self):
        """Test valid budget amount."""
        request = BudgetConfigUpdateRequest(budget_amount_usd=Decimal("1000.00"))
        assert request.budget_amount_usd == Decimal("1000.00")

    def test_invalid_budget_amount_zero(self):
        """Test validation fails for zero budget."""
        with pytest.raises(ValidationError):
            BudgetConfigUpdateRequest(budget_amount_usd=Decimal("0"))

    def test_invalid_budget_amount_negative(self):
        """Test validation fails for negative budget."""
        with pytest.raises(ValidationError):
            BudgetConfigUpdateRequest(budget_amount_usd=Decimal("-100"))


class TestRateLimitConfigUpdateRequest:
    """Tests for RateLimitConfigUpdateRequest schema."""

    def test_all_fields_optional(self):
        """Test all fields are optional."""
        request = RateLimitConfigUpdateRequest()
        assert request.rpm is None
        assert request.tpm is None
        assert request.concurrent_requests is None

    def test_valid_values(self):
        """Test valid rate limit values."""
        request = RateLimitConfigUpdateRequest(
            rpm=100,
            tpm=10000,
            concurrent_requests=10,
        )

        assert request.rpm == 100
        assert request.tpm == 10000
        assert request.concurrent_requests == 10

    def test_zero_values_valid(self):
        """Test zero values are valid (disables limit)."""
        request = RateLimitConfigUpdateRequest(rpm=0, tpm=0, concurrent_requests=0)
        assert request.rpm == 0

    def test_invalid_negative_values(self):
        """Test validation fails for negative values."""
        with pytest.raises(ValidationError):
            RateLimitConfigUpdateRequest(rpm=-1)


class TestRateLimitConfigResponse:
    """Tests for RateLimitConfigResponse schema."""

    def test_valid_response(self):
        """Test valid response."""
        response = RateLimitConfigResponse(
            org_id="org-001",
            entity_type="org",
            entity_id="org-001",
            rpm=100,
            tpm=10000,
            concurrent_requests=10,
            updated_at=datetime.now(UTC),
        )

        assert response.org_id == "org-001"
        assert response.rpm == 100


class TestLogQueryRequest:
    """Tests for LogQueryRequest schema."""

    def test_all_fields_optional(self):
        """Test all fields are optional."""
        request = LogQueryRequest()
        assert request.start_time is None
        assert request.end_time is None
        assert request.org_id is None

    def test_valid_query(self):
        """Test valid query with all fields."""
        now = datetime.now(UTC)
        request = LogQueryRequest(
            start_time=now,
            end_time=now,
            org_id="org-001",
            user_id="user-001",
            status_code=200,
            path_pattern="/api/*",
            min_response_time_ms=100,
        )

        assert request.org_id == "org-001"
        assert request.min_response_time_ms == 100


class TestLogEntryResponse:
    """Tests for LogEntryResponse schema."""

    def test_valid_response(self):
        """Test valid response."""
        response = LogEntryResponse(
            id="log-001",
            timestamp=datetime.now(UTC),
            org_id="org-001",
            user_id="user-001",
            method="GET",
            path="/api/test",
            status_code=200,
            response_time_ms=50,
            request_body_size=None,
            response_body_size=100,
        )

        assert response.id == "log-001"
        assert response.status_code == 200


class TestUserRoleAssignRequest:
    """Tests for UserRoleAssignRequest schema."""

    def test_valid_request(self):
        """Test valid request."""
        request = UserRoleAssignRequest(
            user_id="user-001",
            role=AdminRole.ORG_ADMIN,
        )

        assert request.user_id == "user-001"
        assert request.role == AdminRole.ORG_ADMIN
        assert request.org_id is None
        assert request.dept_id is None

    def test_valid_request_with_scope(self):
        """Test valid request with scope."""
        request = UserRoleAssignRequest(
            user_id="user-001",
            role=AdminRole.DEPT_ADMIN,
            org_id="org-001",
            dept_id="dept-001",
        )

        assert request.org_id == "org-001"
        assert request.dept_id == "dept-001"

    def test_role_uses_admin_role_enum(self):
        """Test role field uses AdminRole enum."""
        for role in AdminRole:
            request = UserRoleAssignRequest(user_id="user", role=role)
            assert request.role == role


class TestUserRoleResponse:
    """Tests for UserRoleResponse schema."""

    def test_valid_response(self):
        """Test valid response."""
        response = UserRoleResponse(
            user_id="user-001",
            role=AdminRole.PLATFORM_ADMIN,
            org_id=None,
            dept_id=None,
            permissions=[Permission.ORG_CREATE, Permission.ORG_READ],
            created_at=datetime.now(UTC),
        )

        assert response.user_id == "user-001"
        assert response.role == AdminRole.PLATFORM_ADMIN
        assert Permission.ORG_CREATE in response.permissions


class TestHealthCheckSchemas:
    """Tests for health check schemas."""

    def test_health_check_component_healthy(self):
        """Test healthy component."""
        component = HealthCheckComponent(
            name="database",
            status="healthy",
            latency_ms=5,
        )

        assert component.name == "database"
        assert component.status == "healthy"
        assert component.latency_ms == 5
        assert component.error is None

    def test_health_check_component_unhealthy(self):
        """Test unhealthy component."""
        component = HealthCheckComponent(
            name="redis",
            status="unhealthy",
            error="Connection refused",
        )

        assert component.status == "unhealthy"
        assert component.error == "Connection refused"

    def test_health_check_response(self):
        """Test health check response."""
        response = HealthCheckResponse(
            status="healthy",
            timestamp=datetime.now(UTC),
        )

        assert response.status == "healthy"
        assert response.components is None

    def test_health_check_response_with_components(self):
        """Test health check response with components."""
        response = HealthCheckResponse(
            status="healthy",
            timestamp=datetime.now(UTC),
            components=[HealthCheckComponent(name="db", status="healthy")],
        )

        assert len(response.components) == 1

    def test_readiness_check_response(self):
        """Test readiness check response."""
        response = ReadinessCheckResponse(
            status="ready",
            timestamp=datetime.now(UTC),
            components=[
                HealthCheckComponent(name="database", status="healthy"),
                HealthCheckComponent(name="redis", status="healthy"),
            ],
            all_healthy=True,
        )

        assert response.status == "ready"
        assert response.all_healthy is True
        assert len(response.components) == 2


class TestDashboardSchemas:
    """Tests for dashboard response schemas."""

    def test_platform_dashboard_response(self):
        """Test platform dashboard response."""
        response = PlatformDashboardResponse(
            total_organizations=10,
            total_requests_24h=50000,
            total_tokens_24h=1000000,
            total_cost_24h=Decimal("150.00"),
            active_users_24h=100,
            error_rate_24h=0.02,
            pool_status=PoolStatusResponse(total_accounts=5, healthy_accounts=4, unhealthy_accounts=1, accounts=[]),
            top_organizations=[],
        )

        assert response.total_organizations == 10
        assert response.error_rate_24h == 0.02

    def test_org_dashboard_response(self):
        """Test org dashboard response."""
        response = OrgDashboardResponse(
            org_id="org-001",
            org_name="Test Org",
            total_requests_24h=5000,
            total_tokens_24h=100000,
            total_cost_24h=Decimal("15.00"),
            active_users_24h=10,
            error_rate_24h=0.01,
            budget_status={"remaining": 985.00},
            top_departments=[],
            top_models=[],
        )

        assert response.org_id == "org-001"
        assert response.org_name == "Test Org"


class TestSchemaSerialization:
    """Tests for schema serialization."""

    def test_organization_response_serialization(self):
        """Test organization response serializes correctly."""
        response = OrganizationResponse(
            id="org-001",
            name="Test",
            aws_accounts=["123456789012"],
            role_mappings={"admin": "role-1"},
            settings={"key": "value"},
            created_at=datetime.now(UTC),
        )

        data = response.model_dump()

        assert "id" in data
        assert "name" in data
        assert "aws_accounts" in data
        assert "role_mappings" in data
        assert "settings" in data
        assert "created_at" in data

    def test_pool_account_response_serialization(self):
        """Test pool account response serializes correctly."""
        response = PoolAccountResponse(
            id="pool-001",
            account_id="123456789012",
            role_arn="arn:aws:iam::123456789012:role/Role",
            region="us-east-1",
            is_healthy=True,
            last_health_check=None,
            created_at=datetime.now(UTC),
        )

        data = response.model_dump()

        assert data["is_healthy"] is True
        assert data["last_health_check"] is None

    def test_rate_limit_config_serialization(self):
        """Test rate limit config serializes correctly."""
        response = RateLimitConfigResponse(
            org_id="org-001",
            entity_type="org",
            entity_id="org-001",
            rpm=100,
            tpm=None,
            concurrent_requests=None,
            updated_at=datetime.now(UTC),
        )

        data = response.model_dump()

        assert data["rpm"] == 100
        assert data["tpm"] is None


class TestBudgetConfigResponse:
    """Tests for BudgetConfigResponse schema."""

    def test_valid_response(self):
        """Test valid budget config response."""
        response = BudgetConfigResponse(
            org_id="org-001",
            entity_type="org",
            entity_id="org-001",
            period_type="monthly",
            budget_amount_usd=Decimal("1000.00"),
            enforcement_mode="soft",
            updated_at=datetime.now(UTC),
        )

        assert response.org_id == "org-001"
        assert response.budget_amount_usd == Decimal("1000.00")
        assert response.enforcement_mode == "soft"


class TestLogQueryResponse:
    """Tests for LogQueryResponse schema."""

    def test_valid_response(self):
        """Test valid log query response."""
        response = LogQueryResponse(
            items=[],
            total=0,
            page=1,
            page_size=20,
            has_more=False,
        )

        assert response.total == 0
        assert response.has_more is False

    def test_response_with_items(self):
        """Test response with log items."""
        items = [
            LogEntryResponse(
                id="log-001",
                timestamp=datetime.now(UTC),
                org_id="org-001",
                user_id="user-001",
                method="GET",
                path="/api/test",
                status_code=200,
                response_time_ms=50,
                request_body_size=None,
                response_body_size=100,
            )
        ]

        response = LogQueryResponse(
            items=items,
            total=1,
            page=1,
            page_size=20,
            has_more=False,
        )

        assert len(response.items) == 1
        assert response.items[0].id == "log-001"
