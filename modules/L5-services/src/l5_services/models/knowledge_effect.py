"""Append-only knowledge effects and serialized native asset ownership."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class KnowledgeOwnership(Base):
    __tablename__ = "l5_devops_knowledge_ownership"

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    space_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    receipt: Mapped[dict | None] = mapped_column(Payload)


class KnowledgeEffect(Base):
    __tablename__ = "l5_devops_knowledge_effects"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            [
                "l5_service_setup_intents.tenant_id",
                "l5_service_setup_intents.operation_id",
            ],
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    work_id: Mapped[str] = mapped_column(String(36), nullable=False)
    lease_owner: Mapped[str] = mapped_column(String(255), nullable=False)
    fence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    payload: Mapped[dict] = mapped_column(Payload, nullable=False)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
