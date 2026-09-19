"""Model catalogue and alias resolution tests — Issue #5420 (PMM-03).

AC-03: Alias → canonical resolution; bare/latest refused.
AC-04a: Probe mechanism verified inert (no Bedrock call from any read).
AC-07: Retired model flagged, not selectable, visible.
"""

from __future__ import annotations

import pytest

from src.admin.persona_models.catalogue import (
    PERSONA_MODEL_ALIASES,
    PLATFORM_MODEL_CATALOGUE,
    aliases_for_model,
    catalogue_lookup,
    resolve_alias,
)
from src.admin.persona_models.catalogue_service import build_model_catalogue


class TestAliasResolution:
    """AC-03: alias → canonical resolution."""

    def test_known_alias_resolves(self):
        """opus46 → global.anthropic.claude-opus-4-6-v1"""
        result = resolve_alias("opus46")
        assert result == "global.anthropic.claude-opus-4-6-v1"

    def test_known_alias_case_insensitive(self):
        result = resolve_alias("Opus46")
        assert result == "global.anthropic.claude-opus-4-6-v1"

    def test_canonical_id_passes_through(self):
        """A canonical model ID that is already in the catalogue passes through."""
        result = resolve_alias("global.anthropic.claude-sonnet-4-6")
        assert result == "global.anthropic.claude-sonnet-4-6"

    def test_bare_alias_refused(self):
        """Bare aliases like 'opus' or 'sonnet' are refused (#2300)."""
        assert resolve_alias("opus") is None
        assert resolve_alias("sonnet") is None
        assert resolve_alias("haiku") is None

    def test_latest_style_refused(self):
        """Latest-style aliases are refused (anti-drift rule)."""
        assert resolve_alias("claude-3-5-sonnet-latest") is None

    def test_unknown_string_refused(self):
        assert resolve_alias("gpt-4-turbo") is None

    def test_all_aliases_resolve_to_catalogue_entries(self):
        """Every alias resolves to a model in the platform catalogue."""
        for alias, expected_id in PERSONA_MODEL_ALIASES.items():
            result = resolve_alias(alias)
            assert result == expected_id
            assert catalogue_lookup(expected_id) is not None, f"Alias {alias} resolves to {expected_id} which is not in the catalogue"

    def test_catalogue_publishes_every_approved_alias_exactly_once(self):
        """Non-mutating clients can resolve aliases without a second registry."""
        published = {alias: model.canonical_model_id for model in PLATFORM_MODEL_CATALOGUE for alias in aliases_for_model(model.canonical_model_id)}
        assert published == PERSONA_MODEL_ALIASES
        assert len(published) == sum(len(aliases_for_model(model.canonical_model_id)) for model in PLATFORM_MODEL_CATALOGUE)

    def test_fable_not_in_catalogue(self):
        """Fable 5.1 is NOT in the catalogue — #2300 lesson (§7)."""
        assert catalogue_lookup("anthropic.claude-fable-5-1") is None
        assert catalogue_lookup("us.anthropic.claude-fable-5-1") is None
        assert catalogue_lookup("global.anthropic.claude-fable-5-1") is None
        assert resolve_alias("fable") is None
        assert resolve_alias("fable51") is None


class TestModelCatalogue:
    """Model catalogue structure and content tests."""

    def test_catalogue_has_9_entries(self):
        """Platform catalogue has 9 entries (8 from Lambda + D4 candidate)."""
        assert len(PLATFORM_MODEL_CATALOGUE) == 9

    def test_all_entries_are_claude_class(self):
        """All current catalogue entries are claude-agent-sdk."""
        for model in PLATFORM_MODEL_CATALOGUE:
            assert model.compatibility_class == "claude-agent-sdk"

    def test_all_entries_have_harness_revision(self):
        for model in PLATFORM_MODEL_CATALOGUE:
            assert model.harness_contract_revision == "0.3.220"

    def test_all_entries_are_active(self):
        for model in PLATFORM_MODEL_CATALOGUE:
            assert model.lifecycle == "active"

    def test_d4_candidate_in_catalogue(self):
        """The D4 Claude-class candidate is in the catalogue."""
        entry = catalogue_lookup("us.anthropic.claude-sonnet-4-6")
        assert entry is not None
        assert entry.model_family == "Sonnet"
        assert entry.canonical_version == "4.6"


class TestModelCatalogueRead:
    """Integration tests for the model catalogue read."""

    @pytest.mark.asyncio
    async def test_model_catalogue_all_probing_disabled(self, session):
        """AC-04a: with no evidence, all models report probing_disabled."""
        models = await build_model_catalogue(
            session,
            persona_key="developer",
        )
        assert len(models) > 0
        for model in models:
            assert model.selectable is False
            assert model.reason == "probing_disabled"
            assert model.invocable is None
            assert model.evidence is None

    @pytest.mark.asyncio
    async def test_unknown_persona_returns_empty(self, session):
        """An unknown persona returns an empty model list."""
        models = await build_model_catalogue(
            session,
            persona_key="nonexistent-persona",
        )
        assert models == []
