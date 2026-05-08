"""Pydantic schemas for the onboarding flow.

Issue #538: Onboarding flow — request/response models.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

# Regex: lowercase alphanumeric + hyphens, 3-64 chars, no leading/trailing hyphen
TENANT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
RESERVED_TENANT_IDS = frozenset({"admin", "system", "api", "root", "internal", "console", "platform", "null"})


class AccessStatusResponse(BaseModel):
    status: str  # "registered" | "new" | "pending"
    request_id: str | None = None


class AccessRequestPayload(BaseModel):
    proposed_tenant_id: str
    target_login: str
    provider: str
    provider_user_id: str
    motivation: str | None = None

    @field_validator("proposed_tenant_id")
    @classmethod
    def validate_tenant_id(cls, v: str) -> str:
        if v in RESERVED_TENANT_IDS:
            raise ValueError(f"'{v}' is a reserved name and cannot be used as a tenant ID")
        if not TENANT_ID_PATTERN.match(v):
            raise ValueError("Tenant ID must be 3-64 characters, lowercase alphanumeric + hyphens, no leading/trailing hyphen")
        return v


class AccessRequestResponse(BaseModel):
    status: str  # "approved" | "pending" | "collision" | "unavailable"
    tenant_id: str | None = None
    request_id: str | None = None
    redirect: str | None = None
    eta_hours: int | None = None
    reason: str | None = None


class AdminAccessRequestItem(BaseModel):
    id: str
    cognito_sub: str
    provider: str
    provider_user_id: str
    proposed_tenant_id: str
    target_login: str
    motivation: str | None
    status: str
    created_at: str


class AdminAccessRequestList(BaseModel):
    requests: list[AdminAccessRequestItem]


class AdminDecisionPayload(BaseModel):
    decision_note: str | None = None
