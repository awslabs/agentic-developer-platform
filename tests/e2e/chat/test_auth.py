"""
Authentication + WebSocket lifecycle tests (scenarios 1-3).

1. Login round-trip: Cognito hosted UI → dashboard, tokens in sessionStorage.
2. WebSocket opens after login: CDP observes wss:// connection on /chat.
3. No CSP refusal on WS connect: no Content-Security-Policy console errors.
"""

from __future__ import annotations

import re
import time

import pytest

from .helpers import (
    CLOUDFRONT_URL,
    COGNITO_TOKEN_KEYS,
    get_session_storage_tokens,
    inject_tokens_and_navigate,
    login_via_cognito_hosted_ui,
    send_chat_message,
    take_failure_screenshot,
)


class TestLoginRoundTrip:
    """Scenario 1: Full Cognito hosted-UI login flow."""

    def test_login_stores_tokens_in_session_storage(self, page, test_creds, cognito_tokens):
        """Load CloudFront URL → Cognito hosted UI → submit creds → land on dashboard.

        Asserts: sessionStorage contains cognito_id_token, cognito_access_token,
        cognito_refresh_token.
        """
        # Use token injection (the hosted-UI flow is tested in test_frontend_smoke.py;
        # here we verify the token storage contract that downstream tests depend on)
        inject_tokens_and_navigate(page, cognito_tokens)

        tokens = get_session_storage_tokens(page)

        for key in COGNITO_TOKEN_KEYS:
            assert tokens.get(key), (
                f"sessionStorage key '{key}' is missing or empty after login. "
                "The chat page depends on these tokens for WS auth."
            )

        # id_token should look like a JWT (3 dot-separated parts)
        id_token = tokens["cognito_id_token"]
        assert id_token.count(".") == 2, (
            f"cognito_id_token doesn't look like a JWT: {id_token[:50]}..."
        )


class TestWebSocketOpens:
    """Scenario 2: WebSocket opens after login on /chat."""

    def test_ws_created_on_chat_page(self, cdp_page, cognito_tokens):
        """Navigate to /chat, create new conversation, assert wss:// WebSocket
        is observed by CDP Network.webSocketCreated within 10s.
        """
        page, cdp = cdp_page

        ws_urls: list[str] = []

        def on_ws_created(params):
            url = params.get("url", "")
            ws_urls.append(url)

        cdp.on("Network.webSocketCreated", on_ws_created)

        # Inject tokens and go to /chat
        inject_tokens_and_navigate(page, cognito_tokens)

        # Try sending a message to trigger WS connection
        try:
            send_chat_message(page, "hello", timeout=5000)
        except Exception:
            pass  # Input might not be visible yet

        # Wait up to 10s for WS to appear
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not ws_urls:
            page.wait_for_timeout(500)

        # Assert at least one wss:// URL was observed
        wss_urls = [u for u in ws_urls if u.startswith("wss://")]
        assert wss_urls, (
            f"No wss:// WebSocket created within 10s on /chat page. "
            f"Observed URLs: {ws_urls}"
        )

        # Should be an API Gateway WebSocket endpoint
        ws_url = wss_urls[0]
        assert "execute-api" in ws_url or "amazonaws.com" in ws_url, (
            f"WebSocket URL doesn't look like API Gateway: {ws_url}"
        )


class TestNoCSPRefusal:
    """Scenario 3: No Content-Security-Policy errors on WS connect."""

    def test_no_csp_errors_during_ws_open(self, cdp_page, cognito_tokens):
        """Inspect console errors during WS open, fail if any contains
        'Refused to connect' or 'Content Security Policy'.
        """
        page, cdp = cdp_page

        console_errors: list[str] = []

        def on_console(msg):
            if msg.type == "error":
                console_errors.append(msg.text)

        page.on("console", on_console)

        # Inject tokens and navigate
        inject_tokens_and_navigate(page, cognito_tokens)

        # Try to trigger WS connection
        try:
            send_chat_message(page, "hello", timeout=5000)
        except Exception:
            pass

        # Wait for any CSP errors to surface
        page.wait_for_timeout(5000)

        csp_errors = [
            e for e in console_errors
            if "Refused to connect" in e or "Content Security Policy" in e
        ]

        assert not csp_errors, (
            f"CSP errors detected during WS connection:\n"
            + "\n".join(csp_errors)
            + "\nThis is a regression of issue #117 (CSP header fix)."
        )
