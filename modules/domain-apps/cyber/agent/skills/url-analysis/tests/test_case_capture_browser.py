"""Real Chromium fixtures through the guarded collector; no live threat traffic."""

import json
import socket
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest

from browser_guard import PinnedResponse, open_guarded_browser
from case_capture import collect_case
from research_case import add_probe, assess_case, new_case

playwright = pytest.importorskip("playwright.sync_api")


class ManagedClient:
    def __init__(self):
        self.stopped = False

    def start(self, **kwargs):
        assert kwargs["session_timeout_seconds"] == 300
        return "fixture-session"

    def generate_ws_headers(self):
        return "fixture-only", {}

    def stop(self):
        self.stopped = True


class FixtureTransport:
    def __init__(self):
        self.requests = []

    def fetch(self, request, decision, config=None):
        self.requests.append((request.method, request.url))
        path = urlsplit(request.url).path
        if path == "/redirect":
            return PinnedResponse(
                302, {"location": "/delayed?code=secret"}, b"", "93.184.216.34"
            )
        if path == "/to-private":
            return PinnedResponse(
                302, {"location": "http://169.254.169.254/latest"}, b"", "93.184.216.34"
            )
        if path == "/failed":
            raise OSError("fixture network failure")
        pages = {
            "/delayed": """<title>Fixture</title><body>Loading
              <script>setTimeout(() => { document.body.innerHTML = `<h1>Account check</h1>
              <form action="https://receiver.test/collect?token=secret" method="POST">
              <input name="user" value="never-persist-this-value"><input type="password"></form>`; }, 400);</script>""",
            "/mobile": """<title>Profile fixture</title><body><script>
              document.body.innerHTML = /Mobile/.test(navigator.userAgent)
                ? '<h1>Mobile account check</h1><form><input type="password"></form>'
                : '<h1>Desktop information</h1>';</script>""",
            "/blocked": """<title>Read only fixture</title><body>Page with side effects
              <script>fetch('/side-effect', {method:'POST',body:'secret'}).catch(()=>{});</script>""",
            "/challenge": "<title>Checking your browser</title><body>Verify you are human",
            "/clean": "<title>Documentation</title><body>Documentation for a harmless example",
            "/subresource-failure": '<body>Readable page<script src="/failed"></script>',
            "/frames": '<body>Parent<iframe src="/clean"></iframe><iframe src="https://other.test/clean"></iframe>',
            "/relative": '<body>Relative links<a href="/path?campaign=private-value">Link</a>',
        }
        body = pages.get(path, "<body>Fixture response").encode()
        return PinnedResponse(200, {"content-type": "text/html"}, body, "93.184.216.34")


@pytest.fixture
def capture_fixture():
    clients, transports = [], []
    with playwright.sync_playwright() as p:

        def open_fixture(url, runtime, **kwargs):
            try:
                browser = p.chromium.launch(headless=True)
            except playwright.Error as error:
                pytest.fail(
                    f"Real browser validation requires Playwright Chromium: {error}"
                )
            client = ManagedClient()
            transport = FixtureTransport()
            clients.append(client)
            transports.append(transport)
            runtime = SimpleNamespace(
                chromium=SimpleNamespace(connect_over_cdp=lambda *a, **k: browser)
            )
            return open_guarded_browser(
                url,
                runtime,
                client_factory=lambda region: client,
                transport=transport,
                **kwargs,
            )

        def run(url, **kwargs):
            request = {
                "url": url,
                "timeout_ms": 2000,
                "profile": "desktop",
                "wait_seconds": 0,
                **kwargs,
            }
            with (
                patch("case_capture.open_guarded_browser", side_effect=open_fixture),
                patch(
                    "socket.getaddrinfo",
                    return_value=[
                        (
                            socket.AF_INET,
                            socket.SOCK_STREAM,
                            0,
                            "",
                            ("93.184.216.34", 443),
                        )
                    ],
                ),
            ):
                return collect_case(request, p)

        yield run, clients, transports
    assert all(c.stopped for c in clients)


