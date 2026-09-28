"""Immutable source-finalizer evidence; never current cleanup release authority."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ControllerCleanupSnapshot(Base):
    __tablename__ = "controller_cleanup_snapshots"
    __table_args__ = (
        CheckConstraint("octet_length(body)<=131072"),
        CheckConstraint("fence_token>0"),
        UniqueConstraint("org_id", "workspace_id", "allocation_id"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_operation_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    org_id: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    allocation_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sealed_revision: Mapped[str] = mapped_column(Text, nullable=False)
    report_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    enumeration_binding: Mapped[str] = mapped_column(Text, nullable=False)
    report_observations: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_id: Mapped[str] = mapped_column(Text, nullable=False)
    executor_id: Mapped[str] = mapped_column(Text, nullable=False)
    fence_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
