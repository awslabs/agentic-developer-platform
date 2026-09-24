"""Deployment model — model serving deployments (section 6.1)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "operation_id",
            name="uq_deployments_org_operation",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    operation_request_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation_target_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    controller_request_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    controller_approval_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    provider_uid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    namespace: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model_revision: Mapped[str | None] = mapped_column(String(100), nullable=True)
    precision: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # fp8, bf16, awq
    serving_framework: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # vllm, sglang, tgi
    desired_replicas: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    actual_replicas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gpu_per_replica: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tensor_parallel_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_model_len: Mapped[int | None] = mapped_column(Integer, nullable=True)
    endpoint_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="Pending")
    reconcile_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
