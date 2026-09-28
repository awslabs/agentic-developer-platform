"""Research evidence recorder for direct and legacy AgentCore sessions."""

from __future__ import annotations

import base64
import copy
import os
from importlib.metadata import version
from urllib.parse import urljoin

from agentcore_tools.browser_runtime.browser_guard import DEFAULT_REGION, DestinationRefused, open_guarded_browser
from agentcore_tools.browser_runtime.case_contract import (
    COLLECTOR_VERSION,
    SCHEMA_VERSION,
    content_digest,
    digest,
    redact_url,
    sanitize,
    utcnow,
)
from agentcore_tools.browser_runtime.denylist import DenylistResult
from agentcore_tools.browser_runtime.evidence_items import build_evidence_items, inventory_digest

MAX_EVENTS = 200
PROFILES = {
    "desktop": {"viewport": {"width": 1440, "height": 900}, "locale": "en-US"},
    "mobile": {
        "viewport": {"width": 390, "height": 844},
        "locale": "en-US",
        "is_mobile": True,
        "has_touch": True,
        "device_scale_factor": 1,
        "user_agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36",
    },
}

# Fixed code owned by the collector. The caller cannot supply JavaScript.
PAGE_DATA = """() => {
 // Inspect existing inline source only. Never fetch or execute agent-supplied code.
 // Relevant handlers get priority over analytics/bootstrap scripts in a fixed budget.
 let remainingScriptChars = 131072;
 const candidates = Array.from(document.scripts).map((s, index) => ({
   s, index, relevant: !s.src && /password|submit|FormData|fetch\\s*\\(|XMLHttpRequest|sendBeacon/i.test(s.textContent || '')
 }));
 const scripts = candidates.sort((a, b) => Number(b.relevant) - Number(a.relevant) || a.index - b.index)
   .slice(0, 20).map(({s, index, relevant}) => {
     const source = s.src ? '' : (s.textContent || '');
     const budget = Math.min(relevant ? 32768 : 1200, remainingScriptChars);
     const inline = source.slice(0, budget);
     remainingScriptChars -= inline.length;
     return {src: s.src, inline, document_index: index, relevant,
       original_chars: source.length, captured_chars: inline.length,
       truncated: source.length > inline.length};
   });
 const root = document.documentElement.cloneNode(true);
 root.querySelectorAll('script,style,noscript').forEach(e => e.remove());
 root.querySelectorAll('input,textarea,select').forEach(e => {
   e.removeAttribute('value'); if (e.tagName !== 'INPUT') e.textContent = '';
 });
 root.querySelectorAll('*').forEach(e => Array.from(e.attributes).forEach(a => {
   if (!['href','src','action','method','type','name','role','alt'].includes(a.name))
     e.removeAttribute(a.name);
   else if (['href','src','action'].includes(a.name)) {
     try { e.setAttribute(a.name, new URL(a.value, document.baseURI).href); }
     catch { e.removeAttribute(a.name); }
   }
 }));
 return {
  page_title: document.title.slice(0, 500),
  visible_text: (document.body?.innerText || '').slice(0, 20000),
  dom_snapshot: root.outerHTML.slice(0, 100000),
  forms: Array.from(document.forms).slice(0, 30).map(f => ({
    action: f.action, method: f.method,
    fields_truncated: f.querySelectorAll('input,select,textarea').length > 50,
    fields: Array.from(f.querySelectorAll('input,select,textarea'))
      .slice(0, 50).map(i => ({name: i.name, type: i.type, hidden: i.type === 'hidden'}))
  })),
  orphan_inputs: Array.from(document.querySelectorAll('input[type=password],input[type=email]'))
    .filter(i => !i.closest('form')).slice(0, 30).map(i => ({name: i.name, type: i.type})),
  links: Array.from(document.querySelectorAll('a[href]')).slice(0, 50)
    .map(a => ({url: a.href, text: a.innerText.slice(0, 150)})),
  scripts,
  counts: {forms: document.forms.length, links: document.querySelectorAll('a[href]').length,
    scripts: document.scripts.length, text_chars: document.body?.innerText.length || 0,
    dom_chars: root.outerHTML.length},
  nested_capture_truncated: document.title.length > 500 ||
    Array.from(document.forms).some(f => f.querySelectorAll('input,select,textarea').length > 50) ||
    scripts.some(s => s.truncated) ||
    Array.from(document.querySelectorAll('a[href]')).some(a => a.innerText.length > 150) ||
    Array.from(document.querySelectorAll('input[type=password],input[type=email]'))
      .filter(i => !i.closest('form')).length > 30,
  user_agent: navigator.userAgent
 };
}"""

