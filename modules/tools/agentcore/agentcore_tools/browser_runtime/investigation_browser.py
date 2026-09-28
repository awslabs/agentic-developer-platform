"""Bounded browser leases for the existing cyber agent's investigation loop.

No model runs here. The reasoning worker selects each next action from observed
browser affordances; this service owns Playwright, destinations and session caps.
"""

from __future__ import annotations

import copy
import queue
import secrets
import threading
import time
from concurrent.futures import Future
from urllib.parse import urlsplit, urlunsplit

from agentcore_tools.browser_runtime.case_capture import recorded_browser
from agentcore_tools.browser_runtime.case_contract import redact_url, sanitize
from agentcore_tools.browser_runtime.runtime_limits import (
    LEASE_SECONDS,
    NAVIGATION_SECONDS,
    STARTUP_SECONDS,
    ACTION_SECONDS,
)
from agentcore_tools.browser_runtime.denylist import DenylistResult, canonical_hostname

MAX_STEPS = 12
MAX_SESSIONS = 4
LINK_SELECTOR = "a[href]"
CONTROL_SELECTOR = 'summary,button[type="button"][aria-expanded],[role="tab"]'
ELEMENTS = """selector => Array.from(document.querySelectorAll(selector)).slice(0,50)
 .map((e,index) => ({index, href:e.href || '',
 text:(e.innerText || e.textContent || '').slice(0,150),
 download:e.hasAttribute('download'), target:e.getAttribute('target') || '', in_form:!!e.closest('form'),
 visible:!!(e.getClientRects().length)}))"""


class InvestigationError(ValueError):
    def __init__(
        self,
        message,
        *,
        code="invalid_investigation",
        retry_after=None,
        cleanup=None,
        browser_start_unattempted=False,
    ):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after
        self.cleanup = cleanup
        self.browser_start_unattempted = browser_start_unattempted


def validate_start(payload):
    if not isinstance(payload, dict) or set(payload) - {"url", "profile", "scope"}:
        raise InvestigationError("Unsupported investigation options")
    url = payload.get("url")
    if not isinstance(url, str) or len(url) > 8192:
        raise InvestigationError("An absolute HTTP(S) seed URL is required")
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise InvestigationError("An absolute HTTP(S) seed URL is required")
    # Apply the maintained input check; this module is trusted broker code.
    from agentcore_tools.browser_runtime.input_validation import validate_input as _validate_input

    _validate_input(url)
    if payload.get("scope", "observed_external") not in {"host", "observed_external"}:
        raise InvestigationError("scope must be host or observed_external")
    if payload.get("profile", "desktop") not in {"desktop", "mobile"}:
        raise InvestigationError("profile must be desktop or mobile")
    return {
        "url": url,
        "profile": payload.get("profile", "desktop"),
        "scope": payload.get("scope", "observed_external"),
    }


