"""Real HTTP boundaries: redirects must not export worker credentials."""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adp_cred import client as cred
from adp_review import client as review
from adp_trigger import client as trigger
from adp_trigger import transport_identity


@pytest.fixture
def endpoints():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.received.append(dict(self.headers))
            if self.path == "/ok":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(getattr(self.server, "ok_body", b'{"ok":true}'))
            else:
                self.send_response(self.server.redirect_code)
                self.send_header("Location", self.server.redirect_target)
                self.end_headers()

        do_POST = do_GET

        def log_message(self, *args):
            pass

    servers = []
    threads = []
    for _ in range(2):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.received = []
        server.redirect_code = 302
        server.redirect_target = "/ok"
        server.url = f"http://127.0.0.1:{server.server_port}"
        thread = threading.Thread(
            target=lambda s=server: s.serve_forever(poll_interval=0.01), daemon=True
        )
        thread.start()
        servers.append(server)
        threads.append(thread)
    try:
        yield servers
    finally:
        for server, thread in zip(servers, threads):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def call_client(kind, url, monkeypatch, method="GET"):
    from botocore.credentials import Credentials

    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "false")
    monkeypatch.setattr(
        transport_identity,
        "worker_credentials",
        lambda session: Credentials("synthetic-access-id", "synthetic-secret", "synthetic-session"),
    )
    headers = {
        "X-Adp-Run-Credential": "synthetic-run-credential",
        "X-Adp-Workload-Token": "synthetic-workload-token",
    }
    if kind == "cred-legacy":
        return cred._request(method, url, "synthetic-internal-key", extra_headers=headers)
    if kind == "cred-signed":
        return cred._sigv4_request(method, url, extra_headers=headers)
    if kind == "trigger":
        return trigger._send(method, url, body=None, extra_headers=headers)
    monkeypatch.setattr(review, "GITHUB_API", url.rsplit("/", 1)[0])
    return review._api(method, "/" + url.rsplit("/", 1)[1], None, "synthetic-review-token")


