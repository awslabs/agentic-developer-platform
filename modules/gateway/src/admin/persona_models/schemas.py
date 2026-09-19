"""Wire shapes for persona-model preference endpoints — Issue #5419 (PMM-02).

One vocabulary across both self and administration surfaces so the client
branches on ``reason`` rather than maintaining two error parsers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from src.shared.models.persona_models import ALIAS_SOURCES

# The registrable alias vocabulary is DERIVED from the model's ALIAS_SOURCES, which
# the migration-parity test already ties to migration 056. It was previously a
# hand-written Literal listing only three of the five approved sources, so the API
# rejected `eventbridge` and `github_actions` outright: the column accepted them,
# resolution looks for them, and no administrator could ever register one. A third
# hand-kept copy of this list would drift the same way, so it is not repeated here.
AliasSource = Literal[ALIAS_SOURCES]  # type: ignore[valid-type]

# ── Response models ──────────────────────────────────────────────────────────


class PreferenceEntry(BaseModel):
    """One persona's effective model choice for a principal."""

    persona_key: str
    persona_display_name: str
    configurable: bool

    effective_model_id: str | None = None
    source: Literal["principal-mapping", "system-default"]
    status: Literal["configured", "not-configured", "unavailable", "disallowed", "stale"] = "not-configured"

    saved_model_id: str | None = None
    requested_alias: str | None = None
    revision: int | None = None
    updated_at: datetime | None = None


class PreferenceListResponse(BaseModel):
    """Full list of persona preferences for a principal."""

    principal_kind: str
    principal_id: str
    entries: list[PreferenceEntry]


class PreferenceDetailResponse(BaseModel):
    """Single-persona explainer — why this model is effective."""

    persona_key: str
    effective_model_id: str | None = None
    source: Literal["principal-mapping", "system-default"]
    status: str

    saved_model_id: str | None = None
    requested_alias: str | None = None
    revision: int | None = None
    updated_at: datetime | None = None

    default_model_id: str | None = None
    default_source: str = "claude-agent-sdk"


# ── Request models ───────────────────────────────────────────────────────────


class SetPreferenceRequest(BaseModel):
    """Body for ``PUT /me/persona-models/{persona_key}``.

    ``expected_revision`` absent/None means create-only.
    Present means update — must match the current row's revision.
    """

    model: str = Field(min_length=1, description="Canonical model ID or a recognised alias")
    expected_revision: int | None = Field(
        default=None,
        description="Current revision for optimistic concurrency; omit for create-only",
    )


# ── Administration models ────────────────────────────────────────────────────

# The exact truthful vocabulary for the manageable-principal source label.
# Each value is the display form of one of the five approved alias sources,
# plus "unknown" for the no-active-alias edge case.
ManageableSourceLabel = Literal[
    "agent-registry",
    "sa-registration",
    "cognito-client",
    "eventbridge",
    "github-actions",
    "unknown",
]


class ManageableServicePrincipal(BaseModel):
    """One service principal the caller may administer."""

    canonical_principal_id: str
    principal_kind: Literal["service_account"] = "service_account"
    display_name: str
    tenant_label: str
    source: ManageableSourceLabel
    manageable: bool


class ManageableServicePrincipalsResponse(BaseModel):
    """Discovery endpoint response — principals the caller may manage."""

    principals: list[ManageableServicePrincipal]


# ── Conflict response ────────────────────────────────────────────────────────


class ConflictResponse(BaseModel):
    """409 body: the full current row in the same shape a GET returns.

    The client can re-render without a separate GET, and the current state
    is unambiguous: source, status and effective model are present.
    """

    persona_key: str
    principal_kind: str
    principal_id: str
    source: Literal["principal-mapping"] = "principal-mapping"
    status: Literal["configured"] = "configured"
    effective_model_id: str
    current_model_id: str
    current_revision: int
    updated_at: datetime
    updated_by: str
    default_model_id: str | None = None
    default_source: str = "claude-agent-sdk"


# ── Registration models ─────────────────────────────────────────────────────


class RegisterServicePrincipalRequest(BaseModel):
    """Body for ``POST /service-principals/register``."""

    display_name: str = Field(min_length=1, max_length=255)
    alias_source: AliasSource
    alias_id: str = Field(min_length=1, max_length=255)


class RegisterServicePrincipalResponse(BaseModel):
    """Response after registering a new service principal."""

    canonical_principal_id: str
    display_name: str
    alias_source: str
    alias_id: str
    status: str


class LinkAliasRequest(BaseModel):
    """Body for ``POST /service-principals/{canonical_id}/aliases``."""

    alias_source: AliasSource
    alias_id: str = Field(min_length=1, max_length=255)


class StatusTransitionRequest(BaseModel):
    """Body for ``PATCH /service-principals/{canonical_id}/status``."""

    status: Literal["active", "suspended", "retired"]


class StatusTransitionResponse(BaseModel):
    """Response after a lifecycle status transition."""

    canonical_principal_id: str
    display_name: str
    previous_status: str
    status: str


class AliasResponse(BaseModel):
    """Response for alias operations."""

    alias_id: str
    alias_source: str
    canonical_principal_id: str
    is_active: bool
    registered_by: str
