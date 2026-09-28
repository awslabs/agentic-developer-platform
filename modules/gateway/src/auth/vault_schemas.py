"""Pydantic schemas for vault credential and identity CRUD endpoints.

Issue #135: Vault Phase 2a — Credential + Identity CRUD
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator, model_validator

from src.shared.models.vault import CredentialType

# Valid ownership scope values
VALID_SCOPES = ("user", "team", "org", "domain_app")


# ---------------------------------------------------------------------------
# Credential schemas
# ---------------------------------------------------------------------------


class CredentialCreate(BaseModel):
    """Request body for POST /auth/credentials."""

    service: str
    label: str
    credential_type: CredentialType
    value: str  # Raw secret value — stored in SM, never returned
    scope_hint: str = "user"  # "user" | "team" | "org" | "domain_app"
    # domain_app_id is required when scope_hint == "domain_app"
    domain_app_id: str | None = None
    scopes: dict | None = None
    expires_at: datetime | None = None
    strict: bool = False

    @field_validator("scope_hint")
    @classmethod
    def validate_scope_hint(cls, v: str) -> str:
        if v not in VALID_SCOPES:
            raise ValueError(f"scope_hint must be one of {VALID_SCOPES}")
        return v

    @model_validator(mode="after")
    def validate_domain_app_id(self) -> CredentialCreate:
        if self.scope_hint == "domain_app" and not self.domain_app_id:
            raise ValueError("domain_app_id is required when scope_hint is 'domain_app'")
        return self


class CredentialUpdate(BaseModel):
    """Request body for PATCH /auth/credentials/{id}.

    Only label, expires_at, and strict may be updated.
    Value updates are intentionally excluded — re-register for audit clarity.
    """

    label: str | None = None
    expires_at: datetime | None = None
    strict: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_value_field(cls, data: dict) -> dict:
        if isinstance(data, dict) and "value" in data:
            raise ValueError("'value' cannot be updated via PATCH — delete and re-register for audit clarity")
        return data


class CredentialResponse(BaseModel):
    """Response body for credential endpoints. Never includes secret values or ARNs."""

    model_config = {"from_attributes": True}

    id: str
    service: str
    label: str
    credential_type: str
    scope: str  # "user" | "team" | "org" | "domain_app"
    scopes: dict | None
    expires_at: datetime | None
    last_used_at: datetime | None
    strict: bool
    created_at: datetime
    updated_at: datetime | None

    @classmethod
    def from_model(cls, cred) -> CredentialResponse:
        """Build response from a UserCredential ORM instance."""
        return cls(
            id=cred.id,
            service=cred.service,
            label=cred.label,
            credential_type=cred.credential_type,
            scope=cred.owner_scope,
            scopes=cred.scopes,
            expires_at=cred.expires_at,
            last_used_at=cred.last_used_at,
            strict=cred.strict,
            created_at=cred.created_at,
            updated_at=cred.updated_at,
        )


# ---------------------------------------------------------------------------
# Identity schemas
# ---------------------------------------------------------------------------


class IdentityResponse(BaseModel):
    """Response body for identity endpoints."""

    model_config = {"from_attributes": True}

    id: str
    provider: str
    provider_user_id: str
    provider_username: str | None
    verification_method: str
    verified_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, identity) -> IdentityResponse:
        """Build response from a UserIdentity ORM instance."""
        return cls(
            id=identity.id,
            provider=identity.provider,
            provider_user_id=identity.provider_user_id,
            provider_username=identity.provider_username,
            verification_method=identity.verification_method,
            verified_at=identity.verified_at,
            created_at=identity.created_at,
        )
