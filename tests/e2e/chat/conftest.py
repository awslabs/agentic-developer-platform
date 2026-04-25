"""
Shared fixtures for the Playwright E2E chat regression suite.

Provides:
- test_creds: Cognito test credentials from Secrets Manager
- cognito_tokens: programmatically fetched Cognito tokens
- browser/page fixtures with Playwright (sync API)
- authenticated_page: page logged in and navigated to /chat
- cw_route_poller: CloudWatch ingest-route polling helper
"""

from __future__ import annotations

import os
import time
from typing import Generator

import pytest

from .helpers import (
    CLOUDFRONT_URL,
    fetch_test_credentials,
    get_cognito_tokens,
    inject_tokens_and_navigate,
    poll_ingest_route,
    take_failure_screenshot,
    TestCredentials,
)


# ---------------------------------------------------------------------------
# Skip unless live
# ---------------------------------------------------------------------------

def pytest_collection_modifyitems(config, items):
    """Auto-skip all tests in this package unless E2E_CHAT_ENABLED=1."""
    enabled = os.environ.get("E2E_CHAT_ENABLED", "").lower() in ("1", "true", "yes")
    skip_marker = pytest.mark.skip(
        reason="E2E chat tests require E2E_CHAT_ENABLED=1 and AWS credentials"
    )
    for item in items:
        if not enabled:
            item.add_marker(skip_marker)


# ---------------------------------------------------------------------------
# Credentials (session-scoped — fetched once)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def test_creds() -> TestCredentials:
    """Cognito test user credentials from Secrets Manager."""
    return fetch_test_credentials()


@pytest.fixture(scope="session")
def cognito_tokens(test_creds: TestCredentials) -> dict[str, str]:
    """Programmatically-obtained Cognito tokens (id, access, refresh)."""
    return get_cognito_tokens(test_creds)


# ---------------------------------------------------------------------------
# Playwright browser fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def browser_instance():
    """Session-scoped Playwright browser (Chromium, headless)."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    yield browser
    browser.close()
    pw.stop()


@pytest.fixture
def page(browser_instance):
    """Fresh browser page (new context per test for isolation)."""
    context = browser_instance.new_context(
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True,
    )
    pg = context.new_page()
    yield pg
    pg.close()
    context.close()


@pytest.fixture
def authenticated_page(page, test_creds):
    """Page logged in via the real Cognito hosted-UI OAuth flow, navigated to /chat.

    The hosted-UI flow is the only reliable path for this SPA — sessionStorage
    token injection alone doesn't work because the AuthContext's user state
    is built in the OAuth callback handler, not reconstructed from
    sessionStorage on arbitrary mount.

    Slower than injection (~6s vs ~1s) but it actually produces a working
    authed session.
    """
    from .helpers import login_via_cognito_hosted_ui

    login_via_cognito_hosted_ui(page, test_creds)
    page.goto(f"{CLOUDFRONT_URL}/chat", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(2000)  # let WS + React settle
    return page


# ---------------------------------------------------------------------------
# CDP-enabled page for WebSocket observation
# ---------------------------------------------------------------------------

@pytest.fixture
def cdp_page(browser_instance):
    """Page with CDP session for WebSocket/network observation."""
    context = browser_instance.new_context(
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True,
    )
    pg = context.new_page()
    cdp = pg.context.new_cdp_session(pg)
    cdp.send("Network.enable")
    yield pg, cdp
    pg.close()
    context.close()


# ---------------------------------------------------------------------------
# CloudWatch helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def cw_route_poller():
    """Fixture returning the poll_ingest_route function."""
    return poll_ingest_route


# ---------------------------------------------------------------------------
# Failure screenshot hook
# ---------------------------------------------------------------------------

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Auto-capture screenshot on test failure."""
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" and rep.failed:
        # Try to get the page fixture
        page = item.funcargs.get("page") or item.funcargs.get("authenticated_page")
        if page:
            try:
                name = item.nodeid.replace("/", "_").replace("::", "__")
                path = take_failure_screenshot(page, name)
                rep.extra_info = getattr(rep, "extra_info", {})
                rep.extra_info["screenshot"] = path
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Timestamp tracker for route correlation
# ---------------------------------------------------------------------------

@pytest.fixture
def send_timestamp():
    """Returns a callable that records the current time (for CW log correlation)."""
    timestamps: dict[str, float] = {}

    class Tracker:
        def mark(self, name: str = "default") -> float:
            t = time.time()
            timestamps[name] = t
            return t

        def get(self, name: str = "default") -> float:
            return timestamps.get(name, 0.0)

    return Tracker()
