"""Durable, generation-scoped recommendation collection progress."""

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class GenerationCollection(Base):
    __tablename__ = "l5_service_generation_collections"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
        CheckConstraint("page_count >= 1", name="ck_l5_generation_collection_pages"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    native_job_ref: Mapped[dict] = mapped_column(Payload, nullable=False)
    next_token: Mapped[str | None] = mapped_column(String(2048))
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    recommendations: Mapped[list] = mapped_column(Payload, nullable=False)
    pending_recommendations: Mapped[list] = mapped_column(Payload, nullable=False, default=list, server_default="[]")
    excluded: Mapped[list] = mapped_column(Payload, nullable=False, default=list, server_default="[]")
    result_id: Mapped[str | None] = mapped_column(String(36))
