"""Pydantic schemas for account onboarding and vault credential endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


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
    secret_arns: list[str] = Field(
        default_factory=list,
        description="Secrets Manager ARNs for neocloud credentials",
    )
    irsa_role_arns: list[str] = Field(
        default_factory=list, description="IRSA role ARNs for data plane pods"
    )


class AccountResponse(BaseModel):
    """Single account representation."""

    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    provider: str
    account_id: str
    role_arn: str | None = None
    external_id: str | None = None
    status: str
    secret_arns: list[str] = Field(default_factory=list)
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
        default="api_key", pattern="^(api_key|service_account|oauth_token)$"
    )
    aws_account_id: str | None = Field(
        default=None, max_length=255, description="AWS account where secret is stored"
    )
    secret_arn: str = Field(
        ..., min_length=1, max_length=512, description="Secrets Manager ARN"
    )
    irsa_role_arn: str | None = Field(
        default=None,
        max_length=512,
        description="IRSA role ARN that can read the secret",
    )


class CredentialResponse(BaseModel):
    """Single credential representation."""

    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    provider: str
    credential_type: str
    secret_arn: str
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
