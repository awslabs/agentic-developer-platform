"""Admin module SQLAlchemy models."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, TenantMixin, new_uuid, utcnow


class AdminRole(Base):
    """Admin role definition table."""

    __tablename__ = "admin_roles"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    permissions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# Issue #3987: the `UserRole` ORM model was removed here. It declared a
# `user_roles` table that disagreed with the `user_roles` DDL in
# `alembic/versions/006_user_roles_table.py` on 6 of 9 columns (it had
# `role_id`/`dept_id`/`created_by`; the migration has `role`/`granted_by_user_id`/
# `granted_at`). Because `app.py` runs `Base.metadata.create_all` when
# BG_DB_AUTO_CREATE=true, test databases got the ORM shape while migrated
# databases got the migration shape — so a `select(UserRole)` would pass CI and
# raise UndefinedColumn in a deployed environment. It had zero read or write call
# sites. Server-side role resolution reads `tenant_memberships.role` instead (see
# `AccessControl.get_user_role`).


class RequestLog(Base, TenantMixin):
    """HTTP request logging table for audit and analytics."""

    __tablename__ = "request_logs"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    department_id: Mapped[str | None] = mapped_column(String(255), index=True)
    team_id: Mapped[str | None] = mapped_column(String(255))
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(2048), nullable=False, index=True)
    query_params: Mapped[dict | None] = mapped_column(JSON)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    response_time_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    request_body_size: Mapped[int | None] = mapped_column(Integer)
    response_body_size: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(255), index=True)
    client_ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(512))


class AuditLog(Base, TenantMixin):
    """Audit log for tracking administrative actions."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    old_value: Mapped[dict | None] = mapped_column(JSON)
    new_value: Mapped[dict | None] = mapped_column(JSON)
    client_ip: Mapped[str | None] = mapped_column(String(45))
