"""Expiring execution metadata; no worker token or provider credential is stored."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ControllerExecution(Base):
    __tablename__ = "controller_executions"

    operation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id"), nullable=False, index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("workspaces.id"), nullable=False
    )
    controller_holder: Mapped[str] = mapped_column(String(255), nullable=False)
    assignment: Mapped[dict] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ControllerProviderRequest(Base):
    __tablename__ = "controller_provider_requests"
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(255), nullable=False)
    cluster_name: Mapped[str] = mapped_column(String(255), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(255), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Which approved region a launch actually used, once known (#5925). Only
    # meaningful for a multi-region plan; a single-region plan already names
    # its one region in the approved controller_plan itself.
    region: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ControllerCapacity(Base):
    __tablename__ = "controller_capacity"
    org_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    cluster_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)


class ControllerExecutionAccounting(Base):
    __tablename__ = "controller_execution_accounting"
    operation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("workspaces.id"), nullable=False
    )
    observation: Mapped[dict] = mapped_column(JSON, nullable=False)


class ControllerBatchResult(Base):
    __tablename__ = "controller_batch_results"
    __table_args__ = (
        CheckConstraint(
            "octet_length(content)<=16384", name="ck_batch_result_size"
        ).ddl_if(dialect="postgresql"),
    )
    operation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("workspaces.id"), nullable=False
    )
    deployment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("deployments.id"), nullable=False
    )
    allocation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    job_uid: Mapped[str] = mapped_column(String(255), nullable=False)
    pod_uid: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    redacted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
