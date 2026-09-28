"""Tests for chat_logging configuration parsing."""

import pytest

from src.chat_logging.config import ChatLoggingSettings, ScrubLevel


class TestScrubLevelCoercion:
    def test_none_string_coerced_to_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BG_CHAT_LOGGING_SCRUB_LEVEL", "none")
        settings = ChatLoggingSettings()
        assert settings.chat_logging_scrub_level == ScrubLevel.OFF

    @pytest.mark.parametrize("value", ["None", "NONE", "  none  "])
    def test_none_variants_coerced_to_off(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("BG_CHAT_LOGGING_SCRUB_LEVEL", value)
        settings = ChatLoggingSettings()
        assert settings.chat_logging_scrub_level == ScrubLevel.OFF

    @pytest.mark.parametrize("value,expected", [("off", ScrubLevel.OFF), ("basic", ScrubLevel.BASIC), ("standard", ScrubLevel.STANDARD)])
    def test_valid_values_pass_through(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: ScrubLevel) -> None:
        monkeypatch.setenv("BG_CHAT_LOGGING_SCRUB_LEVEL", value)
        settings = ChatLoggingSettings()
        assert settings.chat_logging_scrub_level == expected

    def test_invalid_values_still_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BG_CHAT_LOGGING_SCRUB_LEVEL", "foobar")
        with pytest.raises(ValueError):
            ChatLoggingSettings()
