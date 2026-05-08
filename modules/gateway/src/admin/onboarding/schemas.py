"""Pydantic schemas for the onboarding flow.

Issue #538: Onboarding flow — request/response models.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

# Regex: lowercase alphanumeric + hyphens, 3-64 chars, no leading/trailing hyphen
TENANT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
RESERVED_TENANT_IDS = frozenset({"admin", "system", "api", "root", "internal", "console", "platform", "null"})


class AccessStatusResponse(BaseModel):
    status: str  # "registered" | "new" | "pending"
    request_id: str | None = None


class AccessRequestPayload(BaseModel):
    """Onboarding request payload — the only field a user supplies is motivation.

    Tenant ID, provider, and provider_user_id are all derived server-side from
    the authenticated JWT (GitHub login + numeric ID). Previously these were
    required inputs which forced the Welcome form to show a confusing
    "Workspace ID" field.
    """

    motivation: str | None = None


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
