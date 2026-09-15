"""Real deployed shell navigation plus separately labeled browser simulations.

No application records, users, or deployment flags are changed. The preview is
a shell linking to legacy screens; these tests do not claim migrated-page parity.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import pytest

# The current UI renders <nav aria-label="Main navigation"> twice on every page:
# the desktop sidebar in `MainLayout` and a second copy inside `MobileNav`'s
# slide-out panel, which stays in the DOM at desktop widths and is only hidden by
# `lg:hidden`. Both predate the preview shell. Assertions must therefore address
# the ONE landmark the user can actually see, not "the" landmark: a bare selector
# resolves to two elements and Playwright's strict mode raises instead of
# asserting, and `.first` is worse than useless here because index 0 is the
# HIDDEN mobile copy.
CURRENT_NAV = 'nav[aria-label="Main navigation"]:visible'

# The preview's navigation landmark. Scoped to the visible one for the same reason
# CURRENT_NAV is: `NextLayout` renders `NextNav` twice — the desktop sidebar and a
# second copy inside the mobile drawer. The drawer is only mounted while open, so
# today a bare selector happens to resolve to one element, but relying on that makes
# this assertion depend on a mounting detail rather than on what the user can see.
PREVIEW_NAV = '[data-testid="next-nav"]:visible'

# Which journey owns each route under test. The preview splits navigation into two
# journeys, so no single view offers all four: asserting them all against whichever
# journey happens to be active fails for the two that live in the other one. The
# journey switch exposes `next-journey-<id>` and marks the active tab with
# `aria-current="page"`.
PREVIEW_JOURNEYS = ("use", "admin")
ROUTE_JOURNEY = {
    "/activity": "use",
    "/settings/connections": "use",
    "/budgets": "admin",
    "/ratelimits": "admin",
}

CURRENT_PAGES = [
    ("/runs", "Dashboard", "/api/me/agent-run-stats"),
    ("/activity", "Agent Activity", "/api/me/agent-invocations"),
    ("/budgets", "Budget Management", "/api/budget/person-default/platform"),
    ("/settings/connections", "Connections", "/api/admin/connections"),
]


def expect(actual):
    # Keep disabled-package collection possible without Playwright installed.
    from playwright.sync_api import expect as playwright_expect

    return playwright_expect(actual)


def _goto(page, base_url, path):
    page.goto(base_url + path, wait_until="domcontentloaded", timeout=30_000)


def _current(page):
    # Exactly one visible landmark, not "at least one": 0 means the current UI did
    # not render, and >1 would mean the desktop and mobile copies are on screen at
    # once. Both are real failures, so neither is allowed to pass. The fixtures use
    # a desktop viewport (1440x1000), where the mobile copy is display:none.
    expect(page.locator(CURRENT_NAV)).to_have_count(1, timeout=15_000)
    expect(page.get_by_test_id("next-layout")).to_have_count(0)
    assert not urlsplit(page.url).path.startswith("/next"), "Expected a current-UI route"


def _preview(page, base_url, path="/next", home=True):
    expect(page).to_have_url(base_url + path, timeout=15_000)
    expect(page.get_by_test_id("next-layout")).to_be_visible()
    expect(page.get_by_test_id("back-to-current-ui")).to_be_visible()
    if home:
        expect(page.get_by_test_id("next-home")).to_be_visible()


def _select_journey(page, journey):
    """Show `journey`'s navigation, or report that it is not offered to this actor.

    Returns False when the switch does not offer the journey. That is a legitimate
    state, not a failure: `canEnterAdministration` withholds Administration entirely
    when every administration predicate gates out, and `JourneySwitch` then renders
    nothing at all rather than a lone tab. The caller decides whether a journey being
    absent contradicts what the current nav offered.
    """
    tab = page.locator(f'[data-testid="next-journey-{journey}"]:visible')
    if tab.count() == 0:
        # No switch means only Use ADP is offered, and the preview opens inside it.
        if journey != "use":
            return False
    else:
        tab.click()
        expect(tab).to_have_attribute("aria-current", "page", timeout=15_000)
    expect(page.locator(PREVIEW_NAV)).to_have_count(1, timeout=15_000)
    return True


def _preview_counts(page, routes):
    """Links per route inside the one visible preview nav."""
    nav = page.locator(PREVIEW_NAV)
    expect(nav).to_have_count(1, timeout=15_000)
    return {route: nav.locator(f'a[href="{route}"]').count() for route in routes}


def _identity(page):
    """Return identity/context claims in memory, never authentication tokens."""
    page.wait_for_function(
        """() => document.querySelector('#workspace-selector')
        ?.closest('[aria-busy]')?.getAttribute('aria-busy') === 'false'""",
        timeout=15_000,
    )
    state = page.evaluate("""() => {
        const token = sessionStorage.getItem('cognito_id_token');
        if (!token) return null;
        const value = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
        const c = JSON.parse(atob(value));
        return {sub:c.sub, org:c['custom:org_id'] ?? null, team:c['custom:team_id'] ?? null,
                selected:document.querySelector('#workspace-selector').value};
    }""")
    assert state and state["sub"], "Authenticated identity missing"
    return state


def _login(page, base_url):
    expect(page).to_have_url(base_url + "/login", timeout=15_000)
    expect(page.get_by_test_id("email-login-btn")).to_be_visible()
    expect(page.get_by_test_id("next-layout")).to_have_count(0)
    expect(page.get_by_test_id("next-home")).to_have_count(0)


@pytest.mark.parametrize("path,heading,api_path", CURRENT_PAGES)
def test_legacy_routes_unchanged(authed_page, base_url, path, heading, api_path, record_property):
    """Exact legacy page and a successful real page-initiated data response."""
    with authed_page.expect_response(
        lambda response: urlsplit(response.url).path == api_path and response.request.method == "GET",
        timeout=20_000,
    ) as observed:
        _goto(authed_page, base_url, path)
    response = observed.value
    assert response.status == 200, f"{api_path} returned HTTP {response.status}"
    structured = isinstance(response.json(), (dict, list))
    assert structured, "Page API did not return structured data"
    record_property("real_api_path", api_path)
    record_property("real_api_status", response.status)
    expect(authed_page).to_have_url(base_url + path)
    expect(authed_page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
    _current(authed_page)


def test_entry_link_matches_verified_flag(authed_page, base_url, deployed_features):
    with authed_page.expect_response(lambda response: urlsplit(response.url).path == "/api/features") as observed:
        _goto(authed_page, base_url, "/activity")
    assert observed.value.status == 200, "Page features read failed"
    assert observed.value.json().get("features", {}).get("new_ui") is deployed_features["new_ui"]
    _current(authed_page)
    _identity(authed_page)
    entry = authed_page.get_by_test_id("try-new-ui")
    if deployed_features["new_ui"]:
        expect(entry).to_be_visible()
        expect(entry).to_have_attribute("href", "/next")
    else:
        expect(entry).to_have_count(0)


@pytest.mark.preview_on
def test_entry_return_and_shared_identity(authed_page, base_url):
    before = _identity(authed_page)
    authed_page.get_by_test_id("try-new-ui").click()
    _preview(authed_page, base_url)
    assert _identity(authed_page) == before, "Entering preview changed actor or workspace"
    assert not re.search(r"(?:[?#&])(id_token|access_token|code)=", authed_page.url), "Auth parameter leaked into URL"
    authed_page.get_by_test_id("back-to-current-ui").click()
    _current(authed_page)
    assert _identity(authed_page) == before, "Returning changed actor or workspace"


@pytest.mark.preview_on
def test_preview_links_match_current_navigation(authed_page, base_url, record_property):
    """Presence parity per journey, not count parity.

    The preview deliberately offers one destination twice: in Administration both
    `budgets` ("Budgets") and `model-access-admin` ("Model access") point at
    /budgets, because Bedrock account routing lives inside the Budgets page today
    and is surfaced under its own name. The current sidebar has one /budgets link,
    so asserting equal counts would fail on correct behaviour. The honest comparison
    is direction: a route the visible current nav OFFERS must be reachable in the
    preview, and a route it WITHHOLDS must not appear anywhere in it.
    """
    _current(authed_page)
    _identity(authed_page)
    expect(authed_page.get_by_test_id("try-new-ui")).to_be_visible()
    routes = tuple(ROUTE_JOURNEY)
    # Count inside the single VISIBLE nav. Counting both copies would compare two
    # current-UI links against the preview's one and never match, and the intent of
    # this test is the permission/feature gating that decides whether a route is
    # offered at all — a per-nav count of 1 or 0, not a DOM-copy tally.
    nav = authed_page.locator(CURRENT_NAV)
    expect(nav).to_have_count(1, timeout=15_000)
    visible = {route: nav.locator(f'a[href="{route}"]').count() for route in routes}
    assert all(count <= 1 for count in visible.values()), f"Ambiguous current-nav link counts: {visible}"
    record_property("current_nav_counts", visible)

    authed_page.get_by_test_id("try-new-ui").click()
    _preview(authed_page, base_url)

    # Visit every journey once and record what it offers, so the per-route result is
    # auditable rather than a bare green.
    preview = {}
    for journey in PREVIEW_JOURNEYS:
        if not _select_journey(authed_page, journey):
            continue
        preview[journey] = _preview_counts(authed_page, routes)
    record_property("preview_nav_counts", preview)
    assert "use" in preview, "Preview offered no Use ADP navigation"

    for route, count in visible.items():
        owner = ROUTE_JOURNEY[route]
        if count:
            # Offered in the current nav: must be reachable in its owning journey.
            # At least one, because a destination may legitimately be offered twice.
            assert owner in preview, f"Current nav offers {route} but the preview did not offer the {owner} journey that owns it"
            assert preview[owner][route] >= 1, f"Current nav offers {route} but the preview's {owner} journey has no link to it (observed {preview})"
        else:
            # Withheld in the current nav: must be absent from EVERY journey, not
            # just the owning one. Surfacing a gated route in the wrong journey is
            # still a gating leak. This half stays exact — it is the assertion that
            # proves a route the actor may not see is not offered.
            for journey, counts in preview.items():
                assert counts[route] == 0, f"Current nav withholds {route} but the preview's {journey} journey links to it (observed {preview})"


@pytest.mark.preview_off
@pytest.mark.parametrize("path", ["/next", "/next/not-built-yet"])
def test_live_flag_off_returns_current_ui(authed_page, base_url, path):
    _goto(authed_page, base_url, path)
    _current(authed_page)
    expect(authed_page.get_by_test_id("try-new-ui")).to_have_count(0)


@pytest.mark.preview_on
def test_direct_next_url(authed_page, base_url):
    _goto(authed_page, base_url, "/next")
    _preview(authed_page, base_url)


@pytest.mark.preview_on
def test_reload_and_browser_back(authed_page, base_url):
    _goto(authed_page, base_url, "/activity")
    expect(authed_page.get_by_role("heading", name="Agent Activity", exact=True)).to_be_visible()
    authed_page.get_by_test_id("try-new-ui").click()
    _preview(authed_page, base_url)
    authed_page.reload(wait_until="domcontentloaded")
    _preview(authed_page, base_url)
    authed_page.go_back(wait_until="domcontentloaded")
    expect(authed_page).to_have_url(base_url + "/activity")
    expect(authed_page.get_by_role("heading", name="Agent Activity", exact=True)).to_be_visible()
    _current(authed_page)


@pytest.mark.preview_on
def test_unknown_next_path_has_return(authed_page, base_url):
    _goto(authed_page, base_url, "/next/not-built-yet")
    _preview(authed_page, base_url, "/next/not-built-yet", home=False)
    authed_page.get_by_test_id("back-to-current-ui").click()
    _current(authed_page)


@pytest.mark.parametrize("path", ["/next", "/runs"])
def test_signed_out_redirects_to_actual_login(page, base_url, path):
    _goto(page, base_url, path)
    _login(page, base_url)


@pytest.mark.parametrize("path", ["/next", "/runs"])
def test_simulated_expired_stored_session(authed_page, base_url, path):
    """Browser-only expired storage with no refresh credential; not Cognito expiry."""
    authed_page.evaluate("""() => {
        sessionStorage.setItem('cognito_token_expiry', '1');
        sessionStorage.removeItem('cognito_refresh_token');
    }""")
    assert authed_page.evaluate("sessionStorage.getItem('cognito_token_expiry') === '1'")
    _goto(authed_page, base_url, path)
    _login(authed_page, base_url)
    assert authed_page.evaluate("sessionStorage.getItem('cognito_access_token') === null"), "Expired storage was not cleared"


@pytest.mark.preview_on
def test_simulated_failed_new_bundle(authed_page, base_url, record_property):
    """Fresh per-test context: a real abort and eager fallback are both mandatory."""
    _current(authed_page)
    blocked = []

    def abort_chunk(route):
        blocked.append(urlsplit(route.request.url).path)
        route.abort("failed")

    authed_page.route(re.compile(r"/assets/Next[A-Za-z]*-[^/?]*\.js(?:\?.*)?$"), abort_chunk)
    authed_page.get_by_test_id("try-new-ui").click()
    expect(authed_page.get_by_test_id("next-unavailable")).to_be_visible(timeout=15_000)
    assert blocked, "No preview chunk was blocked; simulation did not exercise recovery"
    record_property("blocked_preview_chunk_count", len(blocked))
    expect(authed_page.get_by_test_id("next-layout")).to_have_count(0)
    fallback = authed_page.get_by_test_id("next-unavailable-current-ui")
    expect(fallback).to_have_attribute("href", "/")
    with authed_page.expect_request(lambda req: req.is_navigation_request() and req.frame == authed_page.main_frame):
        fallback.click()
    _current(authed_page)


@pytest.mark.preview_on
def test_simulated_open_tab_flag_rollback(authed_page, base_url, deployed_features, record_property):
    """After real flag-on loading, only /features is controlled in this browser."""
    _goto(authed_page, base_url, "/next")
    _preview(authed_page, base_url)
    before = _identity(authed_page)
    documents, observations = [], []
    authed_page.on(
        "request",
        lambda req: documents.append(1) if req.is_navigation_request() else None,
    )

    def flag_off(route):
        observations.append(1)
        route.fulfill(json={"features": {**deployed_features, "new_ui": False}})

    authed_page.route(base_url + "/api/features", flag_off)
    # No reload, navigation, focus event or extra fetch manufactures observation.
    expect(authed_page.get_by_test_id("next-layout")).to_have_count(0, timeout=35_000)
    assert observations, "Open tab never re-read /features"
    _current(authed_page)
    expect(authed_page.get_by_test_id("try-new-ui")).to_have_count(0)
    assert not documents, "Rollback required a document reload"
    assert _identity(authed_page) == before, "Rollback changed actor or workspace"
    record_property("controlled_flag_off_responses", len(observations))
