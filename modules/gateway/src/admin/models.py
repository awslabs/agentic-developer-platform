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


class UserRole(Base):
    """User to admin role assignment table."""

    __tablename__ = "user_roles"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    role_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    org_id: Mapped[str | None] = mapped_column(String(255), index=True)  # Scope limit to organization
    dept_id: Mapped[str | None] = mapped_column(String(255), index=True)  # Scope limit to department
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(255))


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