class BrowserInvestigation:
    """Owned entirely by one actor thread, including while the agent reasons."""

    def __init__(
        self,
        request,
        playwright,
        recorder_factory=recorded_browser,
        on_observation=None,
    ):
        self.request = validate_start(request)
        self.host = canonical_hostname(urlsplit(request["url"]).hostname)
        p = urlsplit(request["url"])
        self.root = urlunsplit((p.scheme, p.netloc, "/", "", ""))
        self.steps = 1
        self.closed = False
        self.candidates = {}
        self.view_id = ""

        def checkpoint(observation, manifest):
            if on_observation:
                on_observation(
                    {
                        "schema_version": "domain-investigation/1",
                        "view_id": "",
                        "observations": [observation],
                        "manifest": manifest,
                        "choices": [],
                        "external_leads": [],
                        "steps_used": self.steps,
                        "max_steps": MAX_STEPS,
                        "session_open": True,
                    }
                )

        self.recorder = recorder_factory(
            {
                **self.request,
                "wait_seconds": 0,
                "timeout_ms": NAVIGATION_SECONDS * 1000,
                "_screenshots": False,
                "_capture_frames": False,
                "_navigation_check": self.navigation_check,
                "_on_observation": checkpoint if on_observation else None,
            },
            playwright,
        )
        try:
            self.result, self.session = next(self.recorder)
            self.last = self.packet(0)
        except BaseException:
            self.recorder.close()
            raise

    def navigation_check(self, url):
        from agentcore_tools.browser_runtime.input_validation import validate_input as _validate_input

        try:
            _validate_input(url)
            host = canonical_hostname(urlsplit(url).hostname)
        except (ValueError, TypeError):
            return DenylistResult(
                allowed=False,
                reason="Unsafe navigation input",
                reason_code="invalid_navigation",
            )
        allowed = (
            self.request["scope"] == "observed_external"
            or host == self.host
            or host.endswith("." + self.host)
        )
        return DenylistResult(
            allowed=allowed,
            reason="" if allowed else "Navigation leaves the investigation hostname",
            reason_code="" if allowed else "outside_investigation_scope",
        )

    def packet(self, start):
        self.view_id = secrets.token_hex(12)
        self.candidates = {}
        choices, leads = [], []
        for kind, selector in [("follow", LINK_SELECTOR), ("expand", CONTROL_SELECTOR)]:
            for row in self.session.evaluate(ELEMENTS, selector):
                if not row["visible"] or row["in_form"] or row["download"]:
                    continue
                if kind == "follow" and not self.navigation_check(row["href"]).allowed:
                    leads.append(
                        {
                            "url": redact_url(row["href"]),
                            "text": row["text"],
                            "reason": "outside_scope_or_unsafe_navigation",
                        }
                    )
                    continue
                ident = f"{kind}-{len(self.candidates) + 1:03d}"
                expected = {k: row[k] for k in ("href", "text", "download", "target")}
                self.candidates[ident] = (kind, selector, row["index"], expected)
                choices.append(
                    {
                        "id": ident,
                        "action": kind,
                        "text": row["text"],
                        "url": redact_url(row["href"]) if row["href"] else "",
                    }
                )
        metadata = {k: v for k, v in self.result.items() if k != "observations"}
        observations = copy.deepcopy(self.result["observations"][start:])
        # Previous evidence has been returned to the worker. Keep metadata for
        # provenance and cleanup, not every full-size screenshot in broker RAM.
        for observation in self.result["observations"]:
            observation.pop("screenshot_base64", None)
            observation.pop("dom_snapshot", None)
        return sanitize(
            {
                "schema_version": "domain-investigation/1",
                "view_id": self.view_id,
                "observations": observations,
                "manifest": metadata,
                "choices": choices,
                "external_leads": leads,
                "steps_used": self.steps,
                "max_steps": MAX_STEPS,
                "session_open": not self.closed,
            }
        )

    def step(self, payload):
        if set(payload) - {
            "session_token",
            "view_id",
            "action",
            "candidate_id",
            "seconds",
            "url",
        }:
            raise InvestigationError("Unsupported action fields")
        if self.closed or self.steps >= MAX_STEPS:
            raise InvestigationError("Investigation session or step budget ended")
        if payload.get("view_id") != self.view_id:
            raise InvestigationError("Stale browser view; do not replay an old action")
        action = payload.get("action")
        if action != "screenshot" and any(
            {
                "challenge_or_interstitial",
                "human_verification_challenge",
                "threat_warning",
            }
            & set(o.get("errors", []))
            for o in self.last["observations"]
        ):
            raise InvestigationError(
                "Challenge encountered; close and report it without bypass"
            )
        target = self.session.url
        if action in {"follow", "expand"}:
            row = self.candidates.get(payload.get("candidate_id"))
            if not row or row[0] != action:
                raise InvestigationError(
                    "Select an action from the current observed choices"
                )
            _, selector, index, expected = row
            if action == "follow":
                target = expected["href"]

            def perform(session):
                return session.click_observed(selector, index, expected)

        elif action in {"root", "navigate"}:
            target = self.root if action == "root" else payload.get("url")
            if not isinstance(target, str) or not self.navigation_check(target).allowed:
                raise InvestigationError(
                    "Navigation URL is invalid or outside the authorized scope"
                )

            def perform(session):
                return session.goto(
                    target,
                    wait_until="domcontentloaded",
                    timeout=NAVIGATION_SECONDS * 1000,
                )

        elif action == "screenshot":

            def perform(session):
                return None

        elif action == "back":

            def perform(session):
                return session.go_back()

        elif action == "scroll":

            def perform(session):
                return session.scroll_view()

        elif action == "wait":
            seconds = payload.get("seconds")
            if type(seconds) is not int or not 1 <= seconds <= 15:
                raise InvestigationError("Wait must be 1–15 seconds")

            def perform(session):
                return session.wait_for_timeout(seconds * 1000)

        else:
            raise InvestigationError(
                "Action must be follow, expand, root, navigate, screenshot, back, scroll or wait"
            )
        self.steps += 1
        start = len(self.result["observations"])
        try:
            self.result, _ = self.recorder.send(
                {"action": action, "target_url": target, "perform": perform}
            )
            self.last = self.packet(start)
            if self.steps == MAX_STEPS:
                self.close()
                self.last["session_open"] = False
                self.last["manifest"]["cleanup_status"] = self.result["cleanup_status"]
            return self.last
        except BaseException:
            self.close()
            raise

    def pump(self):
        # Service CDP interception during the agent's reasoning interval, retaining
        # the recorder's network/navigation callbacks until the next observation.
        self.session.wait_for_timeout(50)

    def close(self):
        if not self.closed:
            self.closed = True
            self.recorder.close()
        return {
            "session_id": self.result["session_id"],
            "cleanup_status": self.result["cleanup_status"],
            "session_open": False,
        }


