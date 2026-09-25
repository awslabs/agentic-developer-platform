"""Exercise resolver redirects with local HTTP servers and synthetic keys only."""

import importlib.util
import json
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "gateway/lambdas/ingest/user_resolver.py"


@pytest.fixture
def resolver(monkeypatch):
    spec = importlib.util.spec_from_file_location("isolated_user_resolver_transport", SOURCE)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ENABLE_USER_IDENTITIES", True)
    monkeypatch.setattr(module, "RESOLVER_API_KEY", "synthetic-internal-key")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    return module


@contextmanager
def endpoint(status, *, location=None, body=None):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.respond()

        def do_GET(self):
            self.respond()

        def respond(self):
            calls.append({"path": self.path, "key": self.headers.get("X-Internal-Api-Key")})
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body or {}).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_never_forwards_internal_key(resolver, status):
    with endpoint(200, body={"user_id": "attacker"}) as (destination, received):
        with endpoint(status, location=destination + "/capture") as (origin, sent):
            resolver.RESOLVER_BASE_URL = origin
            assert resolver.resolve_user("slack", "synthetic-user") is None
            assert len(sent) == 1
            assert sent[0]["key"] == "synthetic-internal-key"
        assert received == []


def test_even_same_origin_redirect_is_not_an_identity_response(resolver):
    with endpoint(302, location="/other") as (origin, calls):
        resolver.RESOLVER_BASE_URL = origin
        assert resolver.resolve_user("slack", "synthetic-user") is None
        assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/not-a-resolver",
        "ftp://example.test/",
        "http://user:secret@example.test/",
        "http://example.test/?secret=value",
        "http://example.test/#fragment",
        "http://example.test:bad/",
    ],
)
def test_invalid_endpoint_never_constructs_transport(resolver, monkeypatch, url):
    opener = Mock()
    monkeypatch.setattr(resolver.urllib.request, "build_opener", opener)
    resolver.RESOLVER_BASE_URL = url
    assert resolver.resolve_user("slack", "synthetic-user") is None
    opener.assert_not_called()


def test_success_and_cache_use_only_configured_endpoint(resolver):
    with endpoint(200, body={"user_id": "canonical-user", "org_id": "team"}) as (origin, calls):
        resolver.RESOLVER_BASE_URL = origin
        first = resolver.resolve_user("slack", "synthetic-user")
        assert first.user_id == "canonical-user"
        assert resolver.resolve_user("slack", "synthetic-user") == first
        assert len(calls) == 1


def test_404_retains_magic_link_behavior(resolver):
    with endpoint(
        404, body={"magic_link_url": "https://gateway.example.test/link?token=synthetic"}
    ) as (origin, _):
        resolver.RESOLVER_BASE_URL = origin
        result = resolver.resolve_user("slack", "synthetic-user")
        assert isinstance(result, resolver.UnresolvedUser)
        assert result.magic_link_url.endswith("token=synthetic")


def test_transport_timeout_and_error_log_redaction(resolver, monkeypatch, caplog):
    opener = Mock()
    opener.open.side_effect = RuntimeError("private-key-and-request-content")
    monkeypatch.setattr(resolver.urllib.request, "build_opener", Mock(return_value=opener))
    resolver.RESOLVER_BASE_URL = "http://gateway.internal:8080"
    assert resolver.resolve_user("slack", "synthetic-user") is None
    assert opener.open.call_args.kwargs["timeout"] == 5
    assert "private-key-and-request-content" not in caplog.text
    assert "resolve-user call failed" in caplog.text
