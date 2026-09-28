"""
Example orchestration script: Redirect chain tracking (link shorteners,
cloaking, exploit-kit hops).

Hooks `page.on("response", ...)` + `page.on("framenavigated", ...)` to
capture every hop from target URL to final landing page — HTTP 3xx,
meta-refresh, and JS-driven `location.href=` redirects. Detects TLD
and hostname drift across the chain which is a strong cloaking signal.

Expected verdict: suspicious when the chain crosses 2+ different
registered domains, or lands on a different TLD than it started.
"""

import base64
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

from bedrock_agentcore.tools.browser_client import BrowserClient
from playwright.sync_api import sync_playwright

# -- Config --
URL = sys.argv[1] if len(sys.argv) > 1 else "https://bit.ly/example"
REGION = "us-east-1"


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def host_of(u: str) -> str:
    try:
        return urlparse(u).hostname or ""
    except Exception:
        return ""


def reg_domain(host: str) -> str:
    """Coarse registered-domain extract: last 2 labels (no PSL dependency)."""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


# -- Main --
run_started_at = iso_now()
bc = BrowserClient(region=REGION)
session_id = None
hops: list[dict] = []  # {from_url, to_url, status_code, method}

try:
    session_id = bc.start()
    ws_url, headers = bc.generate_ws_headers()

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_url, headers=headers)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        # Hook HTTP 3xx redirects
        def on_response(resp):
            if 300 <= resp.status < 400:
                loc = resp.headers.get("location", "")
                hops.append(
                    {
                        "from_url": resp.url,
                        "to_url": loc or "(no Location header)",
                        "status_code": resp.status,
                        "method": "http",
                    }
                )

        # Hook frame-level navigation events (catches meta-refresh + JS)
        last_url = [URL]

        def on_framenavigated(frame):
            if frame == page.main_frame and frame.url != last_url[0]:
                # If we already recorded this transition as HTTP, skip
                recent_http = any(
                    h["to_url"] == frame.url and h["method"] == "http"
                    for h in hops[-3:]
                )
                if not recent_http:
                    hops.append(
                        {
                            "from_url": last_url[0],
                            "to_url": frame.url,
                            "status_code": 0,
                            "method": "js",  # meta-refresh shows up here too
                        }
                    )
                last_url[0] = frame.url

        page.on("response", on_response)
        page.on("framenavigated", on_framenavigated)

        response = page.goto(URL, wait_until="networkidle", timeout=30000)
        final_url = page.url
        http_status = response.status if response else 0
        page_title = page.title()

        screenshot_b64 = base64.b64encode(page.screenshot(full_page=True)).decode()
        visible_text = page.inner_text("body")

        page.close()
        browser.close()

    run_completed_at = iso_now()

    # -- Cloaking heuristics --
    anti_analysis_signals = []

    chain_hosts = [host_of(URL)] + [host_of(h["to_url"]) for h in hops]
    chain_hosts = [h for h in chain_hosts if h]
    distinct_regs = {reg_domain(h) for h in chain_hosts}

    if len(distinct_regs) >= 3:
        anti_analysis_signals.append(
            f"redirect_fanout:{len(distinct_regs)}_registered_domains"
        )

    start_tld = urlparse(URL).hostname or ""
    end_tld = urlparse(final_url).hostname or ""
    if (
        start_tld
        and end_tld
        and start_tld.split(".")[-1] != end_tld.split(".")[-1]
    ):
        anti_analysis_signals.append(
            f"tld_drift:{start_tld.split('.')[-1]}->{end_tld.split('.')[-1]}"
        )

    # JS-only redirects (not HTTP 3xx) are a classic cloaking tell
    if any(h["method"] == "js" for h in hops):
        anti_analysis_signals.append("js_redirect_in_chain")

    # -- Build Evidence --
    from evidence_schema import Evidence, RedirectHop, ScreenshotCapture

    evidence = Evidence(
        target_url=URL,
        final_url=final_url,
        http_status=http_status,
        page_title=page_title,
        redirects=[
            RedirectHop(
                from_url=h["from_url"],
                to_url=h["to_url"],
                status_code=h["status_code"],
                method=h["method"],
            )
            for h in hops
        ],
        screenshots=[
            ScreenshotCapture(
                session_id=session_id,
                image_base64=screenshot_b64,
                captured_at=run_completed_at,
            )
        ],
        visible_text=visible_text[:10000],
        anti_analysis_signals=anti_analysis_signals,
        run_started_at=run_started_at,
        run_completed_at=run_completed_at,
        session_id=session_id,
    )

    print(f"Evidence: {len(hops)} hops, final={final_url}")
    print(f"  Chain: {' -> '.join(chain_hosts)}")
    print(f"  Signals: {anti_analysis_signals}")

finally:
    try:
        bc.stop()
        print(f"Session stopped: {session_id}")
    except Exception:
        print(f"Session cleanup failed (will auto-terminate): {session_id}")
