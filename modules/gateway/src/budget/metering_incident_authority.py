"""Operator-controlled, single-attempt authority for metering reconciliation."""

import os
from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class MeteringIncidentGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    incident_id: str = Field(min_length=8, max_length=128)
    actor_id: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(min_length=1, max_length=255)
    request_id: str = Field(min_length=1, max_length=255)
    action: Literal["apply_trusted_receipt", "confirm_not_dispatched"]
    expected_revision: int = Field(ge=1)
    evidence_ref: str = Field(min_length=1, max_length=768)
    evidence_version: str = Field(min_length=1, max_length=255)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def exact_scope(self):
        if any(value == "*" or value.strip() != value for value in (self.incident_id, self.actor_id, self.tenant_id, self.request_id)):
            raise ValueError("incident grants must name an exact actor and attempt")
        if "://" in self.evidence_ref or ".." in self.evidence_ref or self.evidence_ref.startswith("/"):
            raise ValueError("incident evidence must use a private object key")
        return self


def verify_metering_incident_grant(
    *,
    actor_id: str,
    tenant_id: str,
    request_id: str,
    action: str,
    expected_revision: int,
    evidence_ref: str,
    evidence_version: str,
    evidence_sha256: str,
) -> MeteringIncidentGrant:
    configured = os.environ.get("BG_METERING_INCIDENT_GRANTS")
    if not configured:
        raise HTTPException(403, "Protected incident authority required")
    try:
        grants = TypeAdapter(list[MeteringIncidentGrant]).validate_json(configured)
    except (ValueError, TypeError):
        raise HTTPException(503, "Protected incident authority unavailable") from None
    if len({grant.incident_id for grant in grants}) != len(grants):
        raise HTTPException(503, "Protected incident authority unavailable")
    matches = [
        grant
        for grant in grants
        if (
            grant.actor_id,
            grant.tenant_id,
            grant.request_id,
            grant.action,
            grant.expected_revision,
            grant.evidence_ref,
            grant.evidence_version,
            grant.evidence_sha256,
        )
        == (actor_id, tenant_id, request_id, action, expected_revision, evidence_ref, evidence_version, evidence_sha256)
        and grant.expires_at > datetime.now(UTC)
    ]
    if len(matches) != 1:
        raise HTTPException(403, "Protected incident authority required")
    return matches[0]
