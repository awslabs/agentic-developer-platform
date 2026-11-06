"""Readiness refreshes metadata without accepting client or facade schemas."""

from unittest.mock import Mock

import httpx
import pytest
from fastapi import Request

from src.internal import tools_discovery

STALE = {
    "tools": [{"name": "websearch.search"}],
    "connectors": [{"name": "knowledge", "dynamic": True, "discovery_status": "unavailable"}],
}
FRESH = {
    "tools": [{"name": "websearch.search"}, {"name": "knowledge.search"}],
    "connectors": [{"name": "knowledge", "dynamic": True, "discovery_status": "ready"}],
}


def request(headers=None):
    return Request({"type": "http", "headers": headers if headers is not None else [(b"authorization", b"Bearer synthetic-user-token")]})


def transport(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs == {"trust_env": False, "follow_redirects": False, "timeout": 15}
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(tools_discovery.httpx, "AsyncClient", client)


async def test_expired_metadata_refreshes_once_then_rereads_protected_provenance(monkeypatch):
    loader = Mock(side_effect=[STALE, FRESH])
    requests = []

    def handler(req):
        requests.append(req)
        assert str(req.url) == tools_discovery.FACADE_DISCOVERY_URL
        assert req.method == "GET" and not req.content and not req.url.query
        assert req.headers["authorization"] == "Bearer synthetic-user-token"
        assert "x-adp-user-authorization" not in req.headers and "x-owner-sub" not in req.headers
        return httpx.Response(200, json={"tools": [{"name": "knowledge.remember"}]})

    transport(monkeypatch, handler)
    incoming = request(
        [(b"authorization", b"Bearer synthetic-user-token"), (b"x-adp-user-authorization", b"Bearer forged"), (b"x-owner-sub", b"foreign")]
    )
    assert await tools_discovery.refreshed_tool_catalogue(incoming, loader) == FRESH
    assert len(requests) == 1 and loader.call_count == 2


async def test_success_response_cannot_replace_missing_protected_provenance(monkeypatch):
    transport(monkeypatch, lambda req: httpx.Response(200, json=FRESH))
    loader = Mock(return_value=STALE)
    assert await tools_discovery.refreshed_tool_catalogue(request(), loader) == STALE
    assert loader.call_count == 2


@pytest.mark.parametrize("failure", [401, 403, 503, 302, "timeout"])
async def test_failed_refresh_preserves_static_tools_without_redirect_or_retry(monkeypatch, failure):
    calls = []

    def handler(req):
        calls.append(req)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=req)
        return httpx.Response(failure, headers={"location": "https://foreign.example.test"})

    transport(monkeypatch, handler)
    loader = Mock(return_value=STALE)
    assert await tools_discovery.refreshed_tool_catalogue(request(), loader) == STALE
    assert len(calls) == loader.call_count == 1


@pytest.mark.parametrize(
    "catalogue,headers",
    [
        (FRESH, [(b"authorization", b"Bearer valid")]),
        ({"tools": []}, [(b"authorization", b"Bearer valid")]),
        (STALE, []),
        (STALE, [(b"authorization", b"Basic invalid")]),
        (STALE, [(b"authorization", b"Bearer first"), (b"authorization", b"Bearer second")]),
    ],
)
async def test_fresh_static_or_missing_bearer_never_contacts_facade(monkeypatch, catalogue, headers):
    def fail(**kwargs):
        pytest.fail("No discovery refresh should be sent")

    monkeypatch.setattr(tools_discovery.httpx, "AsyncClient", fail)
    loader = Mock(return_value=catalogue)
    assert await tools_discovery.refreshed_tool_catalogue(request(headers), loader) == catalogue
    loader.assert_called_once_with()
