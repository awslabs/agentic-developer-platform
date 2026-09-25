"""Cluster model — infrastructure state (section 6.1)."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Explicit sharing eligibility (issue #6048). A cluster is never offered for
    # shared placement because of its name, account or dedicated ownership alone —
    # this must be set deliberately, separately from `workspace_id`/dedicated use.
    # Defaults false so every existing dedicated cluster stays dedicated-only after
    # this column is added; a migration cannot retroactively opt anything in.
    sharing_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Explicit platform authorization for placing tenant workspaces on ADP's own
    # management cluster (DESIGN.md §2.4 "Management EKS also used as data plane").
    # Independent of `sharing_enabled`: a management cluster must have both set to
    # accept a tenant workspace member, so an operator cannot half-configure this
    # by accident and have one flag imply the other.
    platform_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    desired_state_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    actual_state_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="Pending")
    cloud_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    cluster_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    eks_cluster_arn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    hyperpod_cluster_arn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cfn_stack_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    health_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_heartbeat: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconcile_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
