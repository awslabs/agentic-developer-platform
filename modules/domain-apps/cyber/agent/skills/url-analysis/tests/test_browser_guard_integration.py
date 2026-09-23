"""Real-Chromium integration coverage for browser-only network paths."""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from browser_guard import NavigationGuard, PinnedHTTPTransport, PinnedResponse
from denylist import DenylistResult

playwright = pytest.importorskip("playwright.sync_api")


def _dns(*ips: str):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 443)) for ip in ips]


class StaticTransport:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def fetch(self, request, decision, config=None) -> PinnedResponse:
        self.urls.append(request.url)
        if request.url.endswith("/sw.js"):
            body = b"self.addEventListener('fetch', () => fetch('http://169.254.169.254/'))"
            return PinnedResponse(200, {"Content-Type": "text/javascript"}, body)
        body = b"""
            <script>
            window.swResult = 'pending';
            (async () => {
              try {
                await navigator.serviceWorker.register('/sw.js');
                window.swResult = 'registered';
              } catch (error) {
                window.swResult = 'blocked';
              }
            })();
            </script>
        """
        return PinnedResponse(200, {"Content-Type": "text/html"}, body)


class FakePinnedResponse:
    status = 200

    def __init__(self, path: str) -> None:
        if path == "/set-cookie":
            self._headers = [
                ("Content-Type", "text/html"),
                ("Set-Cookie", "session=guarded; Path=/; SameSite=Lax"),
            ]
            self._chunks = [b"<script>fetch('/cookie-check')</script>", b""]
        else:
            self._headers = [("Content-Type", "text/plain")]
            self._chunks = [b"ok", b""]

    def getheaders(self) -> list[tuple[str, str]]:
        return self._headers

    def read1(self, size: int) -> bytes:
        return self._chunks.pop(0)


class FakePinnedConnection:
    sock = None

    def __init__(self, requests: list[tuple[str, dict[str, str]]]) -> None:
        self.requests = requests
        self.path = ""

    def request(self, method: str, path: str, body=None, headers=None) -> None:
        self.path = path
        self.requests.append((path, dict(headers or {})))

    def getresponse(self) -> FakePinnedResponse:
        return FakePinnedResponse(self.path)

    def close(self) -> None:
        pass


class CookiePinnedTransport(PinnedHTTPTransport):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, dict[str, str]]] = []

    def _open_connection(self, scheme, host, port, connect_ip, deadline):
        return FakePinnedConnection(self.requests)


@patch("socket.getaddrinfo", return_value=_dns("93.184.216.34"))
def test_service_worker_cannot_create_an_unrouted_internal_fetch(mock_dns) -> None:
    transport = StaticTransport()
    guard = NavigationGuard(
        vetted=DenylistResult(allowed=True, resolved_ips=["93.184.216.34"]),
        target_host="public.test",
        transport=transport,
    )

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(
                headless=True, executable_path=runtime.chromium.executable_path
            )
        except playwright.Error as error:
            pytest.skip(f"Chromium runtime dependencies are unavailable: {error}")
        try:
            context = browser.new_context(offline=True, service_workers="block")
            guard.install(context)
            page = context.new_page()
            page.goto("https://public.test/", wait_until="domcontentloaded")
            page.wait_for_function("window.swResult !== 'pending'")
            page.wait_for_timeout(250)

            assert transport.urls == ["https://public.test/"]
            assert (
                page.evaluate(
                    "navigator.serviceWorker.getRegistrations().then(items => items.length)"
                )
                == 0
            )
            assert guard.refusals == []
        finally:
            browser.close()


@patch("socket.getaddrinfo", return_value=_dns("93.184.216.34"))
def test_fulfilled_response_cookie_reaches_next_pinned_request(mock_dns) -> None:
    transport = CookiePinnedTransport()
    guard = NavigationGuard(
        vetted=DenylistResult(allowed=True, resolved_ips=["93.184.216.34"]),
        target_host="public.test",
        transport=transport,
    )

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(
                headless=True, executable_path=runtime.chromium.executable_path
            )
        except playwright.Error as error:
            pytest.skip(f"Chromium runtime dependencies are unavailable: {error}")
        try:
            context = browser.new_context(offline=True, service_workers="block")
            guard.install(context)
            page = context.new_page()
            page.goto("https://public.test/set-cookie", wait_until="networkidle")

            _, complete_headers = next(
                request
                for request in transport.requests
                if request[0] == "/cookie-check"
            )
            assert complete_headers["cookie"] == "session=guarded"
        finally:
            browser.close()
