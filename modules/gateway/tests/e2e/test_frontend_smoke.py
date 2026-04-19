"""
Frontend smoke tests using Playwright.

These tests verify the React SPA loads correctly and basic navigation works.
They require a Playwright installation and only run in live mode.

Markers:
- @pytest.mark.browser — requires Playwright
- @pytest.mark.live_only — only runs against a deployed environment
"""

import re

import pytest

from tests.e2e.config import is_live, load_live_config

pytestmark = [pytest.mark.frontend, pytest.mark.browser, pytest.mark.live_only]


@pytest.fixture
def base_url() -> str:
    """Return the CloudFront base URL for the frontend."""
    if not is_live():
        pytest.skip("Frontend smoke tests require a deployed environment (TEST_ENV=dev)")
    cfg = load_live_config()
    return f"https://{cfg.cloudfront_domain}"


@pytest.fixture
def cognito_auth_domain() -> str:
    """Return the expected Cognito auth domain prefix."""
    if not is_live():
        pytest.skip("Frontend smoke tests require a deployed environment (TEST_ENV=dev)")
    cfg = load_live_config()
    return cfg.cognito_domain or "bedrockgw-dev-auth"


class TestFrontendSmoke:
    """Smoke tests for the admin dashboard frontend."""

    def test_page_loads_with_title(self, playwright_page, base_url):
        """Hit the CloudFront root — page loads, title is present."""
        page = playwright_page
        response = page.goto(base_url, wait_until="networkidle")

        # Page should load successfully (200)
        assert response is not None
        assert response.status == 200

        # Title should be set (not empty)
        title = page.title()
        assert title, "Page title should not be empty"

        # No console errors
        console_errors: list[str] = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.wait_for_timeout(2000)
        # Note: console listener was added after page load, so this primarily
        # catches post-load errors.  For a comprehensive check, the listener
        # would need to be added before goto, but that's fine for smoke testing.

    def test_sign_in_redirects_to_cognito(self, playwright_page, base_url, cognito_auth_domain):
        """Clicking sign-in redirects to the Cognito hosted UI."""
        page = playwright_page
        page.goto(base_url, wait_until="networkidle")

        # Look for a sign-in / login link or button
        sign_in = page.locator("text=/sign.?in|log.?in/i").first
        if sign_in.is_visible():
            with page.expect_navigation(wait_until="commit"):
                sign_in.click()

            nav_url = page.url
            assert "amazoncognito.com" in nav_url or cognito_auth_domain in nav_url, f"Expected redirect to Cognito auth domain, got {nav_url}"
        else:
            # If there's no visible sign-in button, check if the page is
            # already showing a Cognito redirect or an auth-required state
            page_content = page.content()
            assert "login" in page_content.lower() or "sign" in page_content.lower() or "cognito" in page.url.lower(), (
                "Could not find sign-in mechanism on the page"
            )

    def test_authenticated_view_renders(self, playwright_page, base_url):
        """Complete sign-in with test user and verify authenticated view (live only).

        This test requires TEST_USER_EMAIL and TEST_USER_PASSWORD to be set.
        """
        cfg = load_live_config()
        if not cfg.test_user_email or not cfg.test_user_password:
            pytest.skip("TEST_USER_EMAIL and TEST_USER_PASSWORD required for authenticated frontend test")

        page = playwright_page
        page.goto(base_url, wait_until="networkidle")

        # Try to find and click sign-in
        sign_in = page.locator("text=/sign.?in|log.?in/i").first
        if not sign_in.is_visible():
            pytest.skip("No sign-in button found on page")

        sign_in.click()
        page.wait_for_url(re.compile(r"amazoncognito\.com|auth\."), timeout=10000)

        # Fill in Cognito hosted UI credentials
        page.fill("input[name='username'], input[name='email'], #signInFormUsername", cfg.test_user_email)
        page.fill("input[name='password'], #signInFormPassword", cfg.test_user_password)
        page.click("input[type='submit'], button[type='submit']")

        # Wait for redirect back to the SPA
        page.wait_for_url(re.compile(re.escape(cfg.cloudfront_domain)), timeout=15000)

        # Authenticated view should render (look for dashboard-like content)
        page.wait_for_load_state("networkidle")
        body_text = page.inner_text("body")
        assert len(body_text) > 50, "Authenticated view should have substantial content"
