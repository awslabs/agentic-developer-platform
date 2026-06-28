"""Model resolver for alias resolution and access control.

Implements US-9.6: Model Not Allowed - ensures users can only access permitted models.
"""

import fnmatch
import logging
from typing import Any

from src.shared.exceptions import ModelNotAllowedError
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger(__name__)


# Default model alias mappings
DEFAULT_MODEL_ALIASES: dict[str, str] = {
    # Short "latest" aliases for /model directive UX (issue #2279). Values MUST
    # be invocable inference-profile IDs (global./us. prefix) — bare
    # `anthropic.claude-*` IDs aren't supported for on-demand invocation and EOL
    # versions are rejected (issue #2300). Verified ACTIVE via list-inference-profiles.
    "opus": "global.anthropic.claude-opus-4-6-v1",
    "sonnet": "global.anthropic.claude-sonnet-4-6",
    "haiku": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    # Claude 3.5 models
    "claude-3.5-sonnet": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-sonnet": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-sonnet-latest": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-sonnet-v2": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3.5-haiku": "anthropic.claude-3-5-haiku-20241022-v1:0",
    "claude-3-5-haiku": "anthropic.claude-3-5-haiku-20241022-v1:0",
    # Claude 3 models
    "claude-3-opus": "anthropic.claude-3-opus-20240229-v1:0",
    "claude-3-sonnet": "anthropic.claude-3-sonnet-20240229-v1:0",
    "claude-3-haiku": "anthropic.claude-3-haiku-20240307-v1:0",
    # Claude 4 / Claude Sonnet 4 / Claude Opus 4 (latest)
    "claude-sonnet-4": "anthropic.claude-sonnet-4-20250514-v1:0",
    "claude-4-sonnet": "anthropic.claude-sonnet-4-20250514-v1:0",
    "claude-opus-4": "anthropic.claude-opus-4-20250514-v1:0",
    "claude-4-opus": "anthropic.claude-opus-4-20250514-v1:0",
    # Cross-region inference profiles (global — routes to optimal region automatically)
    "claude-3.5-sonnet-global": "global.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-sonnet-global": "global.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-haiku-global": "global.anthropic.claude-3-5-haiku-20241022-v1:0",
    "claude-3-opus-global": "global.anthropic.claude-3-opus-20240229-v1:0",
    "claude-3-sonnet-global": "global.anthropic.claude-3-sonnet-20240229-v1:0",
    "claude-3-haiku-global": "global.anthropic.claude-3-haiku-20240307-v1:0",
    "claude-sonnet-4-global": "global.anthropic.claude-sonnet-4-20250514-v1:0",
    "claude-opus-4-global": "global.anthropic.claude-opus-4-20250514-v1:0",
    # US-region inference profiles (stays within US regions)
    "claude-3.5-sonnet-us": "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-sonnet-us": "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-haiku-us": "us.anthropic.claude-3-5-haiku-20241022-v1:0",
    "claude-sonnet-4-us": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "claude-opus-4-us": "us.anthropic.claude-opus-4-20250514-v1:0",
    # EU-region inference profiles (stays within EU regions)
    "claude-3.5-sonnet-eu": "eu.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-sonnet-eu": "eu.anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-haiku-eu": "eu.anthropic.claude-3-5-haiku-20241022-v1:0",
    "claude-sonnet-4-eu": "eu.anthropic.claude-sonnet-4-20250514-v1:0",
    "claude-opus-4-eu": "eu.anthropic.claude-opus-4-20250514-v1:0",
    # Amazon Titan models
    "titan-text-express": "amazon.titan-text-express-v1",
    "titan-text-lite": "amazon.titan-text-lite-v1",
    "titan-text-premier": "amazon.titan-text-premier-v1:0",
    # Llama models
    "llama-3-70b": "meta.llama3-70b-instruct-v1:0",
    "llama-3-8b": "meta.llama3-8b-instruct-v1:0",
    # Mistral models
    "mistral-7b": "mistral.mistral-7b-instruct-v0:2",
    "mistral-large": "mistral.mistral-large-2402-v1:0",
    "mixtral-8x7b": "mistral.mixtral-8x7b-instruct-v0:1",
}


# Default allowed model patterns (can be overridden per org/team)
DEFAULT_ALLOWED_PATTERNS: list[str] = [
    "anthropic.claude-*",
    "amazon.titan-*",
    "meta.llama*",
    "mistral.*",
    # Cross-region inference profiles
    "global.anthropic.claude-*",
    "us.anthropic.claude-*",
    "eu.anthropic.claude-*",
]