LIMITATIONS = [
    "Only the recorded browser profile, time and broker egress were tested; HTTP sockets originate at the broker.",
    "Mobile is a fixed emulation profile, not a physical device; its user agent may differ from the actual browser version.",
    "Out-of-process frames/workers without CDP interception remain offline and may be unavailable.",
    "State-changing HTTP methods, service workers, WebSockets and popups are blocked; this can alter behavior.",
    "Screenshots show the viewport. DOM, text, scripts, frames and network metadata are bounded.",
    "Request/response bodies and authentication headers are not saved in the network log; it is not a full HAR.",
    "Downloads are offers/metadata only. Payload execution and file hashing are not performed.",
    "Userinfo, fragments and URL query values are redacted; page text/images may still contain sensitive content.",
    "Changing content across observations alone does not prove intentional cloaking.",
]


def validate_options(payload: dict) -> tuple[str, int]:
    profile = payload.get("profile", "desktop")
    delay = payload.get("wait_seconds", 0)
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ValueError("profile must be desktop or mobile")
    if isinstance(delay, bool) or not isinstance(delay, int) or not 0 <= delay <= 15:
        raise ValueError("wait_seconds must be an integer from 0 to 15")
    return profile, delay


def _navigation_refusal(session) -> None:
    for item in session.refusals:
        if item.get("navigation"):
            raise DestinationRefused(
                str(item["url"]),
                DenylistResult(
                    allowed=False,
                    reason=str(item["reason"]),
                    reason_code=str(item["reason_code"]),
                ),
            )


NATIVE_LIMITATIONS = [
    "Only the recorded profile, time and AgentCore Browser egress were tested.",
    "Page networking, scripts, service workers, WebSockets and popups use native Chromium behavior.",
    "AgentCore isolates browser sessions; this collector does not filter every page request or pin DNS destinations.",
    *LIMITATIONS[1:2],
    *LIMITATIONS[4:],
]


