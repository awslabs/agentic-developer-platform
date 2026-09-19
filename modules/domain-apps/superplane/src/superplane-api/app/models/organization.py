"""Organization model — top-level tenant."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    cognito_sub: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True, index=True
    )
    billing_plan: Mapped[str] = mapped_column(
        String(50), nullable=False, default="free"
    )
    quotas_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Org settings (issue #260)
    billing_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    allowed_clouds: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    default_quotas: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # SSO configuration (issue #260)
    sso_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sso_metadata_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    sso_provider_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sso_provider_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    sso_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
