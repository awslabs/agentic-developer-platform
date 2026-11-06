"""Durable Product knowledge publication and revocation receipts."""

from sqlalchemy import JSON, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DevopsKnowledgePublication(Base):
    __tablename__ = "l5_devops_knowledge_publications"

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    space_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    intent: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String(255))
    asset_version: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(255))