class ModelResolver:
    """Resolves model aliases and checks model access permissions.

    Implements:
    - Model alias resolution (e.g., "claude-3.5-sonnet" -> Bedrock model ID)
    - Model access control per tenant (org/team)
    - US-9.6: Model Not Allowed error handling
    """

    def __init__(
        self,
        custom_aliases: dict[str, str] | None = None,
        allowed_models_config: dict[str, list[str]] | None = None,
    ) -> None:
        """Initialize the model resolver.

        Args:
            custom_aliases: Additional alias mappings to merge with defaults
            allowed_models_config: Per-tenant allowed model patterns
                                   Key: org_id or "org_id:team_id"
                                   Value: List of allowed model patterns (glob style)
        """
        self._aliases = {**DEFAULT_MODEL_ALIASES}
        if custom_aliases:
            self._aliases.update(custom_aliases)

        self._allowed_models_config = allowed_models_config or {}

    def resolve_model(self, model_name: str, org_id: str | None = None) -> str:
        """Resolve a model alias to its Bedrock model ID.

        Args:
            model_name: The model name or alias provided in the request
            org_id: Optional organization ID for org-specific aliases

        Returns:
            The resolved Bedrock model ID
        """
        # Check org-specific aliases first (future enhancement)
        # For now, use global aliases

        # Try to resolve alias
        resolved = self._aliases.get(model_name)
        if resolved:
            logger.debug(f"Resolved model alias '{model_name}' to '{resolved}'")
            return resolved

        # If not an alias, return as-is (assume it's a full Bedrock model ID)
        return model_name

    def map_to_bedrock_model(self, model: str) -> str:
        """Map a model name to its Bedrock model ID.

        This is an alias for resolve_model for API consistency.

        Args:
            model: The model name or alias

        Returns:
            The Bedrock model ID
        """
        return self.resolve_model(model)

    def is_model_allowed(self, model_id: str, context: TokenContext) -> bool:
        """Check if a model is allowed for the given context.

        Args:
            model_id: The resolved Bedrock model ID
            context: The token context with tenant information

        Returns:
            True if the model is allowed, False otherwise
        """
        allowed_patterns = self._get_allowed_patterns(context)

        for pattern in allowed_patterns:
            if fnmatch.fnmatch(model_id, pattern):
                logger.debug(f"Model '{model_id}' allowed by pattern '{pattern}'")
                return True

        logger.warning(f"Model '{model_id}' not allowed for org={context.org_id}, team={context.team_id}")
        return False

    def check_model_access(self, model_id: str, context: TokenContext) -> None:
        """Check model access and raise exception if not allowed.

        Args:
            model_id: The resolved Bedrock model ID
            context: The token context with tenant information

        Raises:
            ModelNotAllowedError: If the model is not allowed
        """
        if not self.is_model_allowed(model_id, context):
            allowed_models = self.get_allowed_models(context)
            raise ModelNotAllowedError(model=model_id, allowed_models=allowed_models)

    def get_allowed_models(self, context: TokenContext) -> list[str]:
        """Get list of allowed model patterns for the given context.

        Args:
            context: The token context with tenant information

        Returns:
            List of allowed model patterns (glob-style)
        """
        return self._get_allowed_patterns(context)

    def get_available_models(self, context: TokenContext) -> list[dict[str, Any]]:
        """Get list of available models for the given context.

        Returns models that the user has access to, including resolved aliases.

        Args:
            context: The token context with tenant information

        Returns:
            List of model information dictionaries
        """
        import time

        allowed_patterns = self._get_allowed_patterns(context)
        models = []

        # Add aliases that resolve to allowed models
        for alias, bedrock_id in self._aliases.items():
            for pattern in allowed_patterns:
                if fnmatch.fnmatch(bedrock_id, pattern):
                    models.append(
                        {
                            "id": alias,
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": self._get_model_owner(bedrock_id),
                        }
                    )
                    break

        return models

    def _get_allowed_patterns(self, context: TokenContext) -> list[str]:
        """Get allowed model patterns for the given context.

        Checks in order:
        1. Team-specific patterns (org_id:team_id)
        2. Org-specific patterns (org_id)
        3. Default patterns

        Args:
            context: The token context

        Returns:
            List of allowed model patterns
        """
        # Check team-specific config
        team_key = f"{context.org_id}:{context.team_id}"
        if team_key in self._allowed_models_config:
            return self._allowed_models_config[team_key]

        # Check org-specific config
        if context.org_id in self._allowed_models_config:
            return self._allowed_models_config[context.org_id]

        # Return defaults
        return DEFAULT_ALLOWED_PATTERNS

    def _get_model_owner(self, bedrock_model_id: str) -> str:
        """Extract the model owner from the Bedrock model ID.

        Args:
            bedrock_model_id: The Bedrock model ID

        Returns:
            The model owner/provider
        """
        if bedrock_model_id.startswith("anthropic."):
            return "anthropic"
        elif bedrock_model_id.startswith("amazon."):
            return "amazon"
        elif bedrock_model_id.startswith("meta."):
            return "meta"
        elif bedrock_model_id.startswith("mistral."):
            return "mistral"
        elif bedrock_model_id.startswith("cohere."):
            return "cohere"
        elif bedrock_model_id.startswith("ai21."):
            return "ai21"
        else:
            return "unknown"

    def add_alias(self, alias: str, bedrock_model_id: str) -> None:
        """Add a custom model alias.

        Args:
            alias: The alias name
            bedrock_model_id: The Bedrock model ID it maps to
        """
        self._aliases[alias] = bedrock_model_id

    def remove_alias(self, alias: str) -> bool:
        """Remove a custom model alias.

        Args:
            alias: The alias name to remove

        Returns:
            True if the alias was removed, False if it didn't exist
        """
        if alias in self._aliases:
            del self._aliases[alias]
            return True
        return False

    def set_allowed_models(self, key: str, patterns: list[str]) -> None:
        """Set allowed model patterns for an org or team.

        Args:
            key: Either org_id or "org_id:team_id"
            patterns: List of allowed model patterns (glob-style)
        """
        self._allowed_models_config[key] = patterns

    def get_all_aliases(self) -> dict[str, str]:
        """Get all model aliases.

        Returns:
            Dictionary mapping aliases to Bedrock model IDs
        """
        return dict(self._aliases)
