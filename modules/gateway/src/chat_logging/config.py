"""Chat logging configuration.

Issue #143: Configuration settings for async chat logging with PII scrubbing.
"""

from enum import StrEnum

from pydantic_settings import BaseSettings


class ScrubLevel(StrEnum):
    """Chat logging scrubbing level configuration."""

    OFF = "off"  # No scrubbing (for debugging only, not for production)
    BASIC = "basic"  # Headers + regex only (fast, no AWS API calls)
    STANDARD = "standard"  # Headers + regex + Comprehend PII detection (recommended)


class ChatLoggingSettings(BaseSettings):
    """Chat logging specific settings.

    Async chat logging for proxy requests - captures full conversation logs
    (prompts + responses) with sensitive data scrubbing, stored in S3.
    """

    chat_logging_enabled: bool = False  # Enable/disable chat logging
    chat_logging_bucket: str = ""  # S3 bucket name for chat logs
    chat_logging_scrub_level: ScrubLevel = ScrubLevel.STANDARD  # Scrubbing level: off|basic|standard
    chat_logging_exclude_models: str = ""  # Comma-separated list of models to skip logging

    model_config = {"env_prefix": "BG_", "env_file": ".env"}

    @property
    def chat_logging_exclude_models_list(self) -> list[str]:
        """Get list of excluded models from comma-separated string."""
        if not self.chat_logging_exclude_models:
            return []
        return [m.strip() for m in self.chat_logging_exclude_models.split(",") if m.strip()]


def get_chat_logging_settings() -> ChatLoggingSettings:
    """Get chat logging settings instance."""
    return ChatLoggingSettings()
