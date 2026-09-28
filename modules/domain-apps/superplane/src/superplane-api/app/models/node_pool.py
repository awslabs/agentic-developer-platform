"""NodePool model — GPU node pool configuration (section 6.1)."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class NodePool(Base):
    __tablename__ = "node_pools"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    desired_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actual_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cloud: Mapped[str | None] = mapped_column(String(50), nullable=True)
    gpu_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    gpu_count_per_node: Mapped[int | None] = mapped_column(Integer, nullable=True)
    instance_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    disk_size_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    autoscale_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    autoscale_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spot_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
