"""Verified organization-owned Atlassian credentials, independent of user OAuth."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, TenantMixin, utcnow


class AtlassianSystemConnection(Base, TenantMixin):
    __tablename__ = "atlassian_system_connections"
    __table_args__ = (
        Index("ix_atlassian_system_pairing", "pairing_hash", unique=True),
        Index("ix_atlassian_system_installation", "forge_app_id", "forge_environment_id", "forge_installation_id", unique=True),
    )

    credential_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_credentials.id", ondelete="CASCADE"), primary_key=True)
    revision: Mapped[str] = mapped_column(String(36), nullable=False)
    provider_account_id: Mapped[str | None] = mapped_column(String(255))
    site_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workload_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(128), nullable=False)
    allowed_product_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    schemas: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unverified")
    connection_method: Mapped[str] = mapped_column(String(32), nullable=False, default="service_account", server_default="service_account")
    forge_app_id: Mapped[str | None] = mapped_column(String(255))
    forge_environment_id: Mapped[str | None] = mapped_column(String(255))
    forge_installation_id: Mapped[str | None] = mapped_column(String(255))
    pairing_hash: Mapped[str | None] = mapped_column(String(64))
    pairing_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    forge_token_hash: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AtlassianSystemKey(Base, TenantMixin):
    """Durable provider journal: failed setup/rotation never loses cleanup authority."""

    __tablename__ = "atlassian_system_keys"

    provider_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    credential_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_credentials.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
