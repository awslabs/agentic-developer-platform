"""Managed GitLab namespaces. Provider IDs survive Product renames and retries."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKeyConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, utcnow


class ProductGitlabHome(Base):
    __tablename__ = "product_gitlab_homes"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.id"]),
        Index("ix_gitlab_home_reconcile", "checked_at", "tenant_id", "product_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    instance_url: Mapped[str] = mapped_column(String(512), nullable=False)
    group_id: Mapped[int | None] = mapped_column(BigInteger)
    full_path: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="preparing")
    error_code: Mapped[str | None] = mapped_column(String(64))
    membership_digest: Mapped[str | None] = mapped_column(String(64))
    # Canonical login user ID -> GitLab user ID. Contains no credentials.
    members: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
