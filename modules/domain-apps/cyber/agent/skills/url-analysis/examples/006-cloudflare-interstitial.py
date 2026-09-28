"""
Example orchestration script: Cloudflare / vendor interstitial handling.

When a URL triggers a third-party block page (Cloudflare "Suspected
Phishing", Google Safe Browsing, Microsoft Edge SmartScreen), the
browser never reaches the real destination. The interstitial itself
is strong signal — Cloudflare already flagged this domain — but we
must NOT try to bypass it.

This example shows the expected pattern:
  1. Detect the interstitial by page title / visible text markers
  2. Extract the Ray ID / incident ID for attribution
  3. Record it as an anti-analysis signal + external-signal tag
  4. Return status="partial" instead of status="ok"
  5. Never click "verify you are human" / solve captcha

Encountered live on #497 URL 3 (`hotfixs.qen7varol.surf`).
"""

import base64
import re
import sys
from datetime import datetime, timezone

from bedrock_agentcore.tools.browser_client import BrowserClient
from playwright.sync_api import sync_playwright

# -- Config --
URL = sys.argv[1] if len(sys.argv) > 1 else "https://suspected-phishing.example/"
REGION = "us-east-1"

# Signature-based interstitial detection (title + visible-text fragments)
INTERSTITIAL_SIGNATURES = [
    {
        "vendor": "cloudflare",
        "title_markers": ["suspected phishing", "attention required"],
        "text_markers": ["cloudflare ray id", "cf-ray", "verify you are human"],
        "ray_id_pattern": r"Ray ID[:\s]+([a-f0-9]{8,})",
    },
    {
        "vendor": "google-safe-browsing",
        "title_markers": ["deceptive site ahead", "dangerous site"],
        "text_markers": ["google safe browsing", "attackers on the site you are trying to visit"],
        "ray_id_pattern": None,
    },
    {
        "vendor": "microsoft-smartscreen",
        "title_markers": ["this site has been reported as unsafe"],
        "text_markers": ["microsoft defender smartscreen", "reported as unsafe"],
        "ray_id_pattern": None,
    },
]


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def detect_interstitial(title: str, body_text: str) -> dict | None:
    """Return {vendor, incident_id} if a known interstitial is detected."""
    t_lower = (title or "").lower()
    b_lower = (body_text or "").lower()
    for sig in INTERSTITIAL_SIGNATURES:
        title_hit = any(m in t_lower for m in sig["title_markers"])
        text_hit = any(m in b_lower for m in sig["text_markers"])
        if title_hit or text_hit:
            incident_id = ""
            if sig["ray_id_pattern"]:
                m = re.search(sig["ray_id_pattern"], body_text or "", re.IGNORECASE)
                if m:
                    incident_id = m.group(1)
            return {"vendor": sig["vendor"], "incident_id": incident_id}
    return None


# -- Main --
run_started_at = iso_now()
bc = BrowserClient(region=REGION)
session_id = None
run_status = "ok"

try:
    session_id = bc.start()
    ws_url, headers = bc.generate_ws_headers()

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_url, headers=headers)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        try:
            response = page.goto(URL, wait_until="domcontentloaded", timeout=30000)
            final_url = page.url
            http_status = response.status if response else 0
            page_title = page.title()
        except Exception as e:
            final_url = URL
            http_status = 0
            page_title = ""
            print(f"Navigation error (may still have interstitial): {e}")

        screenshot_b64 = base64.b64encode(page.screenshot(full_page=True)).decode()
        visible_text = page.inner_text("body") if page.url else ""

        page.close()
        browser.close()

    run_completed_at = iso_now()

    # -- Interstitial detection --
    interstitial = detect_interstitial(page_title, visible_text)

    anti_analysis_signals = []
    recommended_tags = []

    if interstitial:
        vendor = interstitial["vendor"]
        incident = interstitial["incident_id"]
        run_status = "partial"  # real destination was blocked
        anti_analysis_signals.append(f"external_block:{vendor}")
        if incident:
            anti_analysis_signals.append(f"incident_id:{incident}")
        # Pin the upstream vendor verdict as a high-weight signal —
        # Cloudflare/Google/Microsoft flagging is strong third-party
        # evidence independent of our enrichment sources.
        recommended_tags.append(f"upstream_verdict_malicious:{vendor}")

        # NB: we intentionally do NOT click "Verify you are human" or
        # solve the captcha. Bypassing the interstitial would:
        # (a) likely violate AUP of the hosting provider,
        # (b) risk deanonymizing our analysis infra,
        # (c) destroy the signal we just captured.
        print(f"Interstitial detected: vendor={vendor}, incident={incident or '?'}")

    # -- Build Evidence --
    from evidence_schema import Evidence, ScreenshotCapture

    evidence = Evidence(
        target_url=URL,
        final_url=final_url,
        http_status=http_status,
        page_title=page_title,
        screenshots=[
            ScreenshotCapture(
                session_id=session_id,
                image_base64=screenshot_b64,
                captured_at=run_completed_at,
            )
        ],
        visible_text=visible_text[:10000],
        anti_analysis_signals=anti_analysis_signals + recommended_tags,
        run_started_at=run_started_at,
        run_completed_at=run_completed_at,
        session_id=session_id,
        error=None if run_status == "ok" else f"status={run_status}: interstitial blocked content",
    )

    print(
        f"Evidence: status={run_status}, interstitial="
        f"{interstitial['vendor'] if interstitial else 'none'}, "
        f"signals={anti_analysis_signals}"
    )

finally:
    try:
        bc.stop()
        print(f"Session stopped: {session_id}")
    except Exception:
        print(f"Session cleanup failed (will auto-terminate): {session_id}")
