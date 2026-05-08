"""Tests for the provider registry and ORM validation.

Issue #537: Identity projection redesign — provider validation.
"""

import pytest

from src.shared.identity.providers import SUPPORTED_PROVIDERS, IdentityProvider
from src.shared.models.vault import UserIdentity


class TestProviderRegistry:
    """Tests for the canonical provider set."""

    def test_supported_providers_contains_required(self):
        """All required providers are in the set."""
        required = {"cognito", "github", "slack", "teams", "discord", "email", "whatsapp"}
        assert required == SUPPORTED_PROVIDERS

    def test_identity_provider_enum_values(self):
        """IdentityProvider enum has all expected members."""
        assert IdentityProvider.cognito == "cognito"
        assert IdentityProvider.github == "github"
        assert IdentityProvider.slack == "slack"
        assert IdentityProvider.teams == "teams"
        assert IdentityProvider.discord == "discord"
        assert IdentityProvider.email == "email"
        assert IdentityProvider.whatsapp == "whatsapp"

    def test_supported_providers_derived_from_enum(self):
        """SUPPORTED_PROVIDERS is derived from the enum (single source of truth)."""
        enum_values = frozenset(IdentityProvider)
        assert enum_values == SUPPORTED_PROVIDERS

    def test_supported_providers_is_frozen(self):
        """Cannot accidentally mutate the set."""
        with pytest.raises(AttributeError):
            SUPPORTED_PROVIDERS.add("bogus")


class TestUserIdentityProviderValidation:
    """Tests for ORM @validates('provider') on UserIdentity."""

    def test_valid_provider_accepted(self):
        """Valid provider values pass validation."""
        identity = UserIdentity()
        for provider in SUPPORTED_PROVIDERS:
            result = identity.validate_provider("provider", provider)
            assert result == provider

    def test_invalid_provider_rejected(self):
        """Invalid provider value raises ValueError."""
        identity = UserIdentity()
        with pytest.raises(ValueError, match="Unsupported provider"):
            identity.validate_provider("provider", "telegram")

    def test_empty_provider_rejected(self):
        """Empty string is rejected."""
        identity = UserIdentity()
        with pytest.raises(ValueError, match="Unsupported provider"):
            identity.validate_provider("provider", "")
