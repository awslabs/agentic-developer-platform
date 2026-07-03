"""Tests for ModelResolver component."""

import pytest

from src.proxy.model_resolver import DEFAULT_MODEL_ALIASES, ModelResolver
from src.shared.exceptions import ModelNotAllowedError
from src.shared.schemas.auth import TokenContext


class TestModelResolver:
    """Test cases for ModelResolver."""

    def test_resolve_known_alias(self, model_resolver: ModelResolver) -> None:
        """Test resolving a known model alias."""
        result = model_resolver.resolve_model("claude-3.5-sonnet")
        assert result == "anthropic.claude-3-5-sonnet-20241022-v2:0"

    def test_resolve_full_model_id(self, model_resolver: ModelResolver) -> None:
        """Test resolving a full Bedrock model ID (pass-through)."""
        model_id = "anthropic.claude-3-5-sonnet-20241022-v2:0"
        result = model_resolver.resolve_model(model_id)
        assert result == model_id

    def test_resolve_unknown_model(self, model_resolver: ModelResolver) -> None:
        """Test resolving an unknown model (pass-through)."""
        model_id = "some.unknown-model-v1"
        result = model_resolver.resolve_model(model_id)
        assert result == model_id

    def test_resolve_multiple_aliases(self, model_resolver: ModelResolver) -> None:
        """Test that multiple aliases resolve to the same model."""
        aliases = ["claude-3.5-sonnet", "claude-3-5-sonnet", "claude-3-5-sonnet-latest"]
        results = [model_resolver.resolve_model(alias) for alias in aliases]
        assert all(r == results[0] for r in results)

    def test_map_to_bedrock_model(self, model_resolver: ModelResolver) -> None:
        """Test map_to_bedrock_model is alias for resolve_model."""
        alias = "claude-3.5-sonnet"
        assert model_resolver.map_to_bedrock_model(alias) == model_resolver.resolve_model(alias)

    def test_is_model_allowed_default_patterns(self, model_resolver: ModelResolver, token_context: TokenContext) -> None:
        """Test model access check with default allowed patterns."""
        # Claude models should be allowed
        assert model_resolver.is_model_allowed("anthropic.claude-3-5-sonnet-20241022-v2:0", token_context)

        # Titan models should be allowed
        assert model_resolver.is_model_allowed("amazon.titan-text-express-v1", token_context)

        # Mistral models should be allowed
        assert model_resolver.is_model_allowed("mistral.mistral-7b-instruct-v0:2", token_context)

    def test_is_model_allowed_openai_default_2713(self, model_resolver: ModelResolver, token_context: TokenContext) -> None:
        """OpenAI models (bedrock-mantle passthrough) are in the default allowlist.

        #2713 C1: without an openai.* default, the /openai/v1/responses route's
        check_model_access would 403 every Codex run after cutover.
        """
        assert model_resolver.is_model_allowed("openai.gpt-5.5", token_context)

    def test_is_model_allowed_with_restrictions(self, model_resolver_with_restrictions: ModelResolver, token_context: TokenContext) -> None:
        """Test model access check with restricted patterns."""
        # test-org-456 only allows claude-3-5-sonnet-* models
        assert model_resolver_with_restrictions.is_model_allowed("anthropic.claude-3-5-sonnet-20241022-v2:0", token_context)

        # Other Claude models should not be allowed
        assert not model_resolver_with_restrictions.is_model_allowed("anthropic.claude-3-opus-20240229-v1:0", token_context)

        # Titan models should not be allowed for this org
        assert not model_resolver_with_restrictions.is_model_allowed("amazon.titan-text-express-v1", token_context)

    def test_check_model_access_allowed(self, model_resolver: ModelResolver, token_context: TokenContext) -> None:
        """Test check_model_access when model is allowed."""
        # Should not raise
        model_resolver.check_model_access("anthropic.claude-3-5-sonnet-20241022-v2:0", token_context)

    def test_check_model_access_denied(self, model_resolver_with_restrictions: ModelResolver) -> None:
        """Test check_model_access when model is not allowed (US-9.6)."""
        from datetime import datetime, timedelta

        # Create context for restricted org
        context = TokenContext(
            user_id="user1",
            org_id="restricted-org",
            team_id="team1",
            department_id="dept1",
            account_type="human",
            expires_at=datetime.now() + timedelta(hours=1),
        )

        # restricted-org only allows amazon.titan-* models
        with pytest.raises(ModelNotAllowedError) as exc_info:
            model_resolver_with_restrictions.check_model_access("anthropic.claude-3-5-sonnet-20241022-v2:0", context)

        assert exc_info.value.error == "model_not_allowed"
        assert "anthropic.claude-3-5-sonnet-20241022-v2:0" in str(exc_info.value.details)

    def test_get_allowed_models(self, model_resolver: ModelResolver, token_context: TokenContext) -> None:
        """Test get_allowed_models returns patterns."""
        patterns = model_resolver.get_allowed_models(token_context)
        assert isinstance(patterns, list)
        assert len(patterns) > 0
        assert "anthropic.claude-*" in patterns

    def test_get_available_models(self, model_resolver: ModelResolver, token_context: TokenContext) -> None:
        """Test get_available_models returns model list."""
        models = model_resolver.get_available_models(token_context)
        assert isinstance(models, list)
        assert len(models) > 0

        # Each model should have required fields
        for model in models:
            assert "id" in model
            assert "object" in model
            assert model["object"] == "model"
            assert "created" in model
            assert "owned_by" in model

    def test_add_custom_alias(self, model_resolver: ModelResolver) -> None:
        """Test adding a custom model alias."""
        model_resolver.add_alias("my-custom-model", "anthropic.custom-model-v1")
        result = model_resolver.resolve_model("my-custom-model")
        assert result == "anthropic.custom-model-v1"

    def test_remove_alias(self, model_resolver: ModelResolver) -> None:
        """Test removing a model alias."""
        # Add and then remove
        model_resolver.add_alias("temp-alias", "some.model-v1")
        assert model_resolver.resolve_model("temp-alias") == "some.model-v1"

        result = model_resolver.remove_alias("temp-alias")
        assert result is True

        # After removal, should pass through
        assert model_resolver.resolve_model("temp-alias") == "temp-alias"

    def test_remove_nonexistent_alias(self, model_resolver: ModelResolver) -> None:
        """Test removing a non-existent alias."""
        result = model_resolver.remove_alias("nonexistent-alias")
        assert result is False

    def test_set_allowed_models_for_org(self, model_resolver: ModelResolver) -> None:
        """Test setting allowed models for an org."""
        from datetime import datetime, timedelta

        context = TokenContext(
            user_id="user1",
            org_id="new-org",
            team_id="team1",
            department_id="dept1",
            account_type="human",
            expires_at=datetime.now() + timedelta(hours=1),
        )

        # Set restricted patterns for new-org
        model_resolver.set_allowed_models("new-org", ["amazon.titan-*"])

        # Now only titan models should be allowed
        assert model_resolver.is_model_allowed("amazon.titan-text-express-v1", context)
        assert not model_resolver.is_model_allowed("anthropic.claude-3-5-sonnet-20241022-v2:0", context)

    def test_set_allowed_models_for_team(self, model_resolver: ModelResolver) -> None:
        """Test setting allowed models for a specific team."""
        from datetime import datetime, timedelta

        context = TokenContext(
            user_id="user1",
            org_id="org1",
            team_id="restricted-team",
            department_id="dept1",
            account_type="human",
            expires_at=datetime.now() + timedelta(hours=1),
        )

        # Set patterns for org
        model_resolver.set_allowed_models("org1", ["anthropic.*", "amazon.*"])

        # Set more restrictive patterns for team
        model_resolver.set_allowed_models("org1:restricted-team", ["amazon.titan-text-lite-*"])

        # Team should only have access to titan-text-lite
        assert model_resolver.is_model_allowed("amazon.titan-text-lite-v1", context)
        assert not model_resolver.is_model_allowed("amazon.titan-text-express-v1", context)
        assert not model_resolver.is_model_allowed("anthropic.claude-3-5-sonnet-20241022-v2:0", context)

    def test_get_all_aliases(self, model_resolver: ModelResolver) -> None:
        """Test get_all_aliases returns all mappings."""
        aliases = model_resolver.get_all_aliases()
        assert isinstance(aliases, dict)
        assert "claude-3.5-sonnet" in aliases
        assert aliases["claude-3.5-sonnet"] == DEFAULT_MODEL_ALIASES["claude-3.5-sonnet"]

    def test_custom_aliases_in_constructor(self) -> None:
        """Test providing custom aliases in constructor."""
        custom = {"my-model": "provider.my-model-v1"}
        resolver = ModelResolver(custom_aliases=custom)

        assert resolver.resolve_model("my-model") == "provider.my-model-v1"
        # Default aliases should still work
        assert resolver.resolve_model("claude-3.5-sonnet") == DEFAULT_MODEL_ALIASES["claude-3.5-sonnet"]

    def test_model_owner_extraction(self, model_resolver: ModelResolver) -> None:
        """Test model owner is correctly extracted."""
        from datetime import datetime, timedelta

        context = TokenContext(
            user_id="user1",
            org_id="test-org",
            team_id="team1",
            department_id="dept1",
            account_type="human",
            expires_at=datetime.now() + timedelta(hours=1),
        )

        models = model_resolver.get_available_models(context)

        # Find models and check owners
        owners = {m["id"]: m["owned_by"] for m in models}

        # Check some known aliases
        if "claude-3.5-sonnet" in owners:
            assert owners["claude-3.5-sonnet"] == "anthropic"
        if "titan-text-express" in owners:
            assert owners["titan-text-express"] == "amazon"


