"""Tests for common/model_validate.py — Issue #2279.

Tests alias resolution and fnmatch validation against allowed patterns.
"""

import sys
from pathlib import Path

# Add lambda root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from common.model_validate import resolve_and_validate


class TestAliasResolution:
    """Tests that known aliases resolve to the correct Bedrock model IDs."""

    def test_opus_resolves(self):
        result = resolve_and_validate("opus")
        assert result == "anthropic.claude-opus-4-20250514-v1:0"

    def test_sonnet_resolves(self):
        result = resolve_and_validate("sonnet")
        assert result == "anthropic.claude-sonnet-4-20250514-v1:0"

    def test_haiku_resolves(self):
        result = resolve_and_validate("haiku")
        assert result == "anthropic.claude-3-5-haiku-20241022-v1:0"

    def test_claude_opus_4_resolves(self):
        result = resolve_and_validate("claude-opus-4")
        assert result == "anthropic.claude-opus-4-20250514-v1:0"

    def test_claude_sonnet_4_resolves(self):
        result = resolve_and_validate("claude-sonnet-4")
        assert result == "anthropic.claude-sonnet-4-20250514-v1:0"

    def test_case_insensitive(self):
        """Aliases are case-insensitive (users may type OPUS or Opus)."""
        result = resolve_and_validate("OPUS")
        assert result == "anthropic.claude-opus-4-20250514-v1:0"

    def test_case_insensitive_mixed(self):
        result = resolve_and_validate("Sonnet")
        assert result == "anthropic.claude-sonnet-4-20250514-v1:0"


class TestPassThroughModelIds:
    """Tests that full Bedrock model IDs pass through and are validated."""

    def test_full_bedrock_id_allowed(self):
        """A full Bedrock model ID that matches default patterns passes through."""
        model_id = "anthropic.claude-sonnet-4-20250514-v1:0"
        result = resolve_and_validate(model_id)
        assert result == model_id

    def test_us_region_profile_allowed(self):
        """US-region inference profile IDs are allowed by default patterns."""
        model_id = "us.anthropic.claude-opus-4-20250514-v1:0"
        result = resolve_and_validate(model_id)
        assert result == model_id

    def test_non_anthropic_model_rejected_by_default(self):
        """A non-Claude model not in patterns is rejected (returns None)."""
        result = resolve_and_validate("meta.llama3-70b-instruct-v1:0")
        # Default patterns only allow anthropic.claude-* and regional variants
        assert result is None


class TestValidationAgainstPatterns:
    """Tests fnmatch validation against persona/tenant allowed_models."""

    def test_persona_allowed_models_restricts(self):
        """Persona's allowed_models restricts which models are valid."""
        # Persona only allows Sonnet
        persona_allowed = ["anthropic.claude-sonnet-*"]
        result = resolve_and_validate("opus", persona_allowed_models=persona_allowed)
        assert result is None  # Opus not in persona's allowed list

    def test_persona_allowed_models_permits(self):
        """Model that matches persona's pattern is allowed."""
        persona_allowed = ["anthropic.claude-opus-*"]
        result = resolve_and_validate("opus", persona_allowed_models=persona_allowed)
        assert result == "anthropic.claude-opus-4-20250514-v1:0"

    def test_tenant_patterns_used_when_persona_empty(self):
        """Tenant patterns are used when persona allowed_models is empty."""
        tenant_patterns = ["anthropic.claude-3-5-haiku-*"]
        result = resolve_and_validate(
            "haiku", persona_allowed_models=None, tenant_patterns=tenant_patterns
        )
        assert result == "anthropic.claude-3-5-haiku-20241022-v1:0"

    def test_tenant_patterns_reject(self):
        """Tenant patterns can reject a model."""
        tenant_patterns = ["anthropic.claude-sonnet-*"]
        result = resolve_and_validate(
            "opus", persona_allowed_models=None, tenant_patterns=tenant_patterns
        )
        assert result is None

    def test_default_patterns_allow_all_claude(self):
        """Default patterns allow all Anthropic Claude models."""
        for alias in ["opus", "sonnet", "haiku"]:
            result = resolve_and_validate(alias)
            assert result is not None, f"Alias '{alias}' should be allowed by defaults"


class TestUnknownAliases:
    """Tests behavior with unknown/invalid aliases."""

    def test_unknown_alias_that_looks_like_model_id(self):
        """An unknown alias that doesn't match any pattern returns None."""
        result = resolve_and_validate("gpt-4o")
        assert result is None

    def test_unknown_alias_gibberish(self):
        """Complete gibberish returns None."""
        result = resolve_and_validate("xyzzy-turbo-9000")
        assert result is None

    def test_empty_string_rejected(self):
        """Empty string alias is rejected."""
        result = resolve_and_validate("")
        assert result is None
