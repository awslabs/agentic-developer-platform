"""Authoritative persona and model catalogue data — Issue #5420 (PMM-03).

This module is the single source for:
  - The compatibility-class vocabulary (R2).
  - The persona-to-class registry (all current personas → claude-agent-sdk).
  - The platform-supported model catalogue (seeded from the Lambda's curated
    invocability-verified list plus the D4 Claude-class candidate).

Persona rows are derived from ``personas.py`` at request time — never copied
into a second list.  A persona added or removed in ``personas.py`` flows
through without any edit here (AC-01).

The staged copy of ``personas.py`` is imported at runtime; a parity test
in the test suite asserts it matches the authoritative source.  See
``docs/design-notes/5420-persona-and-model-catalogue.md`` §2.1 option (a).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Compatibility-class vocabulary (R2, §2.4)
# ---------------------------------------------------------------------------
# Class IDs are stable and unversioned.  Versioning lives in the separate
# ``harness_contract_revision`` field, so a harness upgrade revises evidence
# without renaming a class.

COMPATIBILITY_CLASS_CLAUDE = "claude-agent-sdk"
COMPATIBILITY_CLASS_CODEX = "codex-sdk"

COMPATIBILITY_CLASSES: frozenset[str] = frozenset({COMPATIBILITY_CLASS_CLAUDE, COMPATIBILITY_CLASS_CODEX})

# The Claude Agent SDK version pinned in the agent worker.
# Source: modules/agent-factory/agent/package.json → @anthropic-ai/claude-agent-sdk.
# Update this when the SDK pin moves and re-run probes (§4.1b).
HARNESS_CONTRACT_REVISION = "0.3.220"


# ---------------------------------------------------------------------------
# Persona-to-class registry (R2, §2.4)
# ---------------------------------------------------------------------------
# Every persona executing directly today maps to claude-agent-sdk.  The codex
# persona is NOT an exception: its outer agent is the Claude SDK worker and
# Codex is a bounded delegated tool (design §3.4).
#
# When #5433 registers native gpt-* personas, they register into this map
# as codex-sdk entries.  That epic does not create a second registry.

# Personas that are registered but not configurable, with reason.
_NOT_CONFIGURABLE: dict[str, str] = {
    # pt-superpower dispatches with no persona identity today (#4037).
    # Listing it lets the catalogue stay honest about what exists (AC-01)
    # while refusing a choice that cannot take effect.
    "pt-superpower": "dispatches_without_persona_identity",
}


def persona_compatibility_class(persona_key: str) -> str | None:
    """Return the compatibility class for a persona, or None if unknown.

    All registered personas currently map to ``claude-agent-sdk``.
    """
    # Import here to avoid circular imports and to read from the staged copy.
    # The staged copy is asserted to match the authoritative source by a
    # parity test — see tests/admin/persona_models/test_persona_parity.py.
    from src.admin.persona_models._personas import VALID_PERSONAS

    if persona_key not in VALID_PERSONAS:
        return None
    # All current personas execute through the Claude Agent SDK.
    return COMPATIBILITY_CLASS_CLAUDE


def persona_is_configurable(persona_key: str) -> bool:
    """Whether a persona can be configured with a model choice."""
    return persona_key not in _NOT_CONFIGURABLE


def persona_not_configurable_reason(persona_key: str) -> str | None:
    """Machine-readable reason why a persona is not configurable, or None."""
    return _NOT_CONFIGURABLE.get(persona_key)


# ---------------------------------------------------------------------------
# Platform-supported model catalogue (§3.3)
# ---------------------------------------------------------------------------
# Seeded from the Lambda's curated 8 entries (model_validate.py:19-38) plus the
# D4 Claude-class candidate us.anthropic.claude-sonnet-4-6.
#
# That curation came from direct bedrock-runtime invoke-model calls. Direct
# invocation is NOT proof of the Claude Agent SDK harness request shape, so it
# does not establish invocability for this story's compatibility class. Seeding
# decides catalogue MEMBERSHIP only; selectability requires a durable
# exact-harness evidence row (§4.1). Empty evidence means nothing here is
# selectable yet — that is the intended fail-closed state, not a gap.
#
# This is a NEW versioned artifact, not either alias map (design §3.3).
# The gateway alias map (model_resolver.py) is wider and contains known
# non-invocable entries; the Lambda alias map is invocability-curated but
# uses global. prefixes only.

LifecycleStatus = Literal["active", "retired"]


@dataclass(frozen=True)
class CatalogueModel:
    """One entry in the platform-supported model catalogue."""

    canonical_model_id: str
    model_family: str
    canonical_version: str
    compatibility_class: str
    harness_contract_revision: str
    lifecycle: LifecycleStatus = "active"


# The catalogue.  Order is presentational (family groups).
#
# Membership here asserts NOTHING about invocability. No entry below carries
# harness-shaped invocability proof; the evidence store is empty until a bounded
# probe runs under explicit spend approval (AC-04, PMM-09). Reading this list is
# not reading evidence.
#
# DO NOT add a model here because it has a price row or appears in a listing.
# Listing is not evidence.  Price coverage is not entitlement.  #2300.
PLATFORM_MODEL_CATALOGUE: tuple[CatalogueModel, ...] = (
    # --- Opus family ---
    CatalogueModel(
        canonical_model_id="global.anthropic.claude-opus-5",
        model_family="Opus",
        canonical_version="5",
        compatibility_class=COMPATIBILITY_CLASS_CLAUDE,
        harness_contract_revision=HARNESS_CONTRACT_REVISION,
    ),
    CatalogueModel(
        canonical_model_id="global.anthropic.claude-opus-4-8",
        model_family="Opus",
        canonical_version="4.8",
        compatibility_class=COMPATIBILITY_CLASS_CLAUDE,
        harness_contract_revision=HARNESS_CONTRACT_REVISION,
    ),
    CatalogueModel(
        canonical_model_id="global.anthropic.claude-opus-4-7",
        model_family="Opus",
        canonical_version="4.7",
        compatibility_class=COMPATIBILITY_CLASS_CLAUDE,
        harness_contract_revision=HARNESS_CONTRACT_REVISION,
    ),
    CatalogueModel(
        canonical_model_id="global.anthropic.claude-opus-4-6-v1",
        model_family="Opus",
        canonical_version="4.6",
        compatibility_class=COMPATIBILITY_CLASS_CLAUDE,
        harness_contract_revision=HARNESS_CONTRACT_REVISION,
    ),
    CatalogueModel(
        canonical_model_id="global.anthropic.claude-opus-4-5-20251101-v1:0",
        model_family="Opus",
        canonical_version="4.5",
        compatibility_class=COMPATIBILITY_CLASS_CLAUDE,
        harness_contract_revision=HARNESS_CONTRACT_REVISION,
    ),
    # --- Sonnet family ---
    CatalogueModel(
        canonical_model_id="global.anthropic.claude-sonnet-4-6",
        model_family="Sonnet",
        canonical_version="4.6",
        compatibility_class=COMPATIBILITY_CLASS_CLAUDE,
        harness_contract_revision=HARNESS_CONTRACT_REVISION,
    ),
    CatalogueModel(
        canonical_model_id="global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        model_family="Sonnet",
        canonical_version="4.5",
        compatibility_class=COMPATIBILITY_CLASS_CLAUDE,
        harness_contract_revision=HARNESS_CONTRACT_REVISION,
    ),
    # --- Haiku family ---
    CatalogueModel(
        canonical_model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0",
        model_family="Haiku",
        canonical_version="4.5",
        compatibility_class=COMPATIBILITY_CLASS_CLAUDE,
        harness_contract_revision=HARNESS_CONTRACT_REVISION,
    ),
    # --- D4 Claude-class candidate ---
    # us.anthropic.claude-sonnet-4-6 is the candidate default per the approved
    # design §3.3.  It is NOT a proven default — evidence is empty until
    # PMM-09 runs the probes (R3).  No alias resolves to the us. form today;
    # closing that gap is PMM-09's (#5427).
    CatalogueModel(
        canonical_model_id="us.anthropic.claude-sonnet-4-6",
        model_family="Sonnet",
        canonical_version="4.6",
        compatibility_class=COMPATIBILITY_CLASS_CLAUDE,
        harness_contract_revision=HARNESS_CONTRACT_REVISION,
    ),
)

# Index for O(1) lookups by canonical model ID.
_CATALOGUE_BY_ID: dict[str, CatalogueModel] = {m.canonical_model_id: m for m in PLATFORM_MODEL_CATALOGUE}


def catalogue_lookup(canonical_model_id: str) -> CatalogueModel | None:
    """Look up a model by its canonical versioned identifier."""
    return _CATALOGUE_BY_ID.get(canonical_model_id)


# ---------------------------------------------------------------------------
# Alias resolution — persona-selection baseline (§3.6, C3)
# ---------------------------------------------------------------------------
# Restricted to models in PLATFORM_MODEL_CATALOGUE.  This is deliberately
# narrower than model_resolver.py's DEFAULT_MODEL_ALIASES, which includes
# non-Anthropic families (openai.* for the Codex bridge) that D6 forbids
# for persona execution.
#
# Bare/ambiguous aliases (opus, sonnet, haiku) are refused, not resolved,
# to prevent silent drift when a provider moves a "latest" pointer (#2300).

PERSONA_MODEL_ALIASES: dict[str, str] = {
    "opus5": "global.anthropic.claude-opus-5",
    "opus48": "global.anthropic.claude-opus-4-8",
    "opus47": "global.anthropic.claude-opus-4-7",
    "opus46": "global.anthropic.claude-opus-4-6-v1",
    "opus45": "global.anthropic.claude-opus-4-5-20251101-v1:0",
    "sonnet46": "global.anthropic.claude-sonnet-4-6",
    "sonnet45": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "haiku45": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
}


def resolve_alias(alias: str) -> str | None:
    """Resolve a friendly alias to a canonical model ID, or None if unknown.

    Returns the canonical ID if the alias is in the persona-selection alias
    map.  Returns None for bare/ambiguous aliases and unknown strings.
    A canonical model ID that is already in the catalogue passes through.
    """
    lowered = alias.lower().strip()
    # Direct alias lookup
    if lowered in PERSONA_MODEL_ALIASES:
        return PERSONA_MODEL_ALIASES[lowered]
    # Pass through if it's already a known canonical ID
    if lowered in _CATALOGUE_BY_ID or alias in _CATALOGUE_BY_ID:
        return _CATALOGUE_BY_ID.get(lowered, _CATALOGUE_BY_ID.get(alias)).canonical_model_id
    return None


# ---------------------------------------------------------------------------
# Persona-selection allowed patterns (§3.4, C3)
# ---------------------------------------------------------------------------
# Separate from model_resolver.py's DEFAULT_ALLOWED_PATTERNS, which includes
# openai.* to keep the Codex bridge working (#2709/#2713).  D6 permits
# Anthropic Claude only for persona execution.

PERSONA_ALLOWED_PATTERNS: tuple[str, ...] = (
    "anthropic.claude-*",
    "us.anthropic.claude-*",
    "eu.anthropic.claude-*",
    "global.anthropic.claude-*",
)
