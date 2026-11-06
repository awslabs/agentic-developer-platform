"""Versioned, owner-scoped Crew backup candidates in the control database."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid, utcnow


class CrewCheckpoint(Base):
    __tablename__ = "crew_checkpoints"
    __table_args__ = (
        CheckConstraint("generation > 0 AND sequence > 0", name="ck_crew_checkpoints_sequence"),
        CheckConstraint("status IN ('uploading', 'verified', 'failed', 'deleting', 'deleted')", name="ck_crew_checkpoints_status"),
        UniqueConstraint("tenant_id", "user_id", "generation", "idempotency_key", name="uq_crew_checkpoint_retry"),
        UniqueConstraint("tenant_id", "user_id", "sequence", name="uq_crew_checkpoint_sequence"),
        Index("ix_crew_checkpoints_executor_status", "executor_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    executor_id: Mapped[str] = mapped_column(String(36), ForeignKey("crew_executors.id"), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_key: Mapped[str] = mapped_column(String(52), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    predecessor_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="uploading", server_default="uploading")
    archive_bucket: Mapped[str | None] = mapped_column(String(255))
    archive_key: Mapped[str | None] = mapped_column(String(1024))
    archive_version_id: Mapped[str | None] = mapped_column(String(255))
    archive_sha256: Mapped[str | None] = mapped_column(String(64))
    archive_size: Mapped[int | None] = mapped_column(BigInteger)
    manifest_bucket: Mapped[str | None] = mapped_column(String(255))
    manifest_key: Mapped[str | None] = mapped_column(String(1024))
    manifest_version_id: Mapped[str | None] = mapped_column(String(255))
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    manifest_size: Mapped[int | None] = mapped_column(BigInteger)
    crew_version: Mapped[str | None] = mapped_column(String(64))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    failure_class: Mapped[str | None] = mapped_column(String(64))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restore_pins: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    restore_pod_uid: Mapped[str | None] = mapped_column(String(128))
    restore_pod_name: Mapped[str | None] = mapped_column(String(128))
    restore_box_id: Mapped[str | None] = mapped_column(String(255))
    restore_machine_generation: Mapped[int | None] = mapped_column(Integer)
    restore_token_hash: Mapped[str | None] = mapped_column(String(64))
