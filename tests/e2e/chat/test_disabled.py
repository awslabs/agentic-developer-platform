"""A disabled Chat deployment must enforce its route and navigation gate."""
from urllib.parse import urlsplit

import pytest

from .helpers import CLOUDFRONT_URL


@pytest.mark.chat_disabled
def test_disabled_chat_routes_and_navigation(authenticated_page):
    from playwright.sync_api import expect

    page = authenticated_page
    for path in ['/chat', '/my-chats']:
        page.goto(CLOUDFRONT_URL.rstrip('/') + path, wait_until='domcontentloaded')
        page.wait_for_url('**/runs', timeout=15_000)
        assert urlsplit(page.url).path == '/runs'
        expect(page.get_by_role('heading', name='Dashboard', exact=True)).to_be_visible()
        nav = page.locator('nav[aria-label="Main navigation"]:visible')
        expect(nav).to_have_count(1)
        expect(nav.locator('a[href="/chat"], a[href="/my-chats"]')).to_have_count(0)
