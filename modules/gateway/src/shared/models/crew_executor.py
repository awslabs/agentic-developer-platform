"""Tenant-owned Crew executor generations."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid, utcnow


class CrewExecutor(Base):
    __tablename__ = "crew_executors"
    __table_args__ = (
        CheckConstraint("length(tenant_id) > 0", name="ck_crew_executors_tenant_id"),
        CheckConstraint("length(user_id) > 0", name="ck_crew_executors_user_id"),
        CheckConstraint("target_kind IN ('eks-pod', 'ec2-instance')", name="ck_crew_executors_target_kind"),
        CheckConstraint("state IN ('provisioning', 'ready', 'stopped', 'ending', 'deleted')", name="ck_crew_executors_state"),
        CheckConstraint("generation > 0", name="ck_crew_executors_generation"),
        Index("uq_crew_executors_owner_key", "owner_key", unique=True),
        Index(
            "uq_crew_executors_active_owner",
            "tenant_id",
            "user_id",
            unique=True,
            postgresql_where=text("state <> 'deleted'"),
            sqlite_where=text("state <> 'deleted'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_key: Mapped[str | None] = mapped_column(String(52))
    latest_verified_checkpoint_id: Mapped[str | None] = mapped_column(String(36))
    team_id: Mapped[str | None] = mapped_column(String(255))
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="eks-pod", server_default="eks-pod")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="provisioning", server_default="provisioning")
    target_ref: Mapped[str | None] = mapped_column(String(512))
    dev_box_id: Mapped[str | None] = mapped_column(String(255))
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_generation: Mapped[int | None] = mapped_column(Integer)
    lease_expires_at: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idle_policy: Mapped[str | None] = mapped_column(String(255))
    approved_by: Mapped[str | None] = mapped_column(String(255))


class CrewOwnerGeneration(Base):
    __tablename__ = "crew_owner_generations"
    __table_args__ = (CheckConstraint("generation >= 0", name="ck_crew_owner_generation"),)

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
