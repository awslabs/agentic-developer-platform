"""
Authentication + WebSocket lifecycle tests (scenarios 1-3).

1. Login round-trip: Cognito hosted UI → dashboard, tokens in sessionStorage.
2. WebSocket opens after login: CDP observes wss:// connection on /chat.
3. No CSP refusal on WS connect: no Content-Security-Policy console errors.
"""

from __future__ import annotations

import time
import pytest

from urllib.parse import parse_qs, urlsplit

from .helpers import (
    CLOUDFRONT_URL,
    inject_tokens_and_navigate,
    login_via_cognito_hosted_ui,
    send_chat_message,
    take_failure_screenshot,
)


def _assert_stored_session(page):
    # Report only booleans, never token values, including on assertion failure.
    state = page.evaluate(
        """() => ({
            id_token: Boolean(sessionStorage.getItem('cognito_id_token')),
            access_token: Boolean(sessionStorage.getItem('cognito_access_token')),
            refresh_token: Boolean(sessionStorage.getItem('cognito_refresh_token')),
            jwt_shape: (sessionStorage.getItem('cognito_id_token') || '').split('.').length === 3,
            unexpired: Number(sessionStorage.getItem('cognito_token_expiry')) > Date.now()
        })"""
    )
    assert all(state.values()), f"Incomplete stored session: {state}"
    page.get_by_role("button", name="Logout", exact=True).wait_for(state="visible")


def _is_workspace_response(response):
    return urlsplit(response.url).path.endswith("/auth/workspaces")


@pytest.mark.chat_independent
class TestLoginRoundTrip:
    """Scenario 1: Full email-button → Cognito hosted-UI → OAuth callback flow."""

    def test_login_stores_tokens_in_session_storage(self, page, test_creds):
        """Require a real PKCE authorization, callback, and successful token exchange."""
        observed = {"pkce_authorization": False, "code_callback": False, "token_exchange": False}
        target = urlsplit(CLOUDFRONT_URL)

        def observe_request(request):
            url = urlsplit(request.url)
            query = parse_qs(url.query)
            if (url.hostname or "").endswith(".amazoncognito.com"):
                if query.get("response_type") == ["code"] and query.get("code_challenge"):
                    observed["pkce_authorization"] = True
            elif (url.scheme, url.netloc) == (target.scheme, target.netloc):
                if url.path == "/auth/callback" and query.get("code"):
                    observed["code_callback"] = True

        def observe_response(response):
            url = urlsplit(response.url)
            if (
                (url.hostname or "").endswith(".amazoncognito.com")
                and url.path == "/oauth2/token"
                and response.request.method == "POST"
                and response.status == 200
            ):
                observed["token_exchange"] = True

        page.on("request", observe_request)
        page.on("response", observe_response)
        with page.expect_response(_is_workspace_response) as workspaces:
            login_via_cognito_hosted_ui(page, test_creds)
        assert workspaces.value.status == 200, "OAuth session could not read workspaces"
        assert all(observed.values()), f"OAuth flow was not completed: {observed}"
        _assert_stored_session(page)


@pytest.mark.chat_independent
class TestSessionRestoration:
    """Stored-session startup is independent of the hosted OAuth login flow."""

    def test_supplied_tokens_restore_authenticated_session(self, page, cognito_tokens):
        with page.expect_response(_is_workspace_response) as workspaces:
            inject_tokens_and_navigate(page, cognito_tokens, path="/activity")
        assert workspaces.value.status == 200, "Restored session could not read workspaces"
        assert urlsplit(page.url).path == "/activity", "Restored session did not reach /activity"
        _assert_stored_session(page)
        supplied_tokens_preserved = page.evaluate(
            """tokens => ['id_token', 'access_token', 'refresh_token'].every(
                key => sessionStorage.getItem('cognito_' + key) === tokens[key])""",
            cognito_tokens,
        )
        assert supplied_tokens_preserved, "Restoration replaced or ignored supplied tokens"

        # Reload without injecting again: the normal AuthContext startup must work.
        with page.expect_response(_is_workspace_response) as reloaded_workspaces:
            page.reload(wait_until="domcontentloaded")
        assert reloaded_workspaces.value.status == 200, "Session did not survive reload"
        assert urlsplit(page.url).path == "/activity", "Reload returned to login"
        _assert_stored_session(page)


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