@pytest.mark.parametrize("kind", ["cred-legacy", "cred-signed", "trigger", "review"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_cross_origin_redirect_never_reaches_second_server(endpoints, monkeypatch, kind, status):
    origin, foreign = endpoints
    origin.redirect_code = status
    origin.redirect_target = foreign.url + "/ok"
    try:
        result = call_client(kind, origin.url + "/redirect", monkeypatch)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        assert foreign.received == [], (
            "redirect exported credential-bearing request to another origin"
        )
        assert kind == "review" and result[0] == status
    assert len(origin.received) == 1
    assert foreign.received == []


@pytest.mark.parametrize("kind", ["cred-legacy", "cred-signed", "trigger", "review"])
def test_same_origin_redirect_keeps_supported_response(endpoints, monkeypatch, kind):
    origin, foreign = endpoints
    result = call_client(kind, origin.url + "/redirect", monkeypatch)
    assert result[0] == 200 if kind == "review" else result == {"ok": True}
    assert len(origin.received) == 2
    assert foreign.received == []
    # The permitted same-origin hop still carries the selected auth identity.
    header = "X-Internal-Api-Key" if kind == "cred-legacy" else "Authorization"
    normalized = [{k.lower(): v for k, v in r.items()} for r in origin.received]
    assert normalized[0][header.lower()] == normalized[1][header.lower()]


@pytest.mark.parametrize("kind", ["cred-legacy", "cred-signed", "trigger", "review"])
def test_post_redirect_cannot_export_authentication(endpoints, monkeypatch, kind):
    origin, foreign = endpoints
    origin.redirect_target = foreign.url + "/ok"
    try:
        result = call_client(kind, origin.url + "/redirect", monkeypatch, method="POST")
    except SystemExit as exc:
        assert exc.code == 1
    else:
        assert kind == "review" and result[0] == 302
    assert foreign.received == []


@pytest.mark.parametrize(
    "destination",
    [
        "http://api.example.test/final",
        "https://other.example.test/final",
        "https://api.example.test:444/final",
        "https://userinfo@api.example.test/final",
        "https://api.example.test:bad/final",
        "file:///etc/passwd",
        "ftp://api.example.test/final",
    ],
)
def test_redirect_origin_comparison_refuses_scheme_host_port_and_userinfo(destination):
    from io import BytesIO
    from urllib.error import HTTPError
    from urllib.request import Request

    from lib.authenticated_http import SameOriginRedirectHandler

    response = BytesIO(b"untrusted redirect body")
    request = Request("https://api.example.test/start", headers={"Authorization": "synthetic"})
    with pytest.raises(HTTPError) as failure:
        SameOriginRedirectHandler().redirect_request(
            request, response, 302, "Found", {}, destination
        )
    assert response.closed
    assert destination not in failure.value.reason
    assert failure.value.read() == b""


def test_default_https_port_stays_same_origin():
    from urllib.request import Request

    from lib.authenticated_http import SameOriginRedirectHandler

    request = Request("https://API.example.test/start", headers={"Authorization": "synthetic"})
    redirected = SameOriginRedirectHandler().redirect_request(
        request, None, 302, "Found", {}, "https://api.example.test:443/final"
    )
    assert redirected.get_header("Authorization") == "synthetic"


@pytest.mark.parametrize(
    "url",
    ["file:///tmp/input", "ftp://host/input", "https://user@host/input", "https://host:bad/input"],
)
def test_invalid_initial_endpoint_never_opens_connection(monkeypatch, url):
    from urllib.error import URLError
    from urllib.request import Request

    from lib import authenticated_http

    monkeypatch.setattr(
        authenticated_http, "build_opener", lambda *args: pytest.fail("network opener reached")
    )
    with pytest.raises(URLError, match="invalid authenticated endpoint"):
        authenticated_http.open_authenticated(Request(url))


def call_registration_or_provenance(kind, origin_url, monkeypatch):
    from types import SimpleNamespace

    import botocore.session
    from botocore.credentials import Credentials
    from lib import engine_registration, provenance_client

    credentials = Credentials("synthetic-access", "synthetic-secret", "synthetic-session")
    monkeypatch.setattr(
        botocore.session,
        "get_session",
        lambda: SimpleNamespace(get_credentials=lambda: credentials),
    )
    monkeypatch.setenv("ADP_AGENT_AUTHORITY_ENABLED", "false")
    monkeypatch.setattr(provenance_client, "_emit_metric", lambda *args: None)
    if kind == "registration":
        return engine_registration._post_document(
            origin_url + "/redirect",
            {"fixture": True},
            run_id="synthetic-run",
            endpoint_base=origin_url,
            timeout=3,
        )
    if kind == "provenance-signed":
        monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", origin_url)
    else:
        monkeypatch.delenv("ADP_GATEWAY_ENDPOINT", raising=False)
        monkeypatch.setenv("VAULT_GATEWAY_URL", origin_url)
        monkeypatch.setenv("VAULT_INTERNAL_API_KEY", "synthetic-internal-key")
    return provenance_client.post_provenance(
        actor_user_id="fixture-actor",
        triggered_by=None,
        root_human_id="fixture-root",
        is_human_rooted=True,
        action_kind="fixture",
        source_event={},
        correlation_id="fixture-correlation",
        org_id="fixture-org",
    )


@pytest.mark.parametrize("kind", ["registration", "provenance-signed", "provenance-legacy"])
@pytest.mark.parametrize("status", [301, 302, 303])
def test_registration_provenance_redirect_cannot_export_identity(
    endpoints, monkeypatch, kind, status
):
    from lib.engine_registration import EngineRegistrationError

    origin, foreign = endpoints
    origin.redirect_code = status
    origin.redirect_target = foreign.url + "/ok"
    foreign.ok_body = b'{"id":"synthetic-provenance"}'
    try:
        result = call_registration_or_provenance(kind, origin.url, monkeypatch)
    except EngineRegistrationError:
        assert kind == "registration"
    else:
        assert foreign.received == [], (
            "redirect exported credential-bearing request to another origin"
        )
        assert result is None  # Provenance preserves its existing fail-soft contract.
    assert len(origin.received) == 1
    assert foreign.received == []


@pytest.mark.parametrize("kind", ["registration", "provenance-signed", "provenance-legacy"])
def test_registration_provenance_same_origin_still_succeeds(endpoints, monkeypatch, kind):
    origin, foreign = endpoints
    origin.ok_body = b'{"id":"synthetic-provenance"}'
    result = call_registration_or_provenance(kind, origin.url, monkeypatch)
    assert result == (
        {"id": "synthetic-provenance"} if kind == "registration" else "synthetic-provenance"
    )
    assert len(origin.received) == 2
    assert foreign.received == []
    normalized = [{k.lower(): v for k, v in r.items()} for r in origin.received]
    header = "x-internal-api-key" if kind == "provenance-legacy" else "authorization"
    assert normalized[0][header] == normalized[1][header]
