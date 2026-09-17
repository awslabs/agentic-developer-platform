"""CloudAccount model — account factory + BYOA onboarding (sections 15.3, 15.7)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CloudAccount(Base):
    """Registered cloud account — either Superplane-managed or customer-onboarded (BYOA).

    For BYOA accounts, the CLI creates IAM roles + secrets in the user's account,
    then registers the ARNs here via POST /accounts.
    """

    __tablename__ = "cloud_accounts"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "account_identifier", name="uq_cloud_accounts_org_account"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # aws, nebius, lambda, gcp, azure
    account_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    friendly_name: Mapped[str] = mapped_column(String(255), nullable=False)
    provisioning_mode: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # superplane_managed, customer_onboarded
    cross_account_role_arn: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ingest_role_arn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    irsa_role_arns_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON list of IRSA role ARNs
    secret_arns_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON list of secret ARNs
    cfn_stack_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="Provisioning"
    )
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
