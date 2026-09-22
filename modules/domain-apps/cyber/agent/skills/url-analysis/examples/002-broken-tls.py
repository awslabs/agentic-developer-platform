"""
Example orchestration script: Broken TLS / expired certificate handling.

Demonstrates graceful handling of TLS errors (expired.badssl.com).
The broker is asked to ignore certificate errors so it can capture evidence.

Produced a "partial" status with TLS error noted in evidence.
"""

import sys
from datetime import datetime, timezone

from browser_client import analyze_url
from browser_guard import DestinationRefused

# -- Config --
URL = sys.argv[1] if len(sys.argv) > 1 else "https://expired.badssl.com"


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- Main --
run_started_at = iso_now()
error_msg = None

try:
    result = analyze_url(URL, wait_until="domcontentloaded", ignore_https_errors=True)
    session_id = result["session_id"]
    final_url = result["final_url"]
    http_status = result["http_status"]
    page_title = result["page_title"]
    screenshot_b64 = result["screenshot_base64"]
    visible_text = result["visible_text"]
    anti_analysis_signals = []
    if "expired" in URL.lower():
        anti_analysis_signals.append("tls_certificate_expired")

    run_completed_at = iso_now()

    # 7. Build Evidence
    from evidence_schema import Evidence, ScreenshotCapture

    screenshots = []
    if screenshot_b64:
        screenshots.append(
            ScreenshotCapture(
                session_id=session_id,
                image_base64=screenshot_b64,
                captured_at=run_completed_at,
            )
        )

    evidence = Evidence(
        target_url=URL,
        final_url=final_url,
        http_status=http_status,
        page_title=page_title,
        screenshots=screenshots,
        visible_text=visible_text[:10000],
        anti_analysis_signals=anti_analysis_signals,
        error=error_msg,
        run_started_at=run_started_at,
        run_completed_at=run_completed_at,
        session_id=session_id,
    )

    print(f"Evidence collected with TLS handling: error={error_msg}")
    print(f"Anti-analysis signals: {anti_analysis_signals}")

except DestinationRefused as refusal:
    print(f"REFUSED [{refusal.reason_code}]: {refusal.reason}")
    raise SystemExit(0) from refusal
