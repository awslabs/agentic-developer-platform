"""Keep crawl4ai's browser offline and fulfill each request through the URL guard.

This adapts the existing URL-analysis browser boundary to Playwright's async
hooks. The browser never continues a request on its own connection. Redirects
are returned one hop at a time, so navigation, scripts, frames and fetch/XHR all
pass the same destination and pinned-socket checks.
"""

from __future__ import annotations

import asyncio
import time

from url_denylist import DenylistConfig
from url_fetch import DestinationRefused, _single_fetch

_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
_MAX_PAGE_BYTES = 100 * 1024 * 1024


class BrowserFetchGuard:
    def __init__(self, *, timeout: float = 60):
        self.deadline = time.monotonic() + timeout
        self.bytes_read = 0
        self.slots = asyncio.Semaphore(4)
        self.installed = False

    async def install(self, page, context, **kwargs):
        # Offline remains the fallback if any browser fetch bypasses routing
        # (for example, service-worker networking). All allowed HTTP is fulfilled
        # explicitly below, independent of the browser's own network stack.
        await context.set_offline(True)
        await context.route("**/*", self.handle_route)
        await context.route_web_socket("**/*", self.handle_websocket)
        self.installed = True
        return page

    async def handle_websocket(self, route):
        await route.close(code=1008, reason="destination refused")

    async def handle_route(self, route):
        request = route.request
        async with self.slots:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or self.bytes_read >= _MAX_PAGE_BYTES:
                await route.abort("timedout")
                return
            try:
                request_headers = await request.all_headers()
                # Service workers can own future fetches outside page routing.
                # Chromium identifies their registration/update script request
                # with this browser-generated header. Refuse it before fetching.
                if any(key.lower() == "service-worker" for key in request_headers):
                    await route.abort("blockedbyclient")
                    return
                headers = {
                    key: value
                    for key, value in request_headers.items()
                    if key.lower() not in _HOP_HEADERS
                }
                response, _ = await asyncio.wait_for(
                    asyncio.to_thread(
                        _single_fetch,
                        request.method,
                        request.url,
                        headers,
                        min(30, remaining),
                        DenylistConfig(),
                        request.post_data_buffer,
                    ),
                    timeout=remaining,
                )
                self.bytes_read += len(response.content)
                if self.bytes_read > _MAX_PAGE_BYTES:
                    await route.abort("blockedbyclient")
                    return
                # urllib3 returns decoded content. Do not instruct Chromium to
                # decode it again or trust the compressed body's original size.
                response_headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() not in _HOP_HEADERS | {"content-encoding"}
                }
                await route.fulfill(
                    status=response.status_code, headers=response_headers, body=response.content
                )
            except DestinationRefused:
                await route.abort("blockedbyclient")
            except Exception:
                # Never fall back to route.continue_()/route.fetch(): either one
                # would give the browser a fresh, unchecked DNS/socket decision.
                await route.abort("connectionfailed")
