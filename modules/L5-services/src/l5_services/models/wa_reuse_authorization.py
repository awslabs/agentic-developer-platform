"""Explicit owner permission to reuse one retained result, never to execute work."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class WellArchitectedReuseAuthorization(Base):
    __tablename__ = "l5_service_wa_reuse_authorizations"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "result_id"], ["l5_service_results.tenant_id", "l5_service_results.result_id"]),
        Index("ix_l5_wa_reuse_owner", "tenant_id", "result_id", "actor_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    authorization_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    result_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[dict] = mapped_column(Payload, nullable=False)
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
