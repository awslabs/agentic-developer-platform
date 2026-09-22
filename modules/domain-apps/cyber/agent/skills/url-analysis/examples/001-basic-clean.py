"""
Example orchestration script: Basic clean URL analysis (example.com).

This script demonstrates the standard flow for analyzing a benign URL
through the trusted browser broker. Produced a "clean" verdict.

Run context: executed by the agent inside the analysis pod.
"""

import base64
import sys
from datetime import datetime, timezone

from browser_client import analyze_url
from browser_guard import DestinationRefused

# -- Config --
URL = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- Main --
run_started_at = iso_now()
try:
    result = analyze_url(URL)
    session_id = result["session_id"]
    final_url = result["final_url"]
    http_status = result["http_status"]
    page_title = result["page_title"]
    screenshot_b64 = result["screenshot_base64"]
    visible_text = result["visible_text"]
    forms_raw = result["forms"]

    from evidence_store import shrink_for_claude

    claude_safe = shrink_for_claude(base64.b64decode(screenshot_b64))
    if claude_safe:
        with open(f"/tmp/screenshot_{session_id}.png", "wb") as fh:
            fh.write(claude_safe)

    run_completed_at = iso_now()

    # 7. Build Evidence
    from evidence_schema import DetectedForm, Evidence, FormField, ScreenshotCapture

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
        forms=[
            DetectedForm(
                action=f.get("action", ""),
                method=f.get("method", "GET"),
                fields=[
                    FormField(
                        name=fd.get("name", ""),
                        field_type=fd.get("type", "text"),
                        is_hidden=fd.get("hidden", False),
                    )
                    for fd in f.get("fields", [])
                ],
            )
            for f in forms_raw
        ],
        run_started_at=run_started_at,
        run_completed_at=run_completed_at,
        session_id=session_id,
    )

    print(f"Evidence collected: final_url={final_url}, status={http_status}")
    print(
        f"Verdict input ready: {len(forms_raw)} forms, {len(visible_text)} chars text"
    )

except DestinationRefused as refusal:
    print(f"REFUSED [{refusal.reason_code}]: {refusal.reason}")
    raise SystemExit(0) from refusal
