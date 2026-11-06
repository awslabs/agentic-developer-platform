"""DevOps-owned native creation receipt linked to a shared setup operation."""

from sqlalchemy import JSON, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DevopsSpaceIntent(Base):
    __tablename__ = "l5_devops_space_intents"

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    selection: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    native_space_id: Mapped[str | None] = mapped_column(String(255))
    ownership: Mapped[str | None] = mapped_column(String(32))
    scope_fingerprint: Mapped[str | None] = mapped_column(String(64))
    resource_observation: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    integration_observation: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    prevention_observation: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    prevention_policy: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    prevention_operation_id: Mapped[str | None] = mapped_column(String(128))
    owned_trigger_ids: Mapped[list] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list)
    resume_trigger_ids: Mapped[list] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list)
    active_executions: Mapped[list] = mapped_column(JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list)
    update_operation_id: Mapped[str | None] = mapped_column(String(36))
    update_configuration_revision: Mapped[int | None] = mapped_column(Integer)
    requested_description: Mapped[str | None] = mapped_column(String(1000))
    effective_description: Mapped[str | None] = mapped_column(String(1000))
