"""Tests for chat_logging configuration parsing.

Issue #5672: the scrub level must resolve UPWARD on anything we cannot read as a
deliberate operator choice. Both earlier behaviours failed in the unsafe direction
— an unrecognised value raised and took the app down on startup, and the "none"
mis-spelling was mapped to OFF (no redaction at all). A configuration mistake must
never produce a transcript store with weaker protection than intended.
"""

import pytest

from src.chat_logging.config import ChatLoggingSettings, ScrubLevel


class TestScrubLevelFailsClosed:
    """Anything unreadable resolves to STANDARD, the strongest level."""

    def test_default_when_env_absent_is_standard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BG_CHAT_LOGGING_SCRUB_LEVEL", raising=False)
        settings = ChatLoggingSettings()
        assert settings.chat_logging_scrub_level == ScrubLevel.STANDARD

    @pytest.mark.parametrize("value", ["", "   ", "\t"])
    def test_empty_values_resolve_to_standard(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("BG_CHAT_LOGGING_SCRUB_LEVEL", value)
        settings = ChatLoggingSettings()
        assert settings.chat_logging_scrub_level == ScrubLevel.STANDARD

    @pytest.mark.parametrize("value", ["foobar", "OFF_PLEASE", "1", "strong", "minimal"])
    def test_unrecognised_values_resolve_to_standard(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        """Previously raised ValueError, which took the app down before any request landed."""
        monkeypatch.setenv("BG_CHAT_LOGGING_SCRUB_LEVEL", value)
        settings = ChatLoggingSettings()
        assert settings.chat_logging_scrub_level == ScrubLevel.STANDARD

    @pytest.mark.parametrize("value", ["none", "None", "NONE", "  none  "])
    def test_none_mis_spelling_no_longer_downgrades_to_off(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        """Regression: "none" was a synonym for OFF, so a single typo disabled redaction."""
        monkeypatch.setenv("BG_CHAT_LOGGING_SCRUB_LEVEL", value)
        settings = ChatLoggingSettings()
        assert settings.chat_logging_scrub_level == ScrubLevel.STANDARD

    def test_unrecognised_value_is_reported_at_warning(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """A silently-corrected misconfiguration is one nobody goes back and fixes."""
        monkeypatch.setenv("BG_CHAT_LOGGING_SCRUB_LEVEL", "wibble")
        with caplog.at_level("WARNING"):
            ChatLoggingSettings()
        assert "wibble" in caplog.text
        assert "standard" in caplog.text


class TestExplicitScrubLevelsHonoured:
    """A deliberate, reviewable choice still takes effect."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("off", ScrubLevel.OFF),
            ("basic", ScrubLevel.BASIC),
            ("standard", ScrubLevel.STANDARD),
            ("OFF", ScrubLevel.OFF),
            ("Basic", ScrubLevel.BASIC),
            ("  standard  ", ScrubLevel.STANDARD),
        ],
    )
    def test_valid_values_pass_through(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: ScrubLevel) -> None:
        monkeypatch.setenv("BG_CHAT_LOGGING_SCRUB_LEVEL", value)
        settings = ChatLoggingSettings()
        assert settings.chat_logging_scrub_level == expected
