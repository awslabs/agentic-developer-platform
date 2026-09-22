"""Chat logging configuration.

Issue #143: Configuration settings for async chat logging with PII scrubbing.
"""

import logging
from enum import StrEnum
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class ScrubLevel(StrEnum):
    """Chat logging scrubbing level configuration."""

    OFF = "off"  # No scrubbing (for debugging only, not for production)
    BASIC = "basic"  # Headers + regex only (fast, no AWS API calls)
    STANDARD = "standard"  # Headers + regex + Comprehend PII detection (recommended)


_VALID_SCRUB_LEVELS = frozenset(level.value for level in ScrubLevel)


class ChatLoggingSettings(BaseSettings):
    """Chat logging specific settings.

    Async chat logging for proxy requests - captures full conversation logs
    (prompts + responses) with sensitive data scrubbing, stored in S3.
    """

    chat_logging_enabled: bool = False  # Enable/disable chat logging
    chat_logging_bucket: str = ""  # S3 bucket name for chat logs
    chat_logging_scrub_level: ScrubLevel = ScrubLevel.STANDARD  # Scrubbing level: off|basic|standard
    chat_logging_exclude_models: str = ""  # Comma-separated list of models to skip logging

    # Issue #5672: resolve upward, never downward.
    #
    # Two earlier behaviours both failed in the unsafe direction. An empty or
    # unrecognised value raised a Pydantic enum error and took the whole app down
    # on startup, before a single request landed. And "none" — a common operator
    # mis-spelling of "off" — was accepted as a synonym for OFF, so a typo in a
    # configmap silently turned transcript redaction off entirely.
    #
    # Anything we cannot read as a deliberate choice now resolves to STANDARD,
    # the strongest level, and says so at WARNING. That keeps the no-crash
    # property the "none" synonym was added for while removing the downgrade:
    # a misconfiguration costs Comprehend calls, not user privacy.
    #
    # An operator who explicitly writes "off" or "basic" still gets exactly that.
    # Weakening redaction has to be a deliberate, reviewable act.
    @field_validator("chat_logging_scrub_level", mode="before")
    @classmethod
    def _coerce_scrub_level(cls, v: Any) -> Any:
        if isinstance(v, ScrubLevel):
            return v
        if v is None:
            logger.warning("BG_CHAT_LOGGING_SCRUB_LEVEL is unset; defaulting to '%s' (fail-closed)", ScrubLevel.STANDARD.value)
            return ScrubLevel.STANDARD
        if isinstance(v, str):
            normalized = v.strip().lower()
            if normalized in _VALID_SCRUB_LEVELS:
                return ScrubLevel(normalized)
            logger.warning(
                "BG_CHAT_LOGGING_SCRUB_LEVEL=%r is not one of %s; defaulting to '%s' (fail-closed)",
                v,
                sorted(_VALID_SCRUB_LEVELS),
                ScrubLevel.STANDARD.value,
            )
            return ScrubLevel.STANDARD
        logger.warning(
            "BG_CHAT_LOGGING_SCRUB_LEVEL has non-string type %s; defaulting to '%s' (fail-closed)",
            type(v).__name__,
            ScrubLevel.STANDARD.value,
        )
        return ScrubLevel.STANDARD

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
