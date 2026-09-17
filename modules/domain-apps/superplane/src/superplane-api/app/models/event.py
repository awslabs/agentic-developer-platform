"""Event model — audit and events (section 6.1)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc="Authenticated user/org ID that performed the action",
    )
    action: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        doc="HTTP method or action verb (created, updated, deleted, read)",
    )
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ip: Mapped[str | None] = mapped_column(
        String(45), nullable=True, doc="Client IP address"
    )
    request_path: Mapped[str | None] = mapped_column(
        String(512), nullable=True, doc="Full request path"
    )
    http_status: Mapped[int | None] = mapped_column(
        nullable=True, doc="HTTP response status code"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_events_org_id_created_at", "org_id", "created_at"),
        Index("ix_events_resource_type", "resource_type"),
        Index("ix_events_user_id", "user_id"),
    )