class TestModelResolverEdgeCases:
    """Edge case tests for ModelResolver."""

    def test_empty_model_name(self, model_resolver: ModelResolver) -> None:
        """Test handling empty model name."""
        result = model_resolver.resolve_model("")
        assert result == ""

    def test_whitespace_model_name(self, model_resolver: ModelResolver) -> None:
        """Test handling whitespace in model name."""
        result = model_resolver.resolve_model("  claude-3.5-sonnet  ")
        # Without trimming, it won't match the alias
        assert result == "  claude-3.5-sonnet  "

    def test_case_sensitive_aliases(self, model_resolver: ModelResolver) -> None:
        """Test that aliases are case-sensitive."""
        # Exact case should work
        result = model_resolver.resolve_model("claude-3.5-sonnet")
        assert result == "anthropic.claude-3-5-sonnet-20241022-v2:0"

        # Different case should not match (pass through)
        result = model_resolver.resolve_model("Claude-3.5-Sonnet")
        assert result == "Claude-3.5-Sonnet"

    def test_glob_pattern_matching(self, model_resolver: ModelResolver, token_context: TokenContext) -> None:
        """Test glob pattern matching for allowed models."""
        # Pattern "anthropic.claude-*" should match various Claude models
        assert model_resolver.is_model_allowed("anthropic.claude-3-opus-20240229-v1:0", token_context)
        assert model_resolver.is_model_allowed("anthropic.claude-3-5-sonnet-20241022-v2:0", token_context)
        assert model_resolver.is_model_allowed("anthropic.claude-instant-v1", token_context)

        # But not non-Claude Anthropic models (if they existed)
        # This tests the pattern specificity
