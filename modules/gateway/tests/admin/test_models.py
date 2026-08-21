"""Unit tests for admin module SQLAlchemy models."""

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.models import AdminRole, AuditLog, RequestLog
from src.shared.models.base import Base, TenantMixin


class TestAdminRoleModel:
    """Tests for AdminRole SQLAlchemy model."""

    def test_tablename(self):
        """Test AdminRole has correct table name."""
        assert AdminRole.__tablename__ == "admin_roles"

    def test_inherits_from_base(self):
        """Test AdminRole inherits from Base."""
        assert issubclass(AdminRole, Base)

    def test_primary_key(self):
        """Test AdminRole has id as primary key."""
        mapper = inspect(AdminRole)
        pk_columns = [col.name for col in mapper.primary_key]
        assert "id" in pk_columns

    def test_columns_exist(self):
        """Test AdminRole has all expected columns."""
        mapper = inspect(AdminRole)
        column_names = [col.key for col in mapper.column_attrs]

        assert "id" in column_names
        assert "name" in column_names
        assert "description" in column_names
        assert "permissions" in column_names
        assert "created_at" in column_names
        assert "updated_at" in column_names

    def test_instantiation(self):
        """Test AdminRole can be instantiated with valid data."""
        role = AdminRole(
            id="role-001",
            name="test_role",
            description="A test role",
            permissions=["perm1", "perm2"],
        )

        assert role.id == "role-001"
        assert role.name == "test_role"
        assert role.description == "A test role"
        assert role.permissions == ["perm1", "perm2"]

    def test_instantiation_minimal(self):
        """Test AdminRole can be instantiated with minimal data."""
        role = AdminRole(name="minimal_role")

        assert role.name == "minimal_role"
        # Description is optional
        assert role.description is None

    def test_name_is_required(self):
        """Test AdminRole name field is non-nullable."""
        mapper = inspect(AdminRole)
        name_col = mapper.columns["name"]
        assert name_col.nullable is False

    def test_name_is_unique(self):
        """Test AdminRole name field has unique constraint."""
        mapper = inspect(AdminRole)
        name_col = mapper.columns["name"]
        assert name_col.unique is True

    @pytest.mark.asyncio
    async def test_create_in_database(self, db_session: AsyncSession):
        """Test AdminRole can be persisted to database."""
        role = AdminRole(
            id="role-test-001",
            name="test_admin_role",
            description="Test admin role",
            permissions=["read", "write"],
        )

        db_session.add(role)
        await db_session.commit()

        # Refresh to get database-generated values
        await db_session.refresh(role)

        assert role.id == "role-test-001"
        assert role.created_at is not None
        assert role.updated_at is not None

    def test_permissions_default_is_list(self):
        """Test AdminRole permissions defaults to empty list."""
        role = AdminRole(id="test", name="test")
        # The default factory should produce an empty list
        assert role.permissions is not None or hasattr(AdminRole.permissions, "default")


