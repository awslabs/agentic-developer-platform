"""Tenant-local, append-only Security manual acceptance events."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, String, event
from sqlalchemy.orm import Mapped, Session, mapped_column

from src.shared.models.base import Base, utcnow


class SecurityEvidenceEvent(Base):
    __tablename__ = "l5_security_evidence_events"
    __table_args__ = (Index("ix_l5_security_evidence_request", "tenant_id", "request_id", "kind"),)

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    document: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


@event.listens_for(SecurityEvidenceEvent, "before_update")
@event.listens_for(SecurityEvidenceEvent, "before_delete")
def immutable_event(*args):
    raise ValueError("Security evidence is append-only; append revocation instead")


@event.listens_for(Session, "do_orm_execute")
def immutable_bulk(state):
    if (state.is_update or state.is_delete) and getattr(getattr(state.statement, "table", None), "name", None) == SecurityEvidenceEvent.__tablename__:
        raise ValueError("Security evidence is append-only")