def recorded_browser(request: dict, playwright, *, opener=None):
    """Broker-owned recorder; yield between agent decisions without closing context.

    Commands are trusted Python callbacks supplied by the broker, never caller code.
    The generator and all Playwright operations must stay on their owning thread.
    """
    profile, delay = validate_options(request)
    if opener is None:
        if os.environ.get("URL_ANALYSIS_BROWSER_MODE", "native") == "broker":
            opener = open_guarded_browser
        else:
            from agentcore_tools.browser_runtime.native_browser import open_native_browser

            opener = open_native_browser
    native = (
        getattr(getattr(opener, "func", opener), "__name__", "")
        == "open_native_browser"
    )
    started = utcnow()
    subject = digest(request["url"])
    result = {
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "subject_sha256": subject,
        "target_url": redact_url(request["url"]),
        "started_at": started,
        "region": os.environ.get("AWS_REGION", DEFAULT_REGION),
        "profile": profile,
        "profile_options": PROFILES[profile],
        "observations": [],
        "limitations": (NATIVE_LIMITATIONS if native else LIMITATIONS).copy(),
        "browser_transport": "agentcore_native" if native else "broker_pinned",
        "cleanup_status": "not_started",
    }
    network, redirects, downloads, timeline = [], [], [], []
    dropped = {"network": 0, "redirects": 0, "downloads": 0, "timeline": 0}
    status = 0
    previous_url = request["url"]
    subjects_by_url = {}
    manual_navigation = None
    errors = []

    def append(kind, items, value):
        if len(items) < MAX_EVENTS:
            items.append(sanitize({"at": utcnow(), **value}))
        else:
            dropped[kind] += 1

    session = opener(
        request["url"],
        playwright,
        region=result["region"],
        context_options={
            **PROFILES[profile],
            **request.get("_context_options", {}),
            "accept_downloads": False,
        },
        read_only=True,
        navigation_check=request.get("_navigation_check"),
    )

    def on_response(response):
        nonlocal status
        headers = response.headers
        navigation = (
            response.request.is_navigation_request()
            and response.frame == session.main_frame
        )
        if navigation:
            status = response.status
        append(
            "network",
            network,
            {
                "url": response.url,
                "method": response.request.method,
                "resource_type": response.request.resource_type,
                "status": response.status,
                "mime": headers.get("content-type", ""),
                "main_navigation": navigation,
            },
        )
        if navigation and 300 <= response.status < 400 and headers.get("location"):
            append(
                "redirects",
                redirects,
                {
                    "from_url": response.url,
                    "to_url": urljoin(response.url, headers["location"]),
                    "status": response.status,
                    "kind": "http",
                },
            )
        if "attachment" in headers.get("content-disposition", "").lower():
            append(
                "downloads",
                downloads,
                {
                    "url": response.url,
                    "mime": headers.get("content-type", ""),
                    "basis": "response-header",
                    "executed": False,
                    "hash_available": False,
                },
            )

    def on_navigation(frame):
        nonlocal previous_url, manual_navigation
        if frame == session.main_frame:
            append("timeline", timeline, {"event": "navigation", "url": frame.url})
            if frame.url != previous_url:
                kind = "navigation"
                if not frame.url.startswith(("https://", "http://")):
                    kind = "browser_internal"
                if manual_navigation and (
                    manual_navigation == "back" or frame.url == manual_navigation
                ):
                    kind = "agent_navigation"
                manual_navigation = None
                append(
                    "redirects",
                    redirects,
                    {
                        "from_url": previous_url,
                        "to_url": frame.url,
                        "kind": kind,
                        "status": 0,
                    },
                )
            previous_url = frame.url

    def on_download(download):
        append(
            "downloads",
            downloads,
            {
                "url": download.url,
                "suggested_filename": download.suggested_filename,
                "basis": "download-event",
                "executed": False,
                "hash_available": False,
            },
        )
        download.cancel()

    def checkpoint(observation):
        callback = request.get("_on_observation")
        if callback is None:
            return
        partial = copy.deepcopy(observation)
        partial.update(
            status="partial",
            network_requests=copy.deepcopy(network),
            redirects=copy.deepcopy(redirects),
            downloads=copy.deepcopy(downloads),
            timeline=copy.deepcopy(timeline),
            blocked_requests=session.refusals.copy(),
            connections=session.guard.connections.copy(),
            transport_errors=copy.deepcopy(session.guard.transport_errors),
            dropped_events=dropped.copy(),
        )
        partial["errors"] = partial["errors"] + ["capture_in_progress"]
        partial["evidence_items"] = build_evidence_items(partial)
        partial["evidence_sha256"] = inventory_digest(partial)
        partial["content_sha256"] = content_digest(partial)
        callback(
            sanitize(partial),
            sanitize(
                {
                    **{k: v for k, v in result.items() if k != "observations"},
                    "cleanup_status": "open",
                }
            ),
        )

    def snapshot(action):
        _navigation_refusal(session)
        o = {
            "id": f"obs-{len(result['observations']) + 1:03d}",
            "captured_at": utcnow(),
            "action": action,
            "profile": profile,
            "subject_sha256": subject,
            "session_id": session.session_id,
            "final_url": redact_url(session.url),
            "http_status": status,
            "status": "partial",
            "errors": errors.copy(),
            "frames": [],
            "frame_capture_status": "requested"
            if request.get("_capture_frames", True)
            else "not_requested",
            "screenshot_base64": "",
            "screenshot_sha256": "",
        }
        checkpoint(o)
        try:
            data = sanitize(session.evaluate(PAGE_DATA))
            o.update(data)
        except Exception as exc:
            o["errors"].append(f"dom_capture:{type(exc).__name__}")
        checkpoint(o)
        if request.get("_screenshots", True) or action == "screenshot":
            from agentcore_tools.browser_runtime.runtime_limits import SCREENSHOT_SECONDS

            try:
                png = session.screenshot(
                    full_page=False, timeout=SCREENSHOT_SECONDS * 1000
                )
                if not png.startswith(b"\x89PNG") or len(png) > 5 * 1024 * 1024:
                    raise ValueError("Screenshot is not a bounded PNG")
                o["screenshot_base64"] = base64.b64encode(png).decode()
                o["screenshot_sha256"] = digest(png)
            except Exception as exc:
                o["errors"].append(f"screenshot_capture:{type(exc).__name__}")
        else:
            o["screenshot_status"] = "not_requested"
        checkpoint(o)
        for frame in (
            session.frames[1:6] if request.get("_capture_frames", True) else []
        ):
            try:
                o["frames"].append(
                    sanitize({"url": frame.url, **frame.evaluate(PAGE_DATA)})
                )
            except Exception as exc:
                o["errors"].append(f"frame_capture:{type(exc).__name__}")
        if (
            200 <= status < 400
            and o.get("visible_text", "").strip()
            and not o["errors"]
        ):
            o["status"] = "complete"
        # A challenge page is an observation of the challenge, not the destination.
        text = (o.get("page_title", "") + " " + o.get("visible_text", "")).lower()
        warning_terms = [
            s for s in ("deceptive site ahead", "suspected phishing") if s in text
        ]
        if warning_terms:
            o["interstitial"] = {
                "kind": "threat_warning",
                "source": "captured_page_text",
                "matched_terms": warning_terms,
                "provider_verified": False,
                "limitation": "Page-displayed warning; neither provider identity nor hidden page behavior is independently verified.",
            }
            o["errors"].append("threat_warning")
            o["status"] = "partial"
        elif any(
            s in text
            for s in (
                "verify you are human",
                "checking your browser",
                "captcha",
            )
        ):
            o["interstitial"] = {
                "kind": "human_verification",
                "source": "captured_page_text",
            }
            o["errors"].append("human_verification_challenge")
            o["status"] = "partial"
        _navigation_refusal(session)
        o.update(
            sanitize(
                {
                    "network_requests": copy.deepcopy(network),
                    "redirects": copy.deepcopy(redirects),
                    "downloads": copy.deepcopy(downloads),
                    "timeline": copy.deepcopy(timeline),
                    "blocked_requests": session.refusals.copy(),
                    "connections": session.guard.connections.copy(),
                    "transport_errors": copy.deepcopy(session.guard.transport_errors),
                    "dropped_events": dropped.copy(),
                }
            )
        )
        if o["blocked_requests"] or any(dropped.values()) or len(session.frames) > 6:
            o["status"] = "partial"
            o["errors"].append("observation_coverage_limited")
        if any(r.get("error") or r.get("status", 0) >= 400 for r in network):
            o["status"] = "partial"
            o["errors"].append("network_requests_failed")
        if session.guard.connections_dropped:
            o["status"] = "partial"
            o["errors"].append("connections_truncated")
        counts = o.get("counts", {})
        caps = {
            "forms": 30,
            "links": 50,
            "scripts": 20,
            "text_chars": 20000,
            "dom_chars": 100000,
        }
        page_counts = [counts] + [f.get("counts", {}) for f in o["frames"]]
        if any(
            c.get(k, 0) > cap for c in page_counts for k, cap in caps.items()
        ) or any(c.get("nested_capture_truncated") for c in [o, *o["frames"]]):
            o["status"] = "partial"
            o["errors"].append("page_capture_truncated")
        o["evidence_items"] = build_evidence_items(o)
        o["evidence_sha256"] = inventory_digest(o)
        o["content_sha256"] = content_digest(o)
        result["observations"].append(o)
        subjects_by_url[session.url] = subject

    try:
        result["session_id"] = session.session_id
        result["browser_version"] = session.browser_version
        result["playwright_version"] = version("playwright")
        session.on("response", on_response)
        session.on("framenavigated", on_navigation)
        session.on("download", on_download)
        session.on(
            "requestfailed",
            lambda req: append(
                "network",
                network,
                {
                    "url": req.url,
                    "method": req.method,
                    "resource_type": req.resource_type,
                    "status": 0,
                    "error": "request_failed",
                },
            ),
        )
        try:
            response = session.goto(
                request["url"],
                wait_until=request.get("_wait_until", "domcontentloaded"),
                timeout=request["timeout_ms"],
            )
            status = response.status if response else status
        except Exception as exc:
            _navigation_refusal(session)
            errors.append(f"navigation:{type(exc).__name__}")
        snapshot("initial")
        if delay and not errors:
            try:
                session.wait_for_timeout(delay * 1000)
            except Exception as exc:
                errors.append(f"wait:{type(exc).__name__}")
            snapshot("wait")
        result["cleanup_status"] = "open"
        command = yield result, session
        while command is not None:
            manual_navigation = (
                "back"
                if command["action"] == "back"
                else (
                    command["target_url"]
                    if command["action"] in {"follow", "root", "navigate"}
                    else None
                )
            )
            if command["action"] in {"follow", "root", "navigate"}:
                subject = digest(command["target_url"])
            before_url = session.url
            try:
                response = command["perform"](session)
                if response is not None and hasattr(response, "status"):
                    status = response.status
                if command["action"] == "back":
                    subject = subjects_by_url.get(session.url, digest(session.url))
                snapshot(command["action"])
                if isinstance(response, dict):
                    result["observations"][-1]["interaction"] = response
                if (
                    command["action"] == "follow"
                    and session.url == before_url
                    and command["target_url"].split("#", 1)[0]
                    != before_url.split("#", 1)[0]
                ):
                    result["observations"][-1]["status"] = "partial"
                    result["observations"][-1]["errors"].append(
                        "selected_navigation_not_observed"
                    )
            except DestinationRefused:
                raise
            except Exception as exc:
                _navigation_refusal(session)
                errors.append(f"action:{type(exc).__name__}")
                snapshot(command["action"])
            command = yield result, session
    finally:
        try:
            session.close()
            result["cleanup_status"] = "stopped"
        except Exception as exc:
            result["cleanup_status"] = "failed"
            result["cleanup_error"] = type(exc).__name__
            for o in result["observations"]:
                o["status"] = "partial"
                o["errors"].append("session_cleanup_failed")
        result["completed_at"] = utcnow()


def collect_case(request: dict, playwright) -> dict:
    """One fresh session, initial capture, optionally a delayed same-session capture."""
    recorder = recorded_browser(request, playwright)
    try:
        result, _ = next(recorder)
        return result
    finally:
        recorder.close()
