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

    # Version-pinned aliases (<family><major><minor>) → invocable inference
    # profiles (global. prefix). Bare opus/sonnet/haiku were removed.
    def test_opus48_resolves(self):
        assert resolve_and_validate("opus48") == "global.anthropic.claude-opus-4-8"

    def test_opus46_resolves(self):
        assert resolve_and_validate("opus46") == "global.anthropic.claude-opus-4-6-v1"

    def test_sonnet46_resolves(self):
        assert resolve_and_validate("sonnet46") == "global.anthropic.claude-sonnet-4-6"

    def test_sonnet45_resolves(self):
        assert (
            resolve_and_validate("sonnet45")
            == "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
        )

    def test_haiku45_resolves(self):
        assert (
            resolve_and_validate("haiku45")
            == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
        )

    def test_case_insensitive(self):
        """Aliases are case-insensitive (users may type OPUS48 or opus48)."""
        assert resolve_and_validate("OPUS48") == "global.anthropic.claude-opus-4-8"

    def test_bare_alias_no_longer_resolves(self):
        """Bare 'opus'/'sonnet'/'haiku' are removed — they no longer match an
        alias and (not matching any allowed pattern as a raw ID) are rejected."""
        assert resolve_and_validate("opus") is None
        assert resolve_and_validate("sonnet") is None
        assert resolve_and_validate("haiku") is None


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
        persona_allowed = ["global.anthropic.claude-sonnet-*"]
        result = resolve_and_validate("opus48", persona_allowed_models=persona_allowed)
        assert result is None  # Opus not in persona's allowed list

    def test_persona_allowed_models_permits(self):
        """Model that matches persona's pattern is allowed.

        Note (#2300): the resolved alias is a global.-prefixed inference
        profile, so the persona pattern must match that prefix.
        """
        persona_allowed = ["global.anthropic.claude-opus-*"]
        result = resolve_and_validate("opus46", persona_allowed_models=persona_allowed)
        assert result == "global.anthropic.claude-opus-4-6-v1"

    def test_tenant_patterns_used_when_persona_empty(self):
        """Tenant patterns are used when persona allowed_models is empty."""
        tenant_patterns = ["global.anthropic.claude-haiku-*"]
        result = resolve_and_validate(
            "haiku45", persona_allowed_models=None, tenant_patterns=tenant_patterns
        )
        assert result == "global.anthropic.claude-haiku-4-5-20251001-v1:0"

    def test_tenant_patterns_reject(self):
        """Tenant patterns can reject a model."""
        tenant_patterns = ["global.anthropic.claude-sonnet-*"]
        result = resolve_and_validate(
            "opus48", persona_allowed_models=None, tenant_patterns=tenant_patterns
        )
        assert result is None

    def test_default_patterns_allow_all_claude(self):
        """Default patterns allow all the version-pinned Claude aliases."""
        for alias in ["opus48", "opus46", "sonnet46", "sonnet45", "haiku45"]:
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
