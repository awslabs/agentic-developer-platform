"""Browser rendering preserves permitted JS while every fetch uses admission."""

import ipaddress
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "images" / "ingestion"))

import browser_fetch
import url_denylist
import url_fetch


async def test_hook_installs_offline_routes_before_reporting_ready():
    context = Mock(set_offline=AsyncMock(), route=AsyncMock(), route_web_socket=AsyncMock())
    guard = browser_fetch.BrowserFetchGuard()
    page = object()
    assert await guard.install(page, context) is page
    context.set_offline.assert_awaited_once_with(True)
    context.route.assert_awaited_once_with("**/*", guard.handle_route)
    context.route_web_socket.assert_awaited_once_with("**/*", guard.handle_websocket)
    assert guard.installed


async def test_browser_route_refuses_internal_destination(monkeypatch):
    connector = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(url_fetch, "_transport_fetch", connector)
    request = Mock(
        url="http://169.254.169.254/latest/meta-data/",
        method="GET",
        all_headers=AsyncMock(return_value={}),
        post_data_buffer=None,
    )
    route = Mock(request=request, abort=AsyncMock(), fulfill=AsyncMock())
    await browser_fetch.BrowserFetchGuard().handle_route(route)
    route.abort.assert_awaited_once_with("blockedbyclient")
    route.fulfill.assert_not_awaited()
    connector.assert_not_called()


@pytest.mark.parametrize("body", [None, b'{"search":"public docs"}'])
async def test_browser_route_preserves_post_body_and_returns_one_redirect(monkeypatch, body):
    monkeypatch.setattr(
        url_denylist, "_resolve_hostname", lambda _: [ipaddress.ip_address("93.184.216.34")]
    )
    transport = Mock(
        return_value=url_fetch.FetchResponse(
            url="https://docs.example/start",
            status_code=302,
            headers={"Location": "https://docs.example/end"},
            content=b"",
        )
    )
    monkeypatch.setattr(url_fetch, "_transport_fetch", transport)
    request = Mock(
        url="https://docs.example/start",
        method="POST" if body else "GET",
        all_headers=AsyncMock(return_value={"Host": "forged.example"}),
        post_data_buffer=body,
    )
    route = Mock(request=request, abort=AsyncMock(), fulfill=AsyncMock())
    await browser_fetch.BrowserFetchGuard().handle_route(route)
    route.abort.assert_not_awaited()
    assert route.fulfill.await_args.kwargs["status"] == 302
    assert route.fulfill.await_args.kwargs["headers"]["Location"] == "https://docs.example/end"
    assert transport.call_count == 1
    assert "Host" not in transport.call_args.args[2]
    if body:
        assert transport.call_args.args[-1] == body


@pytest.mark.skipif(
    not __import__("importlib.util").util.find_spec("playwright"),
    reason="local browser regression needs Playwright",
)
async def test_real_browser_renders_allowed_js_and_denies_subresource_and_redirect(monkeypatch):
    from playwright.async_api import async_playwright, Error

    calls = []
    monkeypatch.setattr(
        url_denylist, "_resolve_hostname", lambda _: [ipaddress.ip_address("93.184.216.34")]
    )

    def transport(method, url, headers, timeout, config, verdict, body=None):
        calls.append(url)
        if url.endswith("/redirect"):
            return url_fetch.FetchResponse(
                url=url,
                status_code=302,
                headers={"location": "http://127.0.0.1:8123/private"},
                content=b"",
            )
        if url.endswith("/data"):
            content = b"Rendered public data"
        else:
            content = b"""<html><body><div id="output"></div><script>
                navigator.serviceWorker.register('/sw.js')
                  .then(()=>document.body.dataset.worker='registered')
                  .catch(()=>document.body.dataset.worker='denied');
                fetch('/data').then(r=>r.text()).then(t=>document.querySelector('#output').textContent=t);
                fetch('http://127.0.0.1:8123/private').then(r=>r.text()).then(t=>document.body.dataset.private=t)
                  .catch(()=>document.body.dataset.denied='yes');
                </script></body></html>"""
        return url_fetch.FetchResponse(
            url=url, status_code=200, headers={"content-type": "text/html"}, content=content
        )

    monkeypatch.setattr(url_fetch, "_transport_fetch", transport)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            # Match crawl4ai's fresh, nonpersistent context, including its default
            # service-worker setting. The production offline backstop must work
            # without a test-only service_workers="block" safeguard.
            context = await browser.new_context()
            page = await context.new_page()
            guard = browser_fetch.BrowserFetchGuard()
            await guard.install(page, context)
            await page.goto("https://docs.example/page")
            await page.wait_for_function(
                "document.querySelector('#output').textContent === 'Rendered public data' && document.body.dataset.denied === 'yes' && document.body.dataset.worker === 'denied'"
            )
            assert await page.get_attribute("body", "data-private") is None
            with pytest.raises(Error):
                await page.goto("https://docs.example/redirect")
            assert set(calls) == {
                "https://docs.example/page",
                "https://docs.example/data",
                "https://docs.example/redirect",
            }
        finally:
            await browser.close()


@pytest.mark.parametrize("invoke_hook", [True, False])
async def test_crawl_entry_point_installs_guard_before_navigation(monkeypatch, invoke_hook):
    script = Path(__file__).resolve().parents[2] / "images" / "ingestion" / "ingest-url.py"
    spec = importlib.util.spec_from_file_location("ingest_url_browser_review", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "CRAWL4AI_AVAILABLE", True)
    monkeypatch.setattr(
        url_denylist, "_resolve_hostname", lambda _: [ipaddress.ip_address("93.184.216.34")]
    )
    context = Mock(set_offline=AsyncMock(), route=AsyncMock(), route_web_socket=AsyncMock())
    hooks = {}

    class Crawler:
        crawler_strategy = SimpleNamespace(set_hook=lambda name, hook: hooks.update({name: hook}))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def arun(self, url, config):
            assert "on_page_context_created" in hooks
            if invoke_hook:
                await hooks["on_page_context_created"](object(), context=context, config=config)
                context.set_offline.assert_awaited_once_with(True)
                context.route.assert_awaited_once()
                context.route_web_socket.assert_awaited_once()
            return SimpleNamespace(markdown="# Rendered public documentation")

    monkeypatch.setattr(module, "AsyncWebCrawler", lambda **kwargs: Crawler(), raising=False)
    monkeypatch.setattr(
        module, "BrowserConfig", lambda **kwargs: SimpleNamespace(**kwargs), raising=False
    )
    monkeypatch.setattr(module, "CrawlerRunConfig", lambda: object(), raising=False)
    result = await module.crawl_url_crawl4ai("https://docs.example/page")
    assert result == ("# Rendered public documentation" if invoke_hook else None)


async def test_service_worker_registration_never_fetches_script(monkeypatch):
    transport = Mock(side_effect=AssertionError("must not fetch a service-worker script"))
    monkeypatch.setattr(browser_fetch, "_single_fetch", transport)
    request = Mock(
        url="https://docs.example/sw.js",
        method="GET",
        all_headers=AsyncMock(return_value={"service-worker": "script"}),
        post_data_buffer=None,
    )
    route = Mock(request=request, abort=AsyncMock(), fulfill=AsyncMock())
    await browser_fetch.BrowserFetchGuard().handle_route(route)
    route.abort.assert_awaited_once_with("blockedbyclient")
    route.fulfill.assert_not_awaited()
    transport.assert_not_called()
