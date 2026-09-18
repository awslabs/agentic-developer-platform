"""CloudAccount model — account factory + BYOA onboarding (sections 15.3, 15.7)."""

import json
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.database import Base
from app.models.credential import validate_adp_credential_id


class CloudAccount(Base):
    """Registered cloud account — either Superplane-managed or customer-onboarded (BYOA).

    For BYOA accounts, the CLI creates IAM roles + secrets in the user's account,
    then registers the IAM role ARNs here via POST /accounts.

    Issue #5046 (U13b), R7 schema half: this record references secret material only by
    **ADP credential ID** (`adp_credential_ids_json`), never by a copied secret ARN. See
    `credential.py` for why holding a secret's address is itself the defect.

    The IAM role ARNs below are deliberately retained and are NOT the same class of
    reference. A role ARN names *who may act* — it is an identity, carries no secret
    value, and grants nothing on its own without a trust policy permitting the caller to
    assume it. A secret ARN, by contrast, is the address of secret material. Removing the
    role ARNs would break cross-account role assumption while protecting nothing.
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
    adp_credential_ids_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON list of ADP credential IDs (opaque vault references, never secret ARNs)
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

    @validates("adp_credential_ids_json")
    def _check_adp_credential_ids(self, _key: str, value: str | None) -> str | None:
        """Apply the same ARN-free rule to every element of the list column.

        `credential.py` places this rule on the model rather than only in the request
        schema so it holds for every writer -- router, reconciler, backfill, fixture. That
        reasoning applies identically here, and without this hook it did not: this column
        is an unbounded `Text`, so before this check a caller could store the very thing
        the rename removes -- a copied secret ARN, or a whole PEM block -- under the new
        compliant-looking column name, on `cloud_accounts` instead of `credential_registry`.

        Nothing resolves this column today, so that was a latent gap rather than a live
        leak. Closing it here is what makes the story's rule true of the record it names,
        instead of true only of the sibling table.

        Raises:
            ValueError: if the value is not a JSON list, or any element is ARN-shaped or
                looks like secret material.
        """
        if value is None:
            return None

        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "adp_credential_ids_json must be a JSON list of ADP credential IDs; "
                f"could not parse it as JSON ({exc.msg})"
            ) from exc

        if not isinstance(parsed, list):
            raise ValueError(
                "adp_credential_ids_json must be a JSON *list* of ADP credential IDs, "
                f"got {type(parsed).__name__}"
            )

        for element in parsed:
            # Reuses credential.py's function rather than restating the rule, so the two
            # columns cannot drift apart on what counts as an acceptable reference.
            validate_adp_credential_id(element)

        return value