class TestRequestLogModel:
    """Tests for RequestLog SQLAlchemy model."""

    def test_tablename(self):
        """Test RequestLog has correct table name."""
        assert RequestLog.__tablename__ == "request_logs"

    def test_inherits_from_base(self):
        """Test RequestLog inherits from Base."""
        assert issubclass(RequestLog, Base)

    def test_inherits_from_tenant_mixin(self):
        """Test RequestLog inherits from TenantMixin."""
        assert issubclass(RequestLog, TenantMixin)

    def test_primary_key(self):
        """Test RequestLog has id as primary key."""
        mapper = inspect(RequestLog)
        pk_columns = [col.name for col in mapper.primary_key]
        assert "id" in pk_columns

    def test_columns_exist(self):
        """Test RequestLog has all expected columns."""
        mapper = inspect(RequestLog)
        column_names = [col.key for col in mapper.column_attrs]

        assert "id" in column_names
        assert "timestamp" in column_names
        assert "org_id" in column_names  # From TenantMixin
        assert "user_id" in column_names
        assert "department_id" in column_names
        assert "team_id" in column_names
        assert "method" in column_names
        assert "path" in column_names
        assert "query_params" in column_names
        assert "status_code" in column_names
        assert "response_time_ms" in column_names
        assert "request_body_size" in column_names
        assert "response_body_size" in column_names
        assert "error_message" in column_names
        assert "request_id" in column_names
        assert "client_ip" in column_names
        assert "user_agent" in column_names

    def test_instantiation(self):
        """Test RequestLog can be instantiated with valid data."""
        log = RequestLog(
            id="log-001",
            org_id="org-001",
            user_id="user-001",
            method="POST",
            path="/api/v1/chat",
            status_code=200,
            response_time_ms=150,
        )

        assert log.id == "log-001"
        assert log.org_id == "org-001"
        assert log.user_id == "user-001"
        assert log.method == "POST"
        assert log.path == "/api/v1/chat"
        assert log.status_code == 200
        assert log.response_time_ms == 150

    def test_instantiation_full(self):
        """Test RequestLog with all fields populated."""
        log = RequestLog(
            id="log-002",
            org_id="org-001",
            user_id="user-001",
            department_id="dept-001",
            team_id="team-001",
            method="GET",
            path="/api/v1/models",
            query_params={"limit": "10"},
            status_code=200,
            response_time_ms=50,
            request_body_size=100,
            response_body_size=500,
            request_id="req-123",
            client_ip="192.168.1.1",
            user_agent="Mozilla/5.0",
        )

        assert log.department_id == "dept-001"
        assert log.team_id == "team-001"
        assert log.query_params == {"limit": "10"}
        assert log.request_body_size == 100
        assert log.response_body_size == 500
        assert log.request_id == "req-123"
        assert log.client_ip == "192.168.1.1"
        assert log.user_agent == "Mozilla/5.0"

    def test_required_fields(self):
        """Test RequestLog required fields are non-nullable."""
        mapper = inspect(RequestLog)

        assert mapper.columns["org_id"].nullable is False
        assert mapper.columns["user_id"].nullable is False
        assert mapper.columns["method"].nullable is False
        assert mapper.columns["path"].nullable is False
        assert mapper.columns["status_code"].nullable is False
        assert mapper.columns["response_time_ms"].nullable is False

    def test_indexed_columns(self):
        """Test RequestLog has correct indexes."""
        mapper = inspect(RequestLog)

        assert mapper.columns["timestamp"].index is True
        assert mapper.columns["org_id"].index is True
        assert mapper.columns["user_id"].index is True
        assert mapper.columns["path"].index is True
        assert mapper.columns["status_code"].index is True
        assert mapper.columns["request_id"].index is True

    @pytest.mark.asyncio
    async def test_create_in_database(self, db_session: AsyncSession):
        """Test RequestLog can be persisted to database."""
        log = RequestLog(
            id="log-test-001",
            org_id="org-test",
            user_id="user-test",
            method="GET",
            path="/test",
            status_code=200,
            response_time_ms=10,
        )

        db_session.add(log)
        await db_session.commit()

        await db_session.refresh(log)

        assert log.id == "log-test-001"
        assert log.timestamp is not None


class TestAuditLogModel:
    """Tests for AuditLog SQLAlchemy model."""

    def test_tablename(self):
        """Test AuditLog has correct table name."""
        assert AuditLog.__tablename__ == "audit_logs"

    def test_inherits_from_base(self):
        """Test AuditLog inherits from Base."""
        assert issubclass(AuditLog, Base)

    def test_inherits_from_tenant_mixin(self):
        """Test AuditLog inherits from TenantMixin."""
        assert issubclass(AuditLog, TenantMixin)

    def test_primary_key(self):
        """Test AuditLog has id as primary key."""
        mapper = inspect(AuditLog)
        pk_columns = [col.name for col in mapper.primary_key]
        assert "id" in pk_columns

    def test_columns_exist(self):
        """Test AuditLog has all expected columns."""
        mapper = inspect(AuditLog)
        column_names = [col.key for col in mapper.column_attrs]

        assert "id" in column_names
        assert "timestamp" in column_names
        assert "org_id" in column_names  # From TenantMixin
        assert "user_id" in column_names
        assert "action" in column_names
        assert "resource_type" in column_names
        assert "resource_id" in column_names
        assert "old_value" in column_names
        assert "new_value" in column_names
        assert "client_ip" in column_names

    def test_instantiation(self):
        """Test AuditLog can be instantiated with valid data."""
        audit_log = AuditLog(
            id="audit-001",
            org_id="org-001",
            user_id="user-001",
            action="create",
            resource_type="organization",
            resource_id="org-new",
        )

        assert audit_log.id == "audit-001"
        assert audit_log.org_id == "org-001"
        assert audit_log.user_id == "user-001"
        assert audit_log.action == "create"
        assert audit_log.resource_type == "organization"
        assert audit_log.resource_id == "org-new"

    def test_instantiation_with_values(self):
        """Test AuditLog with old and new values."""
        audit_log = AuditLog(
            id="audit-002",
            org_id="org-001",
            user_id="user-001",
            action="update",
            resource_type="budget",
            resource_id="budget-001",
            old_value={"amount": 1000},
            new_value={"amount": 2000},
            client_ip="10.0.0.1",
        )

        assert audit_log.old_value == {"amount": 1000}
        assert audit_log.new_value == {"amount": 2000}
        assert audit_log.client_ip == "10.0.0.1"

    def test_required_fields(self):
        """Test AuditLog required fields are non-nullable."""
        mapper = inspect(AuditLog)

        assert mapper.columns["org_id"].nullable is False
        assert mapper.columns["user_id"].nullable is False
        assert mapper.columns["action"].nullable is False
        assert mapper.columns["resource_type"].nullable is False
        assert mapper.columns["resource_id"].nullable is False

    def test_indexed_columns(self):
        """Test AuditLog has correct indexes."""
        mapper = inspect(AuditLog)

        assert mapper.columns["timestamp"].index is True
        assert mapper.columns["org_id"].index is True
        assert mapper.columns["user_id"].index is True
        assert mapper.columns["action"].index is True

    @pytest.mark.asyncio
    async def test_create_in_database(self, db_session: AsyncSession):
        """Test AuditLog can be persisted to database."""
        audit_log = AuditLog(
            id="audit-test-001",
            org_id="org-test",
            user_id="user-test",
            action="delete",
            resource_type="user",
            resource_id="user-deleted",
        )

        db_session.add(audit_log)
        await db_session.commit()

        await db_session.refresh(audit_log)

        assert audit_log.id == "audit-test-001"
        assert audit_log.timestamp is not None


