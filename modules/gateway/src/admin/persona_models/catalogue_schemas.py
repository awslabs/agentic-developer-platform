"""Pydantic schemas for persona-model catalogue — Issue #5420 (PMM-03).

Design: ``docs/design-notes/5420-persona-and-model-catalogue.md`` §6.1/§6.2.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Persona catalogue (§6.1)
# ---------------------------------------------------------------------------


class PersonaCatalogueRow(BaseModel):
    """One persona in the catalogue."""

    key: str = Field(description="Stable persona key from the authoritative registry.")
    display_name: str = Field(description="Human-readable persona name.")
    purpose: str = Field(description="Brief description of the persona's role.")
    configurable: bool = Field(description="Whether this persona can be configured with a model choice.")
    not_configurable_reason: str | None = Field(
        default=None,
        description="Machine-readable reason when not configurable (e.g. 'dispatches_without_persona_identity').",
    )
    compatibility_class: str = Field(description="The harness compatibility class this persona executes under.")


# ---------------------------------------------------------------------------
# Invocability evidence (§6.2)
# ---------------------------------------------------------------------------


class InvocabilityEvidence(BaseModel):
    """Nested evidence for a model's invocability at a specific destination."""

    account_id: str = Field(description="The AWS account the evidence was gathered from.")
    region: str = Field(description="The AWS region the evidence was gathered from.")
    verified_at: datetime = Field(description="When the probe ran.")
    expires_at: datetime = Field(description="When this evidence expires.")
    stale: bool = Field(description="Whether this evidence has expired.")
    request_shape_sha256: str | None = Field(
        default=None,
        description="SHA-256 of the request body the probe sent. Nullable when no evidence exists.",
    )


# ---------------------------------------------------------------------------
# Selectable-model catalogue (§6.2)
# ---------------------------------------------------------------------------


class PriceContext(BaseModel):
    """Optional pricing context for display.  Presentational only — §3.7."""

    input_per_million_tokens: float | None = Field(default=None)
    output_per_million_tokens: float | None = Field(default=None)


class ModelCatalogueRow(BaseModel):
    """One model in the selectable-model catalogue."""

    canonical_model_id: str = Field(description="The versioned provider identifier.  Never a floating alias.")
    model_family: str = Field(description="E.g. 'Sonnet', 'Opus', 'Haiku'.")
    canonical_version: str = Field(description="E.g. '4.6', '4.5'.")
    selectable: bool = Field(description="The single boolean consumers branch on.")
    reason: str | None = Field(
        default=None,
        description="Refusal code when selectable is false.",
    )
    permitted: bool | None = Field(
        default=None,
        description="Whether tenant policy permits this model.  null = unevaluated.",
    )
    invocable: bool | None = Field(
        default=None,
        description=(
            "Whether the model is proven invocable at the caller's destination.  "
            "null = unproven (probing disabled); false = probe ran and model refused."
        ),
    )
    evidence: InvocabilityEvidence | None = Field(
        default=None,
        description="Invocability evidence when available.",
    )
    compatibility_class: str = Field(description="The harness compatibility class.")
    harness_contract_revision: str = Field(description="Versioned contract revision within the class.")
    retired: bool = Field(
        default=False,
        description="Whether this model has been retired from the catalogue.",
    )
    price_context: PriceContext | None = Field(
        default=None,
        description="Optional pricing data for display.  A missing price row does not remove a model.",
    )


# ---------------------------------------------------------------------------
# Catalogue response
# ---------------------------------------------------------------------------


class PersonaCatalogueResponse(BaseModel):
    """Response for the persona catalogue read."""

    personas: list[PersonaCatalogueRow]


class ModelCatalogueResponse(BaseModel):
    """Response for the selectable-model catalogue read."""

    persona_key: str = Field(description="The persona this catalogue is filtered for.")
    compatibility_class: str = Field(description="The persona's compatibility class.")
    models: list[ModelCatalogueRow]


# ---------------------------------------------------------------------------
# Shared validation result (§6.3)
# ---------------------------------------------------------------------------

SelectionReasonCode = Literal[
    "unknown_model",
    "unknown_persona",
    "not_permitted",
    "harness_incompatible",
    "not_invocable",
    "probing_disabled",
    "evidence_stale",
    "retired",
    "alias_not_pinned",
    "no_class_default",
]


class SelectionResult(BaseModel):
    """A successful validation result.

    The only acceptable success: backed by a fresh, exact-key evidence row.
    """

    canonical_model_id: str
    compatibility_class: str
    harness_contract_revision: str
    evidence_verified_at: datetime | None = None


class SelectionRejection(BaseModel):
    """A validation refusal with a stable reason code."""

    reason: SelectionReasonCode
    message: str
