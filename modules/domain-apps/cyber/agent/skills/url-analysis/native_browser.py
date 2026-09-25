"""Direct AgentCore CDP sessions. Page networking stays inside AgentCore."""

from __future__ import annotations

from types import SimpleNamespace

from browser_guard import DEFAULT_REGION, GuardedBrowserSession
from runtime_limits import LEASE_SECONDS, STARTUP_SECONDS


class NativeBrowserSession(GuardedBrowserSession):
    """Recorder facade; no request routes, pinned transport or offline context."""

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
):
    """Start an ephemeral AWS-managed browser with native networking.

    read_only/navigation_check remain recorder API compatibility arguments. Scope
    is checked for analyst-selected actions, not imposed on page subrequests.
    AWS session isolation is not a claim of per-request destination filtering.
    """
    from research_case import _validate_input

    _validate_input(url)
    if client_factory is None:
        from bedrock_agentcore.tools.browser_client import BrowserClient

        client_factory = BrowserClient
    client = client_factory(region=region)
    browser = context = page = None
    session_id = None
    try:
        session_id = client.start(session_timeout_seconds=LEASE_SECONDS)
        ws_url, headers = client.generate_ws_headers()
        browser = playwright.chromium.connect_over_cdp(
            ws_url, headers=headers, timeout=STARTUP_SECONDS * 1000
        )
        context = browser.new_context(**dict(context_options or {}))
        page = context.new_page()
        page.set_default_timeout(10000)
        return NativeBrowserSession(
            client=client,
            session_id=session_id,
            browser=browser,
            context=context,
            page=page,
            # Compatibility telemetry: native sockets have no broker peer log.
            guard=SimpleNamespace(
                refusals=[], connections=[], transport_errors=[], connections_dropped=0
            ),
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
