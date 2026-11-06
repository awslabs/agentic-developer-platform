import asyncio

from src.features.routes import get_features


def test_assistant_entry_defaults_off_and_requires_chat(monkeypatch):
    monkeypatch.delenv("FEATURE_ASSISTANT_UI_ENABLED", raising=False)
    monkeypatch.delenv("FEATURE_CHAT_ENABLED", raising=False)
    assert asyncio.run(get_features())["features"]["assistant_ui"] is False

    monkeypatch.setenv("FEATURE_ASSISTANT_UI_ENABLED", "true")
    monkeypatch.setenv("FEATURE_CHAT_ENABLED", "false")
    assert asyncio.run(get_features())["features"]["assistant_ui"] is False

    monkeypatch.setenv("FEATURE_CHAT_ENABLED", "true")
    assert asyncio.run(get_features())["features"]["assistant_ui"] is True

    monkeypatch.setenv("FEATURE_ASSISTANT_UI_ENABLED", "yes")
    assert asyncio.run(get_features())["features"]["assistant_ui"] is False
