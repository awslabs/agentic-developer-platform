"""Pydantic schemas for organization endpoints."""

import ipaddress
import re
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

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
SSO_HOSTNAME = re.compile(
    r"[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?)+"
)


def sso_https_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if (
            not value.startswith("https://") or not hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "?" in value or "#" in value
            or parsed.port not in (None, 443)
            or parsed.netloc not in (hostname, f"{hostname}:443")
            or not SSO_HOSTNAME.fullmatch(hostname)
        ):
            raise ValueError()
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            return f"https://{hostname}"
    except ValueError:
        pass
    raise ValueError("SSO metadata URL must use an approved canonical HTTPS origin")


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

    model_config = ConfigDict(extra="forbid")

    @field_validator("sso_provider")
    @classmethod
    def valid_provider(cls, value: str) -> str:
        if value not in VALID_SSO_PROVIDERS:
            raise ValueError("unsupported SSO provider")
        return value

    @field_validator("sso_provider_type")
    @classmethod
    def valid_type(cls, value: str) -> str:
        if value not in VALID_SSO_TYPES:
            raise ValueError("unsupported SSO provider type")
        return value

    @field_validator("metadata_url")
    @classmethod
    def valid_metadata_url(cls, value: str) -> str:
        sso_https_origin(value)
        return value

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
