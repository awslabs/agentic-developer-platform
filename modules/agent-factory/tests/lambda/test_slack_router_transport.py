"""Slack transport tests use loopback HTTP servers and synthetic tokens only."""

import importlib.util
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "gateway/lambdas/response/routers/slack.py"


@pytest.fixture
def slack(monkeypatch):
    spec = importlib.util.spec_from_file_location("isolated_slack_router_transport", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    return module


@contextmanager
def server(status=200, *, location=None, body=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.respond()

        def do_GET(self):
            self.respond()

        def respond(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append(
                {"path": self.path, "auth": self.headers.get("Authorization"), "body": raw}
            )
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(json.dumps(body or {}).encode())

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", requests
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def router(slack):
    secrets = Mock()
    secrets.get_secret_value.return_value = {"SecretString": "synthetic-bot-token"}
    return slack.SlackRouter(secrets), secrets


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_does_not_forward_slack_token(slack, monkeypatch, code):
    sender, _ = router(slack)
    with server(body={"ok": True}) as (destination, received):
        with server(code, location=destination + "/capture") as (origin, sent):
            monkeypatch.setattr(slack, "_SLACK_POST_MESSAGE_URL", origin + "/api/chat.postMessage")
            result = sender.route("synthetic message", {"channel_id": "C-test"}, "task-test")
            assert result is False
            assert len(sent) == 1
            assert sent[0]["auth"] == "Bearer synthetic-bot-token"
        assert received == []


def test_success_keeps_channel_thread_payload_and_token_cache(slack, monkeypatch):
    sender, secrets = router(slack)
    with server(body={"ok": True}) as (origin, received):
        monkeypatch.setattr(slack, "_SLACK_POST_MESSAGE_URL", origin + "/api/chat.postMessage")
        assert sender.route(
            "synthetic message", {"channel_id": "C-test", "thread_id": "123"}, "task-test"
        )
        assert sender.route("second", {"channel_id": "C-test"}, "task-test")
        assert json.loads(received[0]["body"]) == {
            "channel": "C-test",
            "thread_ts": "123",
            "text": "synthetic message",
        }
        assert len(received) == 2
        secrets.get_secret_value.assert_called_once()


def test_missing_channel_does_not_fetch_token_or_make_request(slack, monkeypatch):
    sender, secrets = router(slack)
    opener = Mock()
    monkeypatch.setattr(slack.urllib.request, "build_opener", opener)
    assert not sender.route("synthetic message", {}, "task-test")
    secrets.get_secret_value.assert_not_called()
    opener.assert_not_called()


def test_timeout_and_transport_failure_logs_omit_private_details(slack, monkeypatch, caplog):
    sender, _ = router(slack)
    opener = Mock()
    opener.open.side_effect = RuntimeError("private-bot-token-and-message")
    monkeypatch.setattr(slack.urllib.request, "build_opener", Mock(return_value=opener))
    assert not sender.route("synthetic message", {"channel_id": "C-test"}, "task-test")
    assert opener.open.call_args.kwargs["timeout"] == 10
    assert "private-bot-token-and-message" not in caplog.text
    assert "Slack send failed" in caplog.text


def test_secret_failure_logs_omit_private_details(slack, caplog):
    sender, secrets = router(slack)
    secrets.get_secret_value.side_effect = RuntimeError("private-secret-detail")
    assert not sender.route("synthetic message", {"channel_id": "C-test"}, "task-test")
    assert "private-secret-detail" not in caplog.text
    assert "Failed to get Slack token" in caplog.text


def test_api_failure_response_is_not_echoed_into_logs(slack, monkeypatch, caplog):
    sender, _ = router(slack)
    with server(body={"ok": False, "error": "private-request-content"}) as (origin, _):
        monkeypatch.setattr(slack, "_SLACK_POST_MESSAGE_URL", origin + "/api/chat.postMessage")
        assert not sender.route("synthetic message", {"channel_id": "C-test"}, "task-test")
    assert "private-request-content" not in caplog.text
    assert "Slack API reported a failure" in caplog.text
