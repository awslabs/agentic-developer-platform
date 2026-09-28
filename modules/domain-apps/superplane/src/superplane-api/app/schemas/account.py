"""Pydantic schemas for account onboarding and vault credential endpoints."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.models.credential import (
    MAX_ADP_REFERENCE_COUNT,
    MAX_ADP_REFERENCE_LENGTH,
    validate_adp_credential_id,
)

ADPReference = Annotated[str, StringConstraints(max_length=MAX_ADP_REFERENCE_LENGTH)]

# ── Account (POST /accounts, GET /accounts, DELETE /accounts/{id}) ──


class RegisterAccountRequest(BaseModel):
    """POST /accounts — register a BYOA cloud account."""

    name: str = Field(
        ..., min_length=1, max_length=255, description="Friendly name for the account"
    )
    provider: str = Field(default="aws", pattern="^(aws|nebius|lambda|gcp|azure)$")
    account_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="AWS account ID or provider identifier",
    )
    role_arn: str = Field(
        ..., min_length=1, max_length=512, description="Cross-account IAM role ARN"
    )
    external_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="External ID for cross-account trust",
    )
    ingest_role_arn: str | None = Field(
        default=None, max_length=512, description="Ingest Lambda cross-account role ARN"
    )
    # Issue #5046 (U13b): ADP credential IDs, not Secrets Manager ARNs. A copied secret
    # ARN is a second route to the secret material outside the vault; an ADP credential ID
    # is an opaque handle only the vault can resolve.
    adp_credential_ids: list[ADPReference] = Field(
        default_factory=list,
        max_length=MAX_ADP_REFERENCE_COUNT,
        description="ADP credential IDs (opaque vault references) for neocloud credentials",
    )

    irsa_role_arns: list[str] = Field(
        default_factory=list, description="IRSA role ARNs for data plane pods"
    )

    @field_validator("adp_credential_ids")
    @classmethod
    def _reject_arns_or_secrets(cls, values: list[str]) -> list[str]:
        """Validate every element, matching the singular field on the credential request.

        Without this, the list form was the unguarded way into the same defect: the
        singular `adp_credential_id` rejected an ARN with a 422 while this field accepted a
        whole list of them. Validating here keeps the boundary behavior consistent, so a
        bad payload is a 422 naming the field rather than a 500 out of the model hook.

        Note `irsa_role_arns` above is deliberately NOT validated: an IAM role ARN is an
        identity, not secret material, and rejecting it would break cross-account
        assumption while protecting nothing.
        """
        return [validate_adp_credential_id(value) for value in values]


class AccountResponse(BaseModel):
    """Single account representation."""

    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    provider: str
    account_id: str
    status: str
    adp_credential_ids: list[str] = Field(default_factory=list)
    irsa_role_arns: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AccountListResponse(BaseModel):
    """GET /accounts — list response."""

    accounts: list[AccountResponse]
    total: int


class AccountDeleteResponse(BaseModel):
    """DELETE /accounts/{id} response."""

    id: uuid.UUID
    status: str = "Deleted"
    message: str = "Account deregistered"


# ── Vault Credentials (POST /vault/credentials, GET, DELETE) ──


class RegisterCredentialRequest(BaseModel):
    """POST /vault/credentials — register a credential ARN."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Friendly name for the credential",
    )
    provider: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Cloud provider (nebius, lambda, etc.)",
    )
    credential_type: str = Field(
        default="api_key", pattern="^(api_key|service_account|oauth_token|aws_role)$"
    )
    aws_account_id: str | None = Field(
        default=None, max_length=255, description="AWS account where secret is stored"
    )
    # Issue #5046 (U13b). Was `secret_arn`. Validated with the same rule the model
    # enforces, so an ARN is rejected at the API boundary with a 422 rather than reaching
    # the database layer.
    adp_credential_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="ADP credential ID — an opaque vault reference, never a secret ARN",
    )

    irsa_role_arn: str | None = Field(
        default=None,
        max_length=512,
        description="IRSA role ARN that can read the secret",
    )

    @field_validator("adp_credential_id")
    @classmethod
    def _reject_arn_or_secret(cls, value: str) -> str:
        return validate_adp_credential_id(value)


class CredentialResponse(BaseModel):
    """Single credential representation."""

    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    provider: str
    credential_type: str
    adp_credential_id: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CredentialListResponse(BaseModel):
    """GET /vault/credentials — list response."""

    credentials: list[CredentialResponse]
    total: int


class CredentialDeleteResponse(BaseModel):
    """DELETE /vault/credentials/{id} response."""

    id: uuid.UUID
    status: str = "Deleted"
    message: str = "Credential deregistered"