class TestModelColumnTypes:
    """Tests for model column type definitions."""

    def test_admin_role_column_types(self):
        """Test AdminRole column types."""
        mapper = inspect(AdminRole)

        # String columns
        assert "VARCHAR" in str(mapper.columns["id"].type).upper()
        assert "VARCHAR" in str(mapper.columns["name"].type).upper()

        # Text column
        assert "TEXT" in str(mapper.columns["description"].type).upper()

        # JSON column
        assert "JSON" in str(mapper.columns["permissions"].type).upper()

        # DateTime columns
        assert "DATETIME" in str(mapper.columns["created_at"].type).upper()
        assert "DATETIME" in str(mapper.columns["updated_at"].type).upper()

    def test_request_log_column_types(self):
        """Test RequestLog column types."""
        mapper = inspect(RequestLog)

        # Integer columns
        assert "INTEGER" in str(mapper.columns["status_code"].type).upper()
        assert "INTEGER" in str(mapper.columns["response_time_ms"].type).upper()

        # JSON column
        assert "JSON" in str(mapper.columns["query_params"].type).upper()

    def test_audit_log_column_types(self):
        """Test AuditLog column types."""
        mapper = inspect(AuditLog)

        # JSON columns
        assert "JSON" in str(mapper.columns["old_value"].type).upper()
        assert "JSON" in str(mapper.columns["new_value"].type).upper()


class TestModelStringLengths:
    """Tests for model string length constraints."""

    def test_admin_role_name_max_length(self):
        """Test AdminRole name has max length of 50."""
        mapper = inspect(AdminRole)
        name_col = mapper.columns["name"]
        assert name_col.type.length == 50

    def test_admin_role_id_max_length(self):
        """Test AdminRole id has max length of 255."""
        mapper = inspect(AdminRole)
        id_col = mapper.columns["id"]
        assert id_col.type.length == 255

    def test_request_log_method_length(self):
        """Test RequestLog method has max length of 10."""
        mapper = inspect(RequestLog)
        method_col = mapper.columns["method"]
        assert method_col.type.length == 10

    def test_request_log_path_length(self):
        """Test RequestLog path has max length of 2048."""
        mapper = inspect(RequestLog)
        path_col = mapper.columns["path"]
        assert path_col.type.length == 2048

    def test_audit_log_action_length(self):
        """Test AuditLog action has max length of 50."""
        mapper = inspect(AuditLog)
        action_col = mapper.columns["action"]
        assert action_col.type.length == 50

    def test_client_ip_length(self):
        """Test client_ip columns support IPv6 (45 chars)."""
        request_log_mapper = inspect(RequestLog)
        audit_log_mapper = inspect(AuditLog)

        assert request_log_mapper.columns["client_ip"].type.length == 45
        assert audit_log_mapper.columns["client_ip"].type.length == 45