def test_delayed_form_network_redirects_and_redacted_case(capture_fixture, tmp_path):
    capture, clients, _ = capture_fixture
    out = tmp_path / "delayed-case"
    url = "https://public.test/redirect"
    new_case(out, url)
    case = add_probe(out, url, wait_seconds=1, capture=capture)
    assert case["probes"][0]["status"] == "complete"
    first, later = case["observations"]
    assert not first["forms"]
    assert later["forms"][0]["fields"][1]["type"] == "password"
    assert later["forms"][0]["action"].endswith("token=REDACTED")
    assert first["content_sha256"] != later["content_sha256"]
    assert first["network_requests"]
    assert any(r["kind"] == "http" for r in first["redirects"])
    assert later["connections"][0]["connected_ip"] == "93.184.216.34"
    assert "never-persist-this-value" not in (out / later["dom_snapshot"]).read_text()
    assert clients[-1].stopped
    # The evidence supports observed variation; it does not prove malicious intent.
    assessed = assess_case(
        out,
        {
            "verdict": "suspicious",
            "assessor": "fixture-test",
            "findings": [
                {
                    "kind": "content_variation",
                    "basis": "observation",
                    "statement": "A form appears after the measured delay",
                    "evidence_ids": ["obs-001", "obs-002"],
                }
            ],
        },
    )
    assert assessed["assessment"]["findings"][0]["evidence_ids"] == [
        "obs-001",
        "obs-002",
    ]


def test_mobile_probe_records_real_profile_difference(capture_fixture, tmp_path):
    capture, _, _ = capture_fixture
    out = tmp_path / "mobile-case"
    url = "https://public.test/mobile"
    new_case(out, url)
    add_probe(out, url, capture=capture)
    case = add_probe(
        out, url, profile="mobile", reason="Compare the mobile view", capture=capture
    )
    desktop, mobile = case["observations"]
    assert "Desktop information" in desktop["visible_text"]
    assert "Mobile account check" in mobile["visible_text"]
    assert "Mobile" in mobile["user_agent"]
    assert mobile["forms"] and not desktop["forms"]


def test_state_changing_request_is_blocked_and_coverage_is_partial(capture_fixture):
    capture, _, transports = capture_fixture
    result = capture("https://public.test/blocked", wait_seconds=1)
    assert all(method != "POST" for method, _ in transports[-1].requests)
    assert result["observations"][0]["blocked_requests"]
    assert result["observations"][0]["status"] == "partial"


@pytest.mark.parametrize("path", ["failed", "challenge"])
def test_failed_or_challenge_view_is_not_complete(capture_fixture, path):
    result = capture_fixture[0](f"https://public.test/{path}")
    assert result["observations"][0]["status"] == "partial"
    assert result["observations"][0]["errors"]
    assert result["cleanup_status"] == "stopped"


def test_blocked_redirect_still_cleans_up_and_does_not_capture(capture_fixture):
    from browser_guard import DestinationRefused

    with pytest.raises(DestinationRefused):
        capture_fixture[0]("https://public.test/to-private")


def test_case_has_no_raw_session_endpoints_or_auth_headers(capture_fixture):
    result = capture_fixture[0]("https://public.test/clean")
    encoded = json.dumps(result)
    assert result["observations"][0]["status"] == "complete"
    assert "wss://" not in encoded
    assert "Authorization" not in encoded
    assert "screenshot_sha256" in encoded


def test_failed_script_cannot_produce_complete_capture(capture_fixture):
    result = capture_fixture[0]("https://public.test/subresource-failure")
    assert result["observations"][0]["status"] == "partial"
    assert "network_requests_failed" in result["observations"][0]["errors"]


def test_frames_are_captured_or_explicitly_incomplete(capture_fixture):
    result = capture_fixture[0]("https://public.test/frames", wait_seconds=1)
    o = result["observations"][-1]
    assert len(o["frames"]) == 2
    if o["status"] == "complete":
        assert all("Documentation" in f["visible_text"] for f in o["frames"])
    else:
        assert o["errors"]


def test_relative_dom_urls_are_redacted(capture_fixture):
    o = capture_fixture[0]("https://public.test/relative")["observations"][0]
    assert "private-value" not in o["dom_snapshot"]
    assert "campaign=REDACTED" in o["dom_snapshot"]
