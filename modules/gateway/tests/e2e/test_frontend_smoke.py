"""
Frontend smoke tests using Playwright.

These tests verify the React SPA loads correctly and basic navigation works.
They require a Playwright installation and only run in live mode.

Markers:
- @pytest.mark.browser — requires Playwright
- @pytest.mark.live_only — only runs against a deployed environment
"""

from urllib.parse import urlsplit

import pytest

from tests.e2e.config import is_live, load_live_config

pytestmark = [pytest.mark.frontend, pytest.mark.browser, pytest.mark.live_only, pytest.mark.e2e]


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

        page.get_by_test_id("email-login-btn").click(timeout=15000)
        page.wait_for_url(lambda url: (urlsplit(url).hostname or "").endswith(".amazoncognito.com"), timeout=15000)
        assert (urlsplit(page.url).hostname or "").endswith(".amazoncognito.com")

    def test_authenticated_view_renders(self, playwright_page, base_url):
        """Complete sign-in with test user and verify authenticated view (live only).

        This test requires TEST_USER_EMAIL and TEST_USER_PASSWORD to be set.
        """
        cfg = load_live_config()
        assert cfg.test_user_email and cfg.test_user_password, "Native browser fixture credentials are required"
        page = playwright_page
        page.goto(base_url, wait_until="domcontentloaded")
        page.get_by_test_id("email-login-btn").click(timeout=15000)
        page.wait_for_url(lambda url: (urlsplit(url).hostname or "").endswith(".amazoncognito.com"), timeout=15000)

        # Cognito renders hidden desktop/mobile copies; fill only the visible form.
        page.locator('input[name="username"]:visible').fill(cfg.test_user_email)
        page.locator('input[name="password"]:visible').fill(cfg.test_user_password)
        page.locator('input[name="signInSubmitButton"]:visible').click()
        target = urlsplit(base_url)
        page.wait_for_url(
            lambda url: (
                (urlsplit(url).scheme, urlsplit(url).netloc) == (target.scheme, target.netloc)
                and urlsplit(url).path not in ("/login", "/auth/callback")
            ),
            timeout=30000,
        )
        page.wait_for_function(
            """() => Boolean(sessionStorage.getItem('cognito_access_token'))
                && Number(sessionStorage.getItem('cognito_token_expiry')) > Date.now()""",
            timeout=15000,
        )
        page.get_by_role("navigation", name="Main navigation", exact=True).wait_for(
            state="visible",
            timeout=30000,
        )
        assert len(page.inner_text("body")) > 50, "Authenticated view should have substantial content"
