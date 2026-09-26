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
    tenant_id: str | None = None
    membership_role: str | None = None
    spend_eligibility: str = "not_evaluated"


class AccessRequestPayload(BaseModel):
    """Onboarding request payload — the only field a user supplies is motivation.

    Tenant ID, provider, and provider_user_id are all derived server-side from
    the authenticated JWT (GitHub login + numeric ID). Previously these were
    required inputs which forced the Welcome form to show a confusing
    "Workspace ID" field.
    """

    motivation: str | None = None
    target_tenant: str | None = None


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
    expected_role: str | None = None
    expected_scope: str | None = None


class AdminApprovalResponse(BaseModel):
    """The outcome of an approval, including the role it granted — #5666 (A11).

    The route previously returned a bare ``dict`` with no ``response_model``, so an
    approval never reported what authority it conferred. That is the one fact an
    approver most needs to see: with the role derived server-side, the response is
    the only place the decision becomes visible to the person who made it, and an
    unexpected ``org_admin`` is precisely what an operator should be able to notice.
    """

    status: str
    tenant_id: str
    granted_role: str
