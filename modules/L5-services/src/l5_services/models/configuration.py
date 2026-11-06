"""Versioned Security configuration state owned by the L5 service."""

from sqlalchemy import JSON, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

Payload = JSON().with_variant(JSONB(), "postgresql")


class Configuration(Base):
    __tablename__ = "l5_service_configurations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "product_id", "project_id", "service_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    configuration_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    service_id: Mapped[str] = mapped_column(String(255), nullable=False)
    relationship_revision: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    handoff_state: Mapped[str] = mapped_column(String(16), nullable=False)


class ConfigurationVersion(Base):
    __tablename__ = "l5_service_config_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "configuration_id"],
            ["l5_service_configurations.tenant_id", "l5_service_configurations.configuration_id"],
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    configuration_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    settings: Mapped[dict] = mapped_column(Payload, nullable=False)
    inherited_fields: Mapped[list] = mapped_column(Payload, nullable=False)
    gaps: Mapped[list] = mapped_column(Payload, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ConfigurationReceipt(Base):
    __tablename__ = "l5_service_config_receipts"

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    service_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation: Mapped[str] = mapped_column(String(32), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
