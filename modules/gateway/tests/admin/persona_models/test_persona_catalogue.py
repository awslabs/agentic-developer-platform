"""Persona catalogue tests — Issue #5420 (PMM-03).

AC-01: Persona added/removed flows through without a second list edit.
AC-02: Exactly 12 registered keys; pt-superpower handled as non-configurable.
"""

from __future__ import annotations

from src.admin.persona_models.catalogue import (
    COMPATIBILITY_CLASS_CLAUDE,
    persona_compatibility_class,
    persona_is_configurable,
    persona_not_configurable_reason,
)
from src.admin.persona_models.catalogue_service import build_persona_catalogue


class TestPersonaCatalogue:
    """AC-01 and AC-02: the persona catalogue reads from the authoritative source."""

    def test_catalogue_has_exactly_12_personas(self):
        """AC-02: exactly 12 registered keys."""
        catalogue = build_persona_catalogue()
        assert len(catalogue) == 12, f"Expected 12 personas, got {len(catalogue)}: {[p.key for p in catalogue]}"

    def test_catalogue_keys_match_valid_personas(self):
        """AC-01/AC-02: keys are exactly VALID_PERSONAS, no more, no less."""
        from src.admin.persona_models._personas import VALID_PERSONAS

        catalogue = build_persona_catalogue()
        catalogue_keys = {p.key for p in catalogue}
        assert catalogue_keys == VALID_PERSONAS

    def test_catalogue_is_sorted(self):
        """Presentation property: alphabetical by key."""
        catalogue = build_persona_catalogue()
        keys = [p.key for p in catalogue]
        assert keys == sorted(keys)

    def test_all_personas_have_claude_class(self):
        """All current personas map to claude-agent-sdk (§2.4)."""
        catalogue = build_persona_catalogue()
        for persona in catalogue:
            assert persona.compatibility_class == COMPATIBILITY_CLASS_CLAUDE, (
                f"{persona.key} has class {persona.compatibility_class}, expected {COMPATIBILITY_CLASS_CLAUDE}"
            )

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
        """All 12 expected persona keys are present."""
        expected = {
            "aidlc",
            "architect",
            "codex",
            "developer",
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
        assert catalogue_keys == expected

    def test_non_vacuous_pass(self):
        """Anti-vacuity: at least 10 personas (copied from test_persona_catalogue_parity.py discipline)."""
        catalogue = build_persona_catalogue()
        assert len(catalogue) >= 10, "Catalogue is suspiciously small — check parsing."


class TestPersonaCompatibility:
    """Compatibility class registry tests (R2, §2.4)."""

    def test_known_persona_returns_class(self):
        assert persona_compatibility_class("developer") == COMPATIBILITY_CLASS_CLAUDE

    def test_unknown_persona_returns_none(self):
        assert persona_compatibility_class("nonexistent-persona") is None

    def test_no_cross_class_fallback(self):
        """A lookup miss is None, never a substitution from another class."""
        result = persona_compatibility_class("gpt-assistant")
        assert result is None

    def test_pt_superpower_has_class(self):
        """Even non-configurable personas have a class (they exist, just can't be configured)."""
        assert persona_compatibility_class("pt-superpower") == COMPATIBILITY_CLASS_CLAUDE

    def test_configurable_flag(self):
        assert persona_is_configurable("developer") is True
        assert persona_is_configurable("pt-superpower") is False

    def test_not_configurable_reason(self):
        assert persona_not_configurable_reason("developer") is None
        assert persona_not_configurable_reason("pt-superpower") == "dispatches_without_persona_identity"
