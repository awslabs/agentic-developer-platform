"""Exercise lazy Browser entry points with only shared packages installed."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[4]


def test_native_consumer_launches_playwright_and_navigates_inert_https_fixture(tmp_path):
    import time
    from functools import partial
    from agentcore_tools.browser_runtime.case_capture import recorded_browser
    from agentcore_tools.browser_runtime.investigation_browser import BrowserInvestigation, InvestigationManager, _Actor
    from agentcore_tools.browser_runtime.native_browser import open_native_browser

    browser_root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ".cache" / "ms-playwright"))
    executables = list(browser_root.glob("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
    if not executables:
        pytest.skip("Playwright Chromium headless shell is not installed")
    chrome = executables[0]
    if b"not found" in subprocess.run(["ldd", str(chrome)], capture_output=True, check=True).stdout:
        pytest.skip("local Chromium shared libraries are not installed")

    first_url = "https://example.com/"
    linked_url = "https://example.com/linked"
    first_page = ("<!doctype html><title>Inert fixture</title><a href='/linked'>Next page</a>"
                  "<form action='/submit' method='POST'><input name='value'><button>Inert form</button></form>")
    linked_page = "<!doctype html><title>Linked fixture</title><h1>Changed page marker</h1>"
    profile = tmp_path / "chromium"
    browser_process = subprocess.Popen([str(chrome), "--headless", "--no-sandbox", "--disable-gpu",
        "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
        "--user-data-dir=" + str(profile), "about:blank"], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    manager = None
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 5
        while not port_file.is_file() and browser_process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert port_file.is_file() and browser_process.poll() is None
        cdp_url = "http://127.0.0.1:" + port_file.read_text().splitlines()[0]
        requested = []
        started = []
        stopped = []

        class Provider:
            def start(self, *, session_timeout_seconds):
                started.append(session_timeout_seconds)
                return "local-provider"

            def generate_ws_headers(self):
                return cdp_url, {"X-Fixture": "local"}

            def stop(self):
                stopped.append(True)

        def opener(url, playwright, **options):
            session = open_native_browser(url, playwright, client_factory=lambda **kwargs: Provider(), **options)

            def fixture(route):
                request = route.request
                requested.append((request.method, request.url))
                if request.method == "GET" and request.url in {first_url, linked_url}:
                    route.fulfill(status=200, content_type="text/html",
                                  body=first_page if request.url == first_url else linked_page)
                else:
                    route.abort("blockedbyclient")

            session._context.route("**/*", fixture)
            return session

        def factory(payload, playwright):
            return BrowserInvestigation(payload, playwright,
                recorder_factory=partial(recorded_browser, opener=opener))

        manager = InvestigationManager(factory=factory, actor_factory=_Actor)
        opened = manager.start({"url": first_url, "scope": "host", "deadline_at": int(time.time()) + 300})
        assert opened["manifest"]["session_id"] == "local-provider"
        assert opened["observations"][0]["page_title"] == "Inert fixture"
        assert opened["observations"][0]["counts"]["forms"] == 1
        choices = opened["choices"]
        assert len(choices) == 1 and choices[0]["text"] == "Next page"
        assert choices[0]["url"] == linked_url and choices[0]["action"] == "follow"
        followed = manager.request({"session_token": opened["session_token"],
            "view_id": opened["view_id"], "action": "follow", "candidate_id": choices[0]["id"]})
        assert followed["view_id"] != opened["view_id"]
        assert followed["observations"][0]["page_title"] == "Linked fixture"
        assert "Changed page marker" in followed["observations"][0]["visible_text"]
        assert requested == [("GET", first_url), ("GET", linked_url)]
        closed = manager.request({"session_token": opened["session_token"], "action": "close"})
        assert closed == {"session_id": "local-provider", "cleanup_status": "stopped", "session_open": False}
        assert len(started) == 1 and 0 < started[0] <= 300 and stopped == [True]
    finally:
        if manager is not None:
            manager.close_all()
        browser_process.terminate()
        try:
            browser_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            browser_process.kill()
            browser_process.wait(timeout=5)
        assert browser_process.poll() is not None


def test_inspected_choice_is_bound_to_one_browser_view():
    from agentcore_tools.browser_runtime.investigation_browser import BrowserInvestigation, InvestigationError

    class Page:
        url = "https://fixture.example/"
        clicks = 0

        def evaluate(self, expression, selector):
            if selector != "a[href]" or self.url.endswith("/linked"):
                return []
            return [{"index": 0, "href": "https://fixture.example/linked", "text": "Next page",
                     "download": False, "target": "", "in_form": False, "visible": True},
                    {"index": 1, "href": "https://fixture.example/submit", "text": "Submit form",
                     "download": False, "target": "", "in_form": True, "visible": True}]

        def click_observed(self, selector, index, expected):
            assert selector == "a[href]" and index == 0
            self.url = expected["href"]
            self.clicks += 1

    class Recorder:
        def __init__(self, browser):
            self.browser = browser

        def send(self, action):
            action["perform"](self.browser.session)
            self.browser.result["observations"].append({"page_title": "Linked fixture", "errors": []})
            return self.browser.result, None

    def browser():
        instance = BrowserInvestigation.__new__(BrowserInvestigation)
        instance.request = {"scope": "host"}
        instance.host = "fixture.example"
        instance.steps = 1
        instance.closed = False
        instance.session = Page()
        instance.result = {"observations": [{"page_title": "Inert fixture", "errors": []}]}
        instance.recorder = Recorder(instance)
        instance.last = instance.packet(0)
        return instance

    first, foreign = browser(), browser()
    view = first.last
    assert len(view["choices"]) == 1 and view["choices"][0]["text"] == "Next page"
    assert view["choices"][0]["id"] != foreign.last["choices"][0]["id"]
    with pytest.raises(InvestigationError, match="observed choices"):
        first.step({"view_id": view["view_id"], "action": "follow",
                    "candidate_id": foreign.last["choices"][0]["id"]})
    assert first.session.clicks == 0
    followed = first.step({"view_id": view["view_id"], "action": "follow",
                           "candidate_id": view["choices"][0]["id"]})
    assert first.session.clicks == 1 and first.session.url.endswith("/linked")
    assert followed["view_id"] != view["view_id"]
    assert followed["observations"][0]["page_title"] == "Linked fixture"
    with pytest.raises(InvestigationError, match="Stale browser view"):
        first.step({"view_id": view["view_id"], "action": "follow",
                    "candidate_id": view["choices"][0]["id"]})
    assert first.session.clicks == 1



def test_native_browser_rejects_mutations_and_prohibited_destinations_before_network():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from agentcore_tools.browser_runtime.denylist import DenylistResult
    from agentcore_tools.browser_runtime.native_browser import open_native_browser

    provider = Mock()
    provider.start.return_value = "private-session"
    provider.generate_ws_headers.return_value = ("wss://provider.example", {})
    playwright = Mock()

    def scope(url):
        return DenylistResult(allowed=not url.endswith("/outside"),
                              reason="outside scope", reason_code="outside_investigation_scope")

    session = open_native_browser("https://1.1.1.1/", playwright,
                                  client_factory=lambda **kwargs: provider,
                                  read_only=True, navigation_check=scope)
    context = playwright.chromium.connect_over_cdp.return_value.new_context.return_value
    assert context.route.call_count == 1
    assert playwright.chromium.connect_over_cdp.return_value.new_context.call_args.kwargs["service_workers"] == "block"
    callback = context.route.call_args.args[1]

    def request(url, method="GET", navigation=True):
        route = Mock()
        route.request = SimpleNamespace(url=url, method=method,
                                        is_navigation_request=lambda: navigation)
        callback(route)
        return route

    posted = request("https://1.1.1.1/form", "POST")
    posted.abort.assert_called_once_with("blockedbyclient")
    assert session.refusals[-1]["reason_code"] == "method_not_allowed"
    scope_refused = request("https://1.1.1.1/outside")
    scope_refused.abort.assert_called_once_with("blockedbyclient")
    assert session.refusals[-1]["reason_code"] == "outside_investigation_scope"
    private = request("http://127.0.0.1/private", navigation=False)
    private.abort.assert_called_once_with("blockedbyclient")
    assert session.refusals[-1]["reason_code"] == "blocked_address"
    permitted = request("https://1.1.1.1/linked")
    permitted.continue_.assert_called_once_with()
    assert posted.continue_.call_count == scope_refused.continue_.call_count == private.continue_.call_count == 0
    websocket_close = Mock()
    context.route_web_socket.call_args.args[1](Mock(close=websocket_close))
    websocket_close.assert_called_once_with()
    session.close()


def test_shared_browser_startup_without_cyber_or_path_mutation(tmp_path):
    script = r'''
import sys
from unittest.mock import Mock, patch
before = list(sys.path)
from agentcore_tools.browser_runtime import local_browser, investigation_browser, native_browser
from agentcore_tools.browser_runtime.errors import BrowserBrokerError
assert sys.path == before
assert investigation_browser.validate_start({"url": "https://example.com"})["url"] == "https://example.com"
for url in ("https://user:pass@example.com", "https://example.com/?token=secret"):
    try:
        investigation_browser.validate_start({"url": url})
    except ValueError:
        pass
    else:
        raise AssertionError("credential URL accepted")
# Exercise the actual lazy imports and process entry point without provider work.
with patch.object(local_browser.subprocess, "Popen", return_value=Mock(poll=lambda: 1)) as launch:
    try:
        local_browser.investigation_request("start", {"url": "https://example.com"})
    except BrowserBrokerError as error:
        assert error.code == "session_ended"
    else:
        raise AssertionError("dead child was accepted")
    assert launch.call_args.args[0][1:3] == ["-m", "agentcore_tools.browser_runtime.local_browser"]
provider = Mock()
provider.start.return_value = "session"
provider.generate_ws_headers.return_value = ("wss://example.com", {})
playwright = Mock()
session = native_browser.open_native_browser("https://example.com", playwright, client_factory=lambda **kw: provider)
assert session is not None and provider.start.call_count == 1
assert not any(name in sys.modules for name in ("cyber_tools", "research_case", "browser_client", "case_capture"))
'''
    with tempfile.TemporaryDirectory(prefix="b6671-") as short_tmp:
        env = {**os.environ, "PYTHONPATH": str(ROOT / "modules/tools/agentcore"), "TMPDIR": short_tmp}
        subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, check=True, timeout=20)


def test_browser_stop_needs_terminated_readback(monkeypatch):
    import boto3
    from agentcore_tools.browser_runtime.isolated_browser import stop_session

    class Provider:
        def __init__(self):
            self.status = "READY"
            self.calls = []

        def get_browser_session(self, **kwargs):
            self.calls.append("get")
            return {"status": self.status}

        def stop_browser_session(self, **kwargs):
            self.calls.append("stop")
            return {}

    provider = Provider()
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: provider)
    assert stop_session("private-session") is False
    assert provider.calls == ["get", "stop", "get"]
    provider.status = "TERMINATED"
    assert stop_session("private-session") is True
    assert provider.calls[-1] == "get"


def test_browser_supervisor_disregards_unverified_worker_stop(monkeypatch):
    from threading import Event, Lock, RLock
    from unittest.mock import Mock
    from agentcore_tools.browser_runtime import isolated_browser

    actor = isolated_browser.ProcessActor.__new__(isolated_browser.ProcessActor)
    actor.cleanup_lock = Lock()
    actor.ended = Event()
    actor.process = Mock(pid=123, poll=lambda: 0)
    actor.lock = RLock()
    actor.pending = {}
    actor.on_exit = lambda actor: None
    actor.session_id = "private-session"
    actor.close_result = {"cleanup_status": "stopped", "session_open": False}
    actor.stop = lambda session_id: False
    monkeypatch.setattr(isolated_browser.os, "killpg", lambda *args: None)
    actor.abort()
    assert actor.ended.is_set()
    assert actor.close_result["cleanup_status"] == "unknown"


def test_browser_close_returns_supervisor_verdict_not_worker_acknowledgement():
    from threading import Event, Lock, RLock
    from agentcore_tools.browser_runtime.isolated_browser import ProcessActor

    actor = ProcessActor.__new__(ProcessActor)
    actor.ended = Event()
    actor.call_lock = Lock()
    actor.lock = RLock()
    actor.pending = {}
    actor.sequence = 0
    actor.action_seconds = 1
    actor.close_result = {"cleanup_status": "unknown", "session_open": False}

    def send(message):
        actor.pending[message["id"]].set_result({"cleanup_status": "stopped", "session_open": False})
        actor.ended.set()

    actor._send = send
    assert actor.call({"action": "close"}) == actor.close_result
    assert actor.close_result["cleanup_status"] == "unknown"


def test_browser_start_deadline_is_checked_before_provider_and_limits_lease(monkeypatch):
    import time
    from unittest.mock import Mock
    import pytest
    from agentcore_tools.browser_runtime import investigation_browser, native_browser
    from agentcore_tools.browser_runtime.runtime_limits import LEASE_SECONDS

    current = int(time.time())
    with pytest.raises(investigation_browser.InvestigationError):
        investigation_browser.validate_start({"url": "https://example.com", "deadline_at": current})
    with pytest.raises(investigation_browser.InvestigationError):
        investigation_browser.validate_start({"url": "https://example.com", "deadline_at": "300"})
    assert investigation_browser.validate_start({"url": "https://example.com",
                                                 "deadline_at": current + 30})["deadline_at"] == current + 30
    provider = Mock()
    provider.start.return_value = "session"
    provider.generate_ws_headers.return_value = ("wss://example.com", {})
    session = native_browser.open_native_browser("https://example.com", Mock(),
        client_factory=lambda **kwargs: provider, deadline_at=current + 30)
    assert 1 <= provider.start.call_args.kwargs["session_timeout_seconds"] <= 30
    session.close()
    assert LEASE_SECONDS >= 30
    provider.reset_mock()
    with pytest.raises(ValueError, match="authority expired"):
        native_browser.open_native_browser("https://example.com", Mock(),
            client_factory=lambda **kwargs: provider, deadline_at=current - 1)
    provider.start.assert_not_called()


def test_browser_worker_lease_cannot_outlive_the_signed_deadline(monkeypatch):
    import json
    import time
    from types import SimpleNamespace
    from unittest.mock import Mock
    import pytest
    from agentcore_tools.browser_runtime import isolated_browser
    from agentcore_tools.browser_runtime.investigation_browser import InvestigationError

    process = Mock()
    process.stdin = Mock()
    launched = Mock(return_value=process)
    monkeypatch.setattr(isolated_browser.subprocess, "Popen", launched)
    monkeypatch.setattr(isolated_browser.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    with pytest.raises(InvestigationError, match="authority expired"):
        isolated_browser.ProcessActor({"url": "https://example.com", "deadline_at": int(time.time()) - 1},
                                      None, lambda _: None)
    launched.assert_not_called()
    deadline = int(time.time()) + 30
    actor = isolated_browser.ProcessActor({"url": "https://example.com", "deadline_at": deadline},
                                          None, lambda _: None)
    assert actor.deadline <= time.monotonic() + 30
    assert json.loads(process.stdin.write.call_args.args[0])["payload"]["deadline_at"] == deadline
