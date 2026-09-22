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

import re
import sys
from datetime import datetime, timezone

from browser_client import analyze_url
from browser_guard import DestinationRefused

# -- Config --
URL = sys.argv[1] if len(sys.argv) > 1 else "https://suspected-phishing.example/"

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
        "text_markers": [
            "google safe browsing",
            "attackers on the site you are trying to visit",
        ],
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
run_status = "ok"

try:
    result = analyze_url(URL, wait_until="domcontentloaded")
    session_id = result["session_id"]
    final_url = result["final_url"]
    http_status = result["http_status"]
    page_title = result["page_title"]
    screenshot_b64 = result["screenshot_base64"]
    visible_text = result["visible_text"]

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
        error=None
        if run_status == "ok"
        else f"status={run_status}: interstitial blocked content",
    )

    print(
        f"Evidence: status={run_status}, interstitial="
        f"{interstitial['vendor'] if interstitial else 'none'}, "
        f"signals={anti_analysis_signals}"
    )

except DestinationRefused as refusal:
    print(f"REFUSED [{refusal.reason_code}]: {refusal.reason}")
    raise SystemExit(0) from refusal