class _Actor:
    def __init__(self, payload, factory, on_exit):
        self.payload, self.factory, self.on_exit = payload, factory, on_exit
        self.inbox = queue.Queue(maxsize=2)
        self.ready = Future()
        self.ended = threading.Event()
        self.close_result = None
        self.deadline = time.monotonic() + LEASE_SECONDS
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        browser = None
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = self.factory(self.payload, playwright)
                self.ready.set_result(browser.last)
                while time.monotonic() < self.deadline and not browser.closed:
                    try:
                        payload, future = self.inbox.get_nowait()
                    except queue.Empty:
                        browser.pump()
                        continue
                    try:
                        if payload["action"] == "close":
                            self.close_result = browser.close()
                            future.set_result(self.close_result)
                            break
                        future.set_result(browser.step(payload))
                    except Exception as error:
                        future.set_exception(error)
                self.close_result = browser.close()
        except BaseException as error:
            if not self.ready.done():
                self.ready.set_exception(error)
        finally:
            if browser is not None:
                self.close_result = browser.close()
            self.ended.set()
            while not self.inbox.empty():
                payload, future = self.inbox.get_nowait()
                if not future.done():
                    if payload.get("action") == "close" and self.close_result:
                        future.set_result(self.close_result)
                    else:
                        future.set_exception(
                            InvestigationError("Session ended; retain earlier evidence")
                        )
            self.on_exit(self)

    def call(self, payload):
        if self.ended.is_set():
            if payload.get("action") == "close" and self.close_result:
                return self.close_result
            raise InvestigationError("Browser lease ended; retain earlier evidence")
        future = Future()
        try:
            self.inbox.put_nowait((payload, future))
        except queue.Full:
            raise InvestigationError("A browser action is already pending")
        try:
            return future.result(timeout=ACTION_SECONDS)
        except TimeoutError:
            # Do not replay an uncertain action. Managed service timeout is the
            # backstop if a browser/driver failure prevents prompt cleanup.
            self.deadline = 0
            raise InvestigationError("Browser action timed out; do not replay it")


class InvestigationManager:
    def __init__(self, factory=BrowserInvestigation, *, actor_factory=None):
        from agentcore_tools.browser_runtime.isolated_browser import ProcessActor

        self.factory = factory
        self.actor_factory = actor_factory or ProcessActor
        self.actors = {}
        self.lock = threading.Lock()

    def start(self, payload):
        payload = validate_start(payload)
        with self.lock:
            # Retain closed results briefly so explicit close is idempotent, but
            # never let completed capabilities accumulate without a bound.
            self.actors = {
                k: v
                for k, v in self.actors.items()
                if not v.ended.is_set() or time.monotonic() < v.deadline + 30
            }
            while len(self.actors) >= 12:
                retired = next(
                    (k for k, v in self.actors.items() if v.ended.is_set()), None
                )
                if retired is None:
                    break
                del self.actors[retired]
            if sum(not a.ended.is_set() for a in self.actors.values()) >= MAX_SESSIONS:
                raise InvestigationError(
                    "Broker investigation capacity is busy",
                    code="capacity_busy",
                    retry_after=5,
                    browser_start_unattempted=True,
                )
            token = secrets.token_urlsafe(32)
            actor = self.actor_factory(payload, self.factory, lambda actor: None)
            self.actors[token] = actor
        if hasattr(actor, "initial"):
            packet = actor.initial()
            return {**packet, "session_token": token, "lease_seconds": LEASE_SECONDS}
        try:
            packet = actor.ready.result(timeout=STARTUP_SECONDS)
        except TimeoutError:
            actor.deadline = 0
            raise InvestigationError("Browser startup timed out")
        return {**packet, "session_token": token, "lease_seconds": LEASE_SECONDS}

    def request(self, payload):
        if not isinstance(payload, dict) or not isinstance(
            payload.get("session_token"), str
        ):
            raise InvestigationError("A browser lease is required")
        with self.lock:
            actor = self.actors.get(payload["session_token"])
        if actor is None:
            raise InvestigationError(
                "Browser lease unavailable; context cannot be recreated or replayed"
            )
        return actor.call(payload)

    def close_all(self):
        with self.lock:
            actors = list(self.actors.values())
        for actor in actors:
            if hasattr(actor, "abort"):
                actor.abort()
            else:
                actor.deadline = 0
        for actor in actors:
            actor.thread.join(timeout=5)

    def capacity(self):
        with self.lock:
            active = sum(not actor.ended.is_set() for actor in self.actors.values())
        return {
            "active_sessions": active,
            "max_sessions": MAX_SESSIONS,
            "accepting_starts": active < MAX_SESSIONS,
        }

    def cancel(self, token):
        with self.lock:
            actor = self.actors.get(token)
        if actor:
            if hasattr(actor, "abort"):
                actor.abort()
            else:
                actor.deadline = 0
