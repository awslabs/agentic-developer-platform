"""Persistent Security Agent spaces selected by a Product configuration."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class Binding(Base):
    __tablename__ = "l5_service_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "configuration_id"],
            ["l5_service_configurations.tenant_id", "l5_service_configurations.configuration_id"],
        ),
        UniqueConstraint("tenant_id", "configuration_id", "connection_key", "native_key"),
        Index("ix_l5_devops_binding_checks", "service_id", "state", "next_check_at"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    binding_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    configuration_id: Mapped[str] = mapped_column(String(36), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    service_id: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[dict] = mapped_column(Payload, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    ownership: Mapped[str] = mapped_column(String(32), nullable=False)
    connection_ref: Mapped[dict] = mapped_column(Payload, nullable=False)
    connection_key: Mapped[str] = mapped_column(String(64), nullable=False)
    native_resource_ref: Mapped[dict | None] = mapped_column(Payload)
    native_key: Mapped[str | None] = mapped_column(String(64))
    provider_identity: Mapped[dict | None] = mapped_column(Payload)
    creation_receipt: Mapped[dict | None] = mapped_column(Payload)
    readiness: Mapped[list] = mapped_column(Payload, nullable=False)
    gaps: Mapped[list] = mapped_column(Payload, nullable=False)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetainedAgentScope(Base):
    __tablename__ = "l5_service_retained_agent_scopes"

    service_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    identity_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(255))
    product_id: Mapped[str | None] = mapped_column(String(255))
    project_id: Mapped[str | None] = mapped_column(String(255))
    binding_id: Mapped[str | None] = mapped_column(String(36))
    conflicted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
