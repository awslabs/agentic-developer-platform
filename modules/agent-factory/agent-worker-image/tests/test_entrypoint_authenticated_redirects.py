"""Actual worker provider entrypoints must not export credentials on redirects."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import entrypoint


@pytest.fixture
def providers():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.received.append((self.path, dict(self.headers)))
            if self.path == self.server.redirect_path:
                self.send_response(self.server.redirect_code)
                self.send_header("Location", self.server.redirect_target)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps({"login": "fixture-user", "default_branch": "main"}).encode()
            )

        do_POST = do_GET

        def log_message(self, *args):
            pass

    servers = []
    threads = []
    for _ in range(2):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.received = []
        server.redirect_path = "/unused"
        server.redirect_target = "/ok"
        server.redirect_code = 302
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


def invoke_pat(origin, monkeypatch):
    request = entrypoint.urllib.request.Request

    def fixture_request(url, *args, **kwargs):
        # Substitute only the fixed initial provider URL; redirects still pass
        # through native urllib, real sockets and the production redirect guard.
        if url == "https://api.github.com/user":
            url = origin.url + "/user"
        return request(url, *args, **kwargs)

    monkeypatch.setattr(entrypoint.urllib.request, "Request", fixture_request)
    credentials = SimpleNamespace(raw_read=lambda **kwargs: {"value": "synthetic-pat"})
    return entrypoint._resolve_execution_token(
        envelope={"token_source": "pat", "user_id": "fixture-user"},
        environ={"ADP_PAT_EXECUTION_ENABLED": "true"},
        cred_client=credentials,
    )


def invoke_gitlab(origin, monkeypatch):
    monkeypatch.setenv("GITLAB_URL", origin.url)
    secrets = SimpleNamespace(
        get_secret_value=lambda **kwargs: {"SecretString": "synthetic-gitlab"}
    )
    monkeypatch.setattr(entrypoint.boto3, "client", lambda *args, **kwargs: secrets)
    deleted = []
    monkeypatch.setattr(entrypoint, "_delete_message", lambda *args: deleted.append(args))
    result = entrypoint._handle_gitlab_mention(
        {"payload": {"source": {"project_id": 11, "issue_iid": 22}}},
        "fixture-queue",
        "us-east-1",
        "fixture-receipt",
    )
    assert len(deleted) == 1
    return result


GITLAB_PATHS = [
    "/api/v4/projects/11/issues/22/notes",
    "/api/v4/projects/11",
    "/api/v4/projects/11/repository/branches",
]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("path", ["/user", *GITLAB_PATHS])
def test_foreign_redirect_never_receives_provider_credentials(providers, monkeypatch, status, path):
    origin, foreign = providers
    origin.redirect_path = path
    origin.redirect_code = status
    origin.redirect_target = foreign.url + "/stolen"
    if path == "/user":
        try:
            invoke_pat(origin, monkeypatch)
        except RuntimeError as exc:
            assert "synthetic-pat" not in str(exc)
    else:
        invoke_gitlab(origin, monkeypatch)
    assert any(p == path for p, _ in origin.received)
    assert foreign.received == [], "provider credential escaped to the redirected origin"
    original = next(headers for p, headers in origin.received if p == path)
    assert original["Authorization" if path == "/user" else "Private-Token"] == (
        "Bearer synthetic-pat" if path == "/user" else "synthetic-gitlab"
    )


@pytest.mark.parametrize("path", ["/user", *GITLAB_PATHS])
def test_same_origin_302_preserves_supported_provider_flow(providers, monkeypatch, path):
    origin, foreign = providers
    origin.redirect_path = path
    if path == "/user":
        result = invoke_pat(origin, monkeypatch)
        assert result.token_mode == "pat"
    else:
        assert invoke_gitlab(origin, monkeypatch) == 0
    assert any(p == "/ok" for p, _ in origin.received)
    assert foreign.received == []
