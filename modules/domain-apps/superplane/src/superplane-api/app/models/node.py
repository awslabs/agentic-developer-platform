"""Node model — individual GPU nodes (section 6.1)."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    node_pool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("node_pools.id"), nullable=False
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    cloud: Mapped[str | None] = mapped_column(String(50), nullable=True)
    region: Mapped[str | None] = mapped_column(String(50), nullable=True)
    instance_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    skypilot_cluster_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    k8s_node_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ssm_instance_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    public_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    private_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    gpu_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    gpu_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gpu_memory_gib: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="Provisioning"
    )
    health_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hourly_cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    terminated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
