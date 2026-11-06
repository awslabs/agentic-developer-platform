"""The Crew action stays hidden until its complete deployment is enabled."""

import pytest

from src.features.routes import get_features


@pytest.mark.asyncio
async def test_crew_dashboard_requires_assistant_proxy_and_origin(monkeypatch):
    for name in ("FEATURE_CREW_DASHBOARD", "FEATURE_ASSISTANT_UI_ENABLED", "CREW_PROXY_ENABLED", "CREW_DASHBOARD_ORIGIN"):
        monkeypatch.delenv(name, raising=False)
    assert (await get_features())["features"]["crew_dashboard"] is False

    monkeypatch.setenv("FEATURE_CREW_DASHBOARD", "true")
    monkeypatch.setenv("FEATURE_ASSISTANT_UI_ENABLED", "true")
    monkeypatch.setenv("CREW_PROXY_ENABLED", "true")
    monkeypatch.setenv("CREW_DASHBOARD_ORIGIN", "https://crew.example.test")
    assert (await get_features())["features"]["crew_dashboard"] is True

    for name in ("FEATURE_CREW_DASHBOARD", "FEATURE_ASSISTANT_UI_ENABLED", "CREW_PROXY_ENABLED", "CREW_DASHBOARD_ORIGIN"):
        monkeypatch.delenv(name)
        assert (await get_features())["features"]["crew_dashboard"] is False
        monkeypatch.setenv(name, "https://crew.example.test" if name == "CREW_DASHBOARD_ORIGIN" else "true")
