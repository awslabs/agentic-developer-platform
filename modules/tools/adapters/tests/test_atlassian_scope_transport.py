"""The scope readback stays on the fixed provider, with bounded unambiguous JSON."""

import json
import time
from types import SimpleNamespace

import pytest

from tool_adapters import atlassian_observe as observer


@pytest.fixture
def provider(monkeypatch):
    state = SimpleNamespace(status=200, content_type="application/json", body=b"[]", closed=False, requests=[])
    sock = SimpleNamespace(settimeout=lambda _: None)

    class Connection:
        def __init__(self, host, timeout):
            assert host == "api.atlassian.com" and 0 < timeout <= 2

        def request(self, method, path, headers):
            assert method == "GET" and path == "/oauth/token/accessible-resources"
            assert headers == {"Authorization": "Bearer transient-fixture", "Accept": "application/json"}
            state.requests.append(path)

        def getresponse(self):
            return SimpleNamespace(
                status=state.status,
                getheader=lambda name, default=None: state.content_type if name == "Content-Type" else default,
                read=lambda size: state.body[:size],
            )

        def close(self):
            state.closed = True

    monkeypatch.setattr(observer.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(observer.socket, "getaddrinfo", lambda *a, **kw: [(None, None, None, None, ("8.8.8.8", 443))])
    monkeypatch.setattr(observer.socket, "create_connection", lambda *a, **kw: sock)
    monkeypatch.setattr(
        observer.ssl, "create_default_context", lambda: SimpleNamespace(wrap_socket=lambda *a, **kw: sock)
    )
    return state


def test_only_fixed_provider_scope_endpoint_is_read(provider):
    resources = [{"id": "site-fixture", "scopes": ["read:jira:agent-interface"]}]
    provider.body = json.dumps(resources).encode()
    assert observer.fetch_site_scopes("transient-fixture", deadline=time.monotonic() + 5) == resources
    assert len(provider.requests) == 1 and provider.closed


@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 500])
def test_scope_failures_and_redirects_are_never_followed(provider, status):
    provider.status = status
    with pytest.raises(ValueError, match="scope evidence unavailable"):
        observer.fetch_site_scopes("transient-fixture", deadline=time.monotonic() + 5)
    assert len(provider.requests) == 1 and provider.closed


@pytest.mark.parametrize("body", [b" " * 65537, b'[{"id":"one","id":"two"}]', b"[NaN]", b"not JSON"])
def test_oversized_or_ambiguous_scope_json_is_refused(provider, body):
    provider.body = body
    with pytest.raises(ValueError):
        observer.fetch_site_scopes("transient-fixture", deadline=time.monotonic() + 5)
    assert provider.closed


def test_private_resolution_is_refused_before_sending_token(provider, monkeypatch):
    monkeypatch.setattr(observer.socket, "getaddrinfo", lambda *a, **kw: [(None, None, None, None, ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="resolution refused"):
        observer.fetch_site_scopes("transient-fixture", deadline=time.monotonic() + 5)
    assert provider.requests == []


def test_expired_observation_cannot_start_scope_readback(provider):
    with pytest.raises(ValueError, match="deadline exceeded"):
        observer.fetch_site_scopes("transient-fixture", deadline=time.monotonic() - 1)
    assert provider.requests == []


def test_observer_catalogue_limit_accommodates_public_tools_without_expanding_results():
    transport = observer.ObservationMCPTransport(deadline=time.monotonic() + 8)
    assert transport.catalogue_bytes > 131063
    assert transport.result_bytes == 65536
