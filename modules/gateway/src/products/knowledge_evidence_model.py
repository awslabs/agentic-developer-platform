"""Append-only local evidence for an original DevOps publication operation."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, String, event
from sqlalchemy.orm import Mapped, Session, mapped_column

from src.shared.models.base import Base


class KnowledgeUseEvidence(Base):
    __tablename__ = "l5_devops_knowledge_evidence"
    __table_args__ = (Index("ix_l5_knowledge_evidence_operation", "tenant_id", "operation_id", "kind"),)

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    document: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@event.listens_for(KnowledgeUseEvidence, "before_update")
@event.listens_for(KnowledgeUseEvidence, "before_delete")
def immutable_event(*args):
    raise ValueError("Knowledge evidence is append-only")


@event.listens_for(Session, "do_orm_execute")
def immutable_bulk(state):
    if (state.is_update or state.is_delete) and getattr(getattr(state.statement, "table", None), "name", None) == KnowledgeUseEvidence.__tablename__:
        raise ValueError("Knowledge evidence is append-only")
