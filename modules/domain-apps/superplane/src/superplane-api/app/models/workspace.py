"""Workspace model — primary user-facing resource (section 15.7)."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Valid isolation modes for workspaces
VALID_ISOLATION_MODES = ("dedicated", "namespace", "research")

# Workspace status constants
STATUS_PENDING = "pending"
STATUS_BOOTSTRAPPING = "bootstrapping"
STATUS_ACTIVE = "active"
STATUS_FAILED = "Failed"
STATUS_DRIFT_DETECTED = "drift_detected"
STATUS_RECONCILING = "reconciling"
STATUS_MAX_RETRIES_EXCEEDED = "max_retries_exceeded"


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    isolation_mode: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # dedicated, namespace, research
    aws_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cloud_accounts.id"), nullable=True
    )
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id"), nullable=True
    )
    shared_cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id"), nullable=True
    )
    namespace_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    quotas_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Budget guardrails — enforced for all modes but especially critical for research
    budget_max_daily_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    budget_max_gpus: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Budget guardrails — hourly spend limit
    budget_max_hourly_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    # Agent IAM policy ARN attached to the workspace (populated for research workspaces)
    agent_iam_role_arn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Platform's own default workspace — not deletable via CLI
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    # Reconciler tracking fields (US-H3)
    bootstrap_retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_bootstrap_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_drift_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconcile_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
