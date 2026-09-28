"""Native networking, state across CLI processes and cleanup regression coverage."""

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from native_browser import open_native_browser


@pytest.fixture
def site():
    posts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            posts.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"native fetch complete")

        def do_GET(self):
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/javascript" if self.path == "/sw.js" else "text/html",
            )
            self.end_headers()
            pages = {
                "/": """<title>Native fixture</title><body><h1 id="result">loading</h1>
                  <a href="/next">Next</a><a href="/popup" target="_blank">Popup</a>
                  <iframe src="/frame"></iframe><script>
                  document.cookie='continuity=kept; path=/';
                  fetch('/post', {method:'POST'}).then(r=>r.text()).then(t=>result.textContent=t);
                  navigator.serviceWorker.register('/sw.js');
                  </script>""",
                "/next": "<title>Next</title><body><script>document.write(document.cookie)</script>",
                "/frame": "<body>Native iframe loaded",
                "/popup": "<title>Popup</title><body>Native popup loaded",
                "/sw.js": "self.addEventListener('install',()=>self.skipWaiting());",
            }
            self.wfile.write(pages.get(self.path, "<body>Fixture").encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", posts
    finally:
        server.shutdown()
        thread.join()


def test_native_requests_scripts_frames_workers_popups_and_cleanup(site):
    from playwright.sync_api import sync_playwright

    url, posts = site
    client = Mock()
    client.start.return_value = "fixture-session"
    client.generate_ws_headers.return_value = ("fixture", {})
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        runtime = SimpleNamespace(
            chromium=SimpleNamespace(connect_over_cdp=lambda *a, **k: browser)
        )
        session = open_native_browser(url, runtime, client_factory=lambda **_: client)
        try:
            session.goto(url, wait_until="domcontentloaded")
            session.wait_for_timeout(600)
            assert "native fetch complete" in session.inner_text("body")
            assert posts == ["/post"]
            assert any(
                "Native iframe loaded" in f.inner_text("body") for f in session.frames
            )
            assert (
                session.evaluate(
                    "async () => (await navigator.serviceWorker.getRegistrations()).length"
                )
                == 1
            )
            before = session.evaluate("() => document.querySelectorAll('a')[1].target")
            session.click_observed(
                "a[href]",
                1,
                {
                    "href": url + "/popup",
                    "text": "Popup",
                    "download": False,
                    "target": "_blank",
                },
            )
            assert session.url == url + "/popup"
            assert before == "_blank"
            assert session.screenshot().startswith(b"\x89PNG")
        finally:
            session.close()
        client.stop.assert_called_once()


def test_cdp_failure_stops_managed_session_and_retains_id():
    client = Mock()
    client.start.return_value = "created-before-cdp-failure"
    client.generate_ws_headers.return_value = ("fixture", {})
    runtime = SimpleNamespace(
        chromium=SimpleNamespace(
            connect_over_cdp=Mock(side_effect=RuntimeError("CDP failure"))
        )
    )
    with pytest.raises(RuntimeError) as error:
        open_native_browser(
            "https://example.com", runtime, client_factory=lambda **_: client
        )
    assert error.value.cleanup == {
        "session_id": "created-before-cdp-failure",
        "cleanup_status": "stopped",
    }
    client.stop.assert_called_once()


def test_continuity_across_cli_processes_and_idempotent_close(site, monkeypatch):
    import local_browser

    url, _ = site
    fixture = Path(__file__).with_name("fixtures") / "native_process.py"
    root = Path(tempfile.mkdtemp(prefix="adp-native-test-"))
    monkeypatch.setattr(local_browser, "_root", lambda: root)
    monkeypatch.setenv("NODE_OPTIONS", "--require /synthetic-missing-instrumentation.js")
    original_popen = subprocess.Popen

    def spawn(command, **kwargs):
        return original_popen([command[0], str(fixture), *command[3:]], **kwargs)

    monkeypatch.setattr(local_browser.subprocess, "Popen", spawn)
    packet = local_browser.investigation_request("start", {"url": url})
    token = packet["session_token"]
    # Subsequent invocations are fresh Python processes, as with the analyst CLI.
    script = """import json,sys
from pathlib import Path
import local_browser
local_browser._root=lambda:Path(sys.argv[1])
print(json.dumps(local_browser.investigation_request(sys.argv[2],json.loads(sys.argv[3]))))"""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(__file__).resolve().parents[1]), str(Path(local_browser.__file__).resolve().parents[2])])}

    def request(operation, payload):
        command = [
            sys.executable,
            "-c",
            script,
            str(root),
            operation,
            json.dumps({"session_token": token, **payload}),
        ]
        result = original_popen(
            command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        stdout, stderr = result.communicate(timeout=20)
        assert result.returncode == 0, stderr
        return json.loads(stdout)

    try:
        assert packet["manifest"]["browser_transport"] == "agentcore_native"
        choice = next(c for c in packet["choices"] if c["text"] == "Next")
        step = request(
            "step",
            {
                "view_id": packet["view_id"],
                "action": "follow",
                "candidate_id": choice["id"],
            },
        )
        assert "continuity=kept" in step["observations"][0]["visible_text"]
        assert step["manifest"]["session_id"] == packet["manifest"]["session_id"]
        shot = request("step", {"view_id": step["view_id"], "action": "screenshot"})
        assert shot["observations"][0]["screenshot_base64"]
    finally:
        closed = request("close", {})
    assert closed["cleanup_status"] == "stopped"
    assert request("close", {}) == closed


def test_native_is_default_even_with_stale_broker_url(monkeypatch):
    import browser_client
    import local_browser

    monkeypatch.delenv("URL_ANALYSIS_BROWSER_MODE", raising=False)
    monkeypatch.setenv("URL_ANALYSIS_BROWSER_BROKER", "http://obsolete.invalid:8765")
    direct = Mock(return_value={"native": True})
    monkeypatch.setattr(local_browser, "investigation_request", direct)
    assert browser_client.investigation_request(
        "start", {"url": "https://example.com"}
    ) == {"native": True}


def test_one_shot_capture_keeps_provenance_and_stops_session(site, monkeypatch):
    import direct_capture
    from evidence_items import validate_inventory
    from case_contract import content_digest

    url, _ = site
    original = direct_capture.ProcessActor
    fixture = Path(__file__).with_name("fixtures") / "native_process.py"

    def actor(*args, **kwargs):
        kwargs["command"] = [sys.executable, str(fixture), "--worker", "--native"]
        return original(*args, **kwargs)

    monkeypatch.setattr(direct_capture, "ProcessActor", actor)
    bundle = direct_capture.capture("capture", {"url": url, "wait_seconds": 1})
    assert bundle["cleanup_status"] == "stopped"
    assert bundle["browser_transport"] == "agentcore_native"
    assert len(bundle["observations"]) == 2
    for observation in bundle["observations"]:
        validate_inventory(observation)
        assert observation["content_sha256"] == content_digest(observation)
    assert "native fetch complete" in bundle["observations"][-1]["visible_text"]
