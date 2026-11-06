"""Direct AgentCore CDP sessions. Page networking stays inside AgentCore."""

from __future__ import annotations

import time
from types import SimpleNamespace

from agentcore_tools.browser_runtime.browser_guard import DEFAULT_REGION, GuardedBrowserSession
from agentcore_tools.browser_runtime.denylist import check_url, scrub_url_credentials
from agentcore_tools.browser_runtime.runtime_limits import LEASE_SECONDS, STARTUP_SECONDS


class NativeBrowserSession(GuardedBrowserSession):
    """Recorder facade; native transport with pre-request policy."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._callbacks = []
        self._context.on("page", self._new_page)

    def _new_page(self, page):
        # Follow genuine popups without rewriting target attributes/click handlers.
        self._page = page
        page.set_default_timeout(10000)
        for event, callback in self._callbacks:
            page.on(event, callback)

    def on(self, event, callback):
        self._callbacks.append((event, callback))
        self._page.on(event, callback)

    def click_observed(self, selector, index, expected):
        element = self._page.locator(selector).nth(index)
        actual = element.evaluate(
            "e => ({href:e.href || '', text:(e.innerText || e.textContent || '').slice(0,150), download:e.hasAttribute('download'), target:e.getAttribute('target') || ''})"
        )
        if actual != expected:
            raise ValueError("Observed element changed; inspect a fresh view")
        if actual["download"]:
            raise ValueError("Download clicks are disabled")
        element.click(timeout=10000)
        self._page.wait_for_timeout(200)
        return {"target_rewritten_to_self": False}


def open_native_browser(
    url,
    playwright,
    *,
    region=DEFAULT_REGION,
    client_factory=None,
    context_options=None,
    read_only=False,
    navigation_check=None,
    deadline_at=None,
):
    """Start an ephemeral AWS-managed browser with native networking.

    Read-only methods and unapproved destinations are rejected before dispatch.
    Native transport still does not pin the destination after DNS validation.
    """
    from agentcore_tools.browser_runtime.input_validation import validate_input as _validate_input

    _validate_input(url)
    if client_factory is None:
        from bedrock_agentcore.tools.browser_client import BrowserClient

        client_factory = BrowserClient
    if deadline_at is not None and deadline_at <= int(time.time()):
        raise ValueError("Browser authority expired")
    client = client_factory(region=region)
    browser = context = page = None
    session_id = None
    try:
        remaining = LEASE_SECONDS if deadline_at is None else int(deadline_at - time.time())
        if remaining <= 0:
            raise ValueError("Browser authority expired")
        session_id = client.start(session_timeout_seconds=min(LEASE_SECONDS, remaining))
        ws_url, headers = client.generate_ws_headers()
        browser = playwright.chromium.connect_over_cdp(
            ws_url, headers=headers, timeout=STARTUP_SECONDS * 1000
        )
        options = dict(context_options or {})
        if read_only:
            options["service_workers"] = "block"
        context = browser.new_context(**options)
        guard = SimpleNamespace(refusals=[], connections=[], transport_errors=[], connections_dropped=0)

        def on_request(route):
            request = route.request
            navigation = request.is_navigation_request()
            if read_only and request.method not in {"GET", "HEAD", "OPTIONS"}:
                result = SimpleNamespace(allowed=False, reason="State-changing HTTP methods are disabled",
                                         reason_code="method_not_allowed")
            else:
                result = navigation_check(request.url) if navigation and navigation_check else check_url(request.url)
                if result.allowed and navigation and navigation_check:
                    result = check_url(request.url)
            if not result.allowed:
                if len(guard.refusals) < 200:
                    guard.refusals.append({"url": scrub_url_credentials(request.url), "reason": result.reason,
                                           "reason_code": result.reason_code, "navigation": navigation})
                route.abort("blockedbyclient")
                return
            route.continue_()

        context.route("**/*", on_request)
        route_web_socket = getattr(context, "route_web_socket", None)
        if read_only and route_web_socket is not None:
            route_web_socket("**/*", lambda route: route.close())
        page = context.new_page()
        page.set_default_timeout(10000)
        return NativeBrowserSession(
            client=client,
            session_id=session_id,
            browser=browser,
            context=context,
            page=page,
            guard=guard,
        )
    except BaseException as error:
        for resource in (page, context, browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        cleanup = "not_started" if session_id is None else "unknown"
        if session_id is not None:
            try:
                client.stop()
                cleanup = "stopped"
            except Exception:
                pass
        error.cleanup = {"session_id": session_id, "cleanup_status": cleanup}
        raise
