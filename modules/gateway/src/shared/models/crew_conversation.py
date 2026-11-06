"""Owner-scoped pointers to conversations in verified Crew checkpoints."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid


class CrewConversation(Base):
    __tablename__ = "crew_conversations"
    __table_args__ = (
        UniqueConstraint("checkpoint_id", "source", "conversation_id", name="uq_crew_conversation_checkpoint"),
        Index("ix_crew_conversations_owner_activity", "tenant_id", "user_id", "last_activity_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    checkpoint_id: Mapped[str] = mapped_column(String(36), ForeignKey("crew_checkpoints.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    archive_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unreadable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
