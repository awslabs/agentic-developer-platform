"""Finite owner selections for read-only WA qualification observations."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class WellArchitectedQualification(Base):
    __tablename__ = "l5_service_wa_qualification"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "configuration_id", "configuration_revision"],
                             ["l5_service_config_versions.tenant_id", "l5_service_config_versions.configuration_id",
                              "l5_service_config_versions.revision"]),
        CheckConstraint("mode = 'observations'", name="ck_l5_wa_qualification_observations"),
        Index("ix_l5_wa_qualification_owner", "tenant_id", "configuration_id", "actor_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    configuration_id: Mapped[str] = mapped_column(String(36), nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="observations")
    selection: Mapped[dict] = mapped_column(Payload, nullable=False)
    selection_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
