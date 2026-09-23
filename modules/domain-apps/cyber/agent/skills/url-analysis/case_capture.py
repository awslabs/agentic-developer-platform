"""Maintained research collector, executed only inside the trusted browser broker."""

from __future__ import annotations

import base64
import copy
import os
from importlib.metadata import version
from urllib.parse import urljoin

from browser_guard import DEFAULT_REGION, DestinationRefused, open_guarded_browser
from case_contract import (
    COLLECTOR_VERSION,
    SCHEMA_VERSION,
    content_digest,
    digest,
    redact_url,
    sanitize,
    utcnow,
)
from denylist import DenylistResult

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
    action: f.action, method: f.method, fields: Array.from(f.querySelectorAll('input,select,textarea'))
      .slice(0, 50).map(i => ({name: i.name, type: i.type, hidden: i.type === 'hidden'}))
  })),
  orphan_inputs: Array.from(document.querySelectorAll('input[type=password],input[type=email]'))
    .filter(i => !i.closest('form')).slice(0, 30).map(i => ({name: i.name, type: i.type})),
  links: Array.from(document.querySelectorAll('a[href]')).slice(0, 50)
    .map(a => ({url: a.href, text: a.innerText.slice(0, 150)})),
  scripts: Array.from(document.scripts).slice(0, 20)
    .map(s => ({src: s.src, inline: s.src ? '' : (s.textContent || '').slice(0, 1200)})),
  counts: {forms: document.forms.length, links: document.querySelectorAll('a[href]').length,
    scripts: document.scripts.length, text_chars: document.body?.innerText.length || 0,
    dom_chars: root.outerHTML.length},
  nested_capture_truncated: document.title.length > 500 ||
    Array.from(document.forms).some(f => f.querySelectorAll('input,select,textarea').length > 50) ||
    Array.from(document.scripts).some(s => !s.src && (s.textContent || '').length > 1200) ||
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


def collect_case(request: dict, playwright) -> dict:
    """One fresh session, initial capture, optionally a delayed same-session capture."""
    profile, delay = validate_options(request)
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
        "limitations": LIMITATIONS.copy(),
        "cleanup_status": "not_started",
    }
    network, redirects, downloads, timeline = [], [], [], []
    dropped = {"network": 0, "redirects": 0, "downloads": 0, "timeline": 0}
    status = 0
    previous_url = request["url"]
    errors = []

    def append(kind, items, value):
        if len(items) < MAX_EVENTS:
            items.append(sanitize({"at": utcnow(), **value}))
        else:
            dropped[kind] += 1

    session = open_guarded_browser(
        request["url"],
        playwright,
        region=result["region"],
        context_options={**PROFILES[profile], "accept_downloads": False},
        read_only=True,
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
        nonlocal previous_url
        if frame == session.main_frame:
            append("timeline", timeline, {"event": "navigation", "url": frame.url})
            if frame.url != previous_url:
                append(
                    "redirects",
                    redirects,
                    {
                        "from_url": previous_url,
                        "to_url": frame.url,
                        "kind": "navigation",
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
            "screenshot_base64": "",
            "screenshot_sha256": "",
        }
        try:
            data = sanitize(session.evaluate(PAGE_DATA))
            o.update(data)
            for frame in session.frames[1:6]:
                try:
                    o["frames"].append(
                        sanitize({"url": frame.url, **frame.evaluate(PAGE_DATA)})
                    )
                except Exception as exc:
                    o["errors"].append(f"frame_capture:{type(exc).__name__}")
            png = session.screenshot(full_page=False, timeout=10000)
            if not png.startswith(b"\x89PNG") or len(png) > 5 * 1024 * 1024:
                raise ValueError("Screenshot is not a bounded PNG")
            o["screenshot_base64"] = base64.b64encode(png).decode()
            o["screenshot_sha256"] = digest(png)
            if (
                200 <= status < 400
                and o.get("visible_text", "").strip()
                and not o["errors"]
            ):
                o["status"] = "complete"
        except Exception as exc:
            o["errors"].append(f"capture:{type(exc).__name__}")
        # A challenge page is an observation of the challenge, not the destination.
        text = (o.get("page_title", "") + " " + o.get("visible_text", "")).lower()
        if any(
            s in text
            for s in (
                "verify you are human",
                "checking your browser",
                "deceptive site ahead",
                "suspected phishing",
                "captcha",
            )
        ):
            o["errors"].append("challenge_or_interstitial")
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
        o["content_sha256"] = content_digest(o)
        result["observations"].append(o)

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
                wait_until="domcontentloaded",
                timeout=min(request["timeout_ms"], 30000),
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
    return result
