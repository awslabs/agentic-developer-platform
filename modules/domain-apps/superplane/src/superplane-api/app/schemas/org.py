"""Pydantic schemas for organization endpoints."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field


# -- Allowed cloud / quota types --

VALID_CLOUDS = {"aws", "gcp", "azure", "lambda", "nebius"}
VALID_SSO_PROVIDERS = {
    "okta",
    "azure-ad",
    "google",
    "onelogin",
    "ping",
    "custom-saml",
    "custom-oidc",
}
VALID_SSO_TYPES = {"SAML", "OIDC"}


class OrgResponse(BaseModel):
    """GET /orgs/current — organization details."""

    id: uuid.UUID
    name: str
    billing_plan: str
    quotas_json: str | None = None
    billing_email: str | None = None
    allowed_clouds: list[str] | None = None
    default_quotas: dict[str, Any] | None = None
    sso_enabled: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class OrgUpdateRequest(BaseModel):
    """PATCH /orgs/current — update org settings.

    All fields are optional; only provided fields are updated.
    """

    name: str | None = Field(
        None, min_length=1, max_length=255, description="Organization name"
    )
    billing_email: EmailStr | None = Field(None, description="Billing contact email")
    allowed_clouds: list[str] | None = Field(
        None,
        description=f"Allowed cloud providers: {', '.join(sorted(VALID_CLOUDS))}",
    )
    default_quotas: dict[str, Any] | None = Field(
        None,
        description="Default workspace quotas (e.g. max_daily_usd, max_gpus)",
    )
    billing_plan: str | None = Field(None, description="Billing plan tier")


class OrgUpdateResponse(BaseModel):
    """Response after updating org settings."""

    id: uuid.UUID
    name: str
    billing_plan: str
    billing_email: str | None = None
    allowed_clouds: list[str] | None = None
    default_quotas: dict[str, Any] | None = None
    sso_enabled: bool = False
    updated: bool = True

    model_config = {"from_attributes": True}


# -- SSO schemas --


class SSOConfigRequest(BaseModel):
    """PATCH /orgs/current/sso — configure SSO provider."""

    sso_provider: str = Field(
        ...,
        description=f"SSO provider: {', '.join(sorted(VALID_SSO_PROVIDERS))}",
    )
    sso_provider_type: str = Field(
        "SAML",
        description="Federation type: SAML or OIDC",
    )
    metadata_url: str = Field(
        ...,
        max_length=2048,
        description="IdP metadata URL (SAML metadata endpoint or OIDC issuer URL)",
    )
    provider_name: str | None = Field(
        None,
        max_length=255,
        description="Display name for the IdP (defaults to sso_provider value)",
    )
    enable: bool = Field(True, description="Enable SSO after configuration")


class SSOConfigResponse(BaseModel):
    """SSO configuration details."""

    sso_provider: str | None = None
    sso_provider_type: str | None = None
    sso_provider_name: str | None = None
    sso_metadata_url: str | None = None
    sso_enabled: bool = False
    cognito_idp_identifier: str | None = Field(
        None,
        description="Cognito identity provider identifier (set after successful registration)",
    )

    model_config = {"from_attributes": True}


class SSODisableRequest(BaseModel):
    """DELETE /orgs/current/sso — disable SSO."""

    confirm: bool = Field(..., description="Must be true to confirm SSO removal")
