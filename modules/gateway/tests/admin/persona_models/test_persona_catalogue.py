"""Persona catalogue tests — Issue #5420 (PMM-03).

AC-01: Persona added/removed flows through without a second list edit.
AC-02: Registered keys include event-selected personas; pt-superpower is non-configurable.
"""

from __future__ import annotations

import pytest

import src.admin.persona_models.catalogue as catalogue_module
from src.admin.persona_models.catalogue import (
    COMPATIBILITY_CLASS_CLAUDE,
    COMPATIBILITY_CLASS_CODEX,
    compatibility_class_harness_contract_revision,
    persona_compatibility_class,
    persona_harness_contract_revision,
    persona_is_configurable,
    persona_not_configurable_reason,
)
from src.admin.persona_models.catalogue_service import build_persona_catalogue


class TestPersonaCatalogue:
    """AC-01 and AC-02: the persona catalogue reads from the authoritative source."""

    def test_catalogue_has_exactly_20_personas(self):
        """AC-02: all 20 registered keys, including automatic personas."""
        catalogue = build_persona_catalogue()
        assert len(catalogue) == 20, f"Expected 20 personas, got {len(catalogue)}: {[p.key for p in catalogue]}"

    def test_catalogue_keys_match_valid_personas(self):
        """AC-01/AC-02: keys are exactly VALID_PERSONAS, no more, no less."""
        from src.admin.persona_models._personas import VALID_PERSONAS

        catalogue = build_persona_catalogue()
        catalogue_keys = {p.key for p in catalogue}
        from src.tasks.personas import TASK_PERSONAS

        assert not (VALID_PERSONAS & set(TASK_PERSONAS))
        assert catalogue_keys == VALID_PERSONAS | set(TASK_PERSONAS)

    def test_catalogue_is_sorted(self):
        """Presentation property: alphabetical by key."""
        catalogue = build_persona_catalogue()
        keys = [p.key for p in catalogue]
        assert keys == sorted(keys)

    def test_personas_use_their_execution_harness_class(self):
        """The native Codex reviewer is never classified as Claude."""
        catalogue = build_persona_catalogue()
        for persona in catalogue:
            expected = COMPATIBILITY_CLASS_CODEX if persona.key == "agent-codex-reviewer" else COMPATIBILITY_CLASS_CLAUDE
            if persona.key in {"agent-task-gpt-developer", "agent-task-gpt-intent-refinement"}:
                expected = COMPATIBILITY_CLASS_CODEX
            elif persona.key.startswith("agent-task-"):
                expected = "anthropic_messages"
            assert persona.compatibility_class == expected

    def test_pt_superpower_not_configurable(self):
        """AC-02: pt-superpower is listed but not configurable (§2.3)."""
        catalogue = build_persona_catalogue()
        superpower = next(p for p in catalogue if p.key == "pt-superpower")
        assert superpower.configurable is False
        assert superpower.not_configurable_reason == "dispatches_without_persona_identity"

    def test_configurable_personas_have_no_reason(self):
        """Configurable personas have no not_configurable_reason."""
        catalogue = build_persona_catalogue()
        for persona in catalogue:
            if persona.configurable:
                assert persona.not_configurable_reason is None, f"{persona.key} is configurable but has reason: {persona.not_configurable_reason}"

    def test_expected_persona_keys_present(self):
        """All 20 expected persona keys are present."""
        expected = {
            "agent-codex-reviewer",
            "aidlc",
            "architect",
            "codex",
            "developer",
            "intent-refinement",
            "malware-analysis-agent",
            "operations",
            "pm",
            "product",
            "pt-superpower",
            "reviewer",
            "superplane-operator",
            "superplane-researcher",
        }
        catalogue = build_persona_catalogue()
        catalogue_keys = {p.key for p in catalogue}
        assert catalogue_keys == expected | {
            "agent-task-gpt-developer",
            "agent-task-gpt-intent-refinement",
            "agent-task-investigator",
            "agent-task-cyber",
            "agent-task-claude-developer",
            "agent-task-codex-developer",
        }

    def test_non_vacuous_pass(self):
        """Anti-vacuity: at least 10 personas (copied from test_persona_catalogue_parity.py discipline)."""
        catalogue = build_persona_catalogue()
        assert len(catalogue) >= 10, "Catalogue is suspiciously small — check parsing."


class TestPersonaCompatibility:
    """Compatibility class registry tests (R2, §2.4)."""

    def test_known_persona_returns_class(self):
        assert persona_compatibility_class("developer") == COMPATIBILITY_CLASS_CLAUDE
        assert persona_compatibility_class("agent-codex-reviewer") == COMPATIBILITY_CLASS_CODEX

    def test_unknown_persona_returns_none(self):
        assert persona_compatibility_class("nonexistent-persona") is None

    def test_no_cross_class_fallback(self):
        """A lookup miss is None, never a substitution from another class."""
        result = persona_compatibility_class("gpt-assistant")
        assert result is None

    def test_harness_revision_is_server_owned_without_cross_class_fallback(self):
        assert persona_harness_contract_revision("developer") == "0.3.220"
        assert persona_harness_contract_revision("agent-codex-reviewer") == "0.155.1"
        assert compatibility_class_harness_contract_revision(COMPATIBILITY_CLASS_CODEX) == "0.155.1"

    def test_harness_revision_fails_closed_without_cross_class_fallback(self, monkeypatch):
        with pytest.raises(ValueError, match="Unknown persona key"):
            persona_harness_contract_revision("gpt-assistant")

        monkeypatch.setattr(
            catalogue_module,
            "persona_compatibility_class",
            lambda _persona_key: "future-sdk",
        )
        with pytest.raises(RuntimeError, match="without a registered harness contract revision"):
            catalogue_module.persona_harness_contract_revision("future-codex-persona")

    def test_pt_superpower_has_class(self):
        """Even non-configurable personas have a class (they exist, just can't be configured)."""
        assert persona_compatibility_class("pt-superpower") == COMPATIBILITY_CLASS_CLAUDE

    def test_configurable_flag(self):
        assert persona_is_configurable("developer") is True
        assert persona_is_configurable("pt-superpower") is False

    def test_not_configurable_reason(self):
        assert persona_not_configurable_reason("developer") is None
        assert persona_not_configurable_reason("pt-superpower") == "dispatches_without_persona_identity"
