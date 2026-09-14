"""Live NUI-01 fixtures. Shared Cognito session restoration requires PR #5115.

The SPA restores sessionStorage on startup; hosted OAuth is a separate flow.
Failed feature reads and unexpected flag states are errors when enabled.
Ordinary repository runs skip only this package.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.e2e.chat.helpers import (
    CLOUDFRONT_URL,
    fetch_test_credentials,
    get_cognito_tokens,
    inject_tokens_and_navigate,
    take_failure_screenshot,
)

PACKAGE = Path(__file__).resolve().parent


class SessionTokens(dict):
    """Keep pytest fixture-argument diagnostics from printing credentials."""

    def __repr__(self):
        return "<in-memory Cognito tokens>"


def pytest_configure(config):
    config.addinivalue_line("markers", "preview_on: requires a verified live flag-on deployment")
    config.addinivalue_line("markers", "preview_off: requires a verified live flag-off deployment")


def pytest_collection_modifyitems(config, items):
    enabled = os.environ.get("E2E_NEW_UI_ENABLED", "").lower() in ("1", "true", "yes")
    expected = os.environ.get("E2E_NEW_UI_EXPECTED_FLAG", "")
    own_items = [item for item in items if Path(item.path).resolve().is_relative_to(PACKAGE)]
    if not own_items:
        return
    if not enabled:
        for item in own_items:
            item.add_marker(pytest.mark.skip(reason="Requires E2E_NEW_UI_ENABLED=1 and a deployed environment"))
        return
    if expected not in ("on", "off"):
        raise pytest.UsageError("Set E2E_NEW_UI_EXPECTED_FLAG=on or off explicitly for a live new-UI run")
    opposite = "preview_off" if expected == "on" else "preview_on"
    deselected = [item for item in own_items if item.get_closest_marker(opposite)]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item not in deselected]


@pytest.fixture(scope="session")
def base_url():
    return CLOUDFRONT_URL.rstrip("/")


@pytest.fixture(scope="session")
def cognito_tokens():
    return SessionTokens(get_cognito_tokens(fetch_test_credentials()))


@pytest.fixture(scope="session")
def browser_instance():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="session", autouse=True)
def deployed_features(browser_instance, cognito_tokens, base_url):
    """Verify the real deployment independently of later browser simulations."""
    context = browser_instance.new_context()
    try:
        response = context.request.get(
            base_url + "/api/features",
            headers={"Authorization": "Bearer " + cognito_tokens["access_token"]},
            timeout=30_000,
        )
        assert response.status == 200, f"Live features read failed: HTTP {response.status}"
        features = response.json().get("features")
        assert isinstance(features, dict), "Live features response has no features object"
        assert isinstance(features.get("new_ui"), bool), "Live new_ui flag is missing or not boolean"
        expected = os.environ["E2E_NEW_UI_EXPECTED_FLAG"] == "on"
        assert features["new_ui"] is expected, "Live preview flag differs from E2E_NEW_UI_EXPECTED_FLAG"
        return features
    finally:
        context.close()


@pytest.fixture
def page(browser_instance):
    context = browser_instance.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
    page = context.new_page()
    yield page
    context.close()


@pytest.fixture
def authed_page(page, cognito_tokens):
    """Real Cognito tokens restored by the shared helper; not hosted OAuth."""
    inject_tokens_and_navigate(page, cognito_tokens, path="/activity")
    yield page


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if Path(item.path).resolve().is_relative_to(PACKAGE) and report.when == "call" and report.failed:
        page = item.funcargs.get("page")
        if page:
            try:
                path = take_failure_screenshot(page, item.name)
                os.chmod(path, 0o600)
                report.sections.append(("Private failure screenshot", path))
            except Exception:
                pass
