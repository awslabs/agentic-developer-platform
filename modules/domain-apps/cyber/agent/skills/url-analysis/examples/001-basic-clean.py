"""
Example orchestration script: Basic clean URL analysis (example.com).

This script demonstrates the standard flow for analyzing a benign URL
using Playwright via CDP WebSocket. Produced a "clean" verdict.

Run context: executed by the agent inside the analysis pod.
"""

import base64
import sys
from datetime import datetime, timezone

from bedrock_agentcore.tools.browser_client import BrowserClient
from playwright.sync_api import sync_playwright

# -- Config --
URL = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
REGION = "us-east-1"


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- Main --
run_started_at = iso_now()
bc = BrowserClient(region=REGION)
session_id = None

try:
    # 1. Start browser session (SDK wraps start_browser_session)
    session_id = bc.start()
    print(f"Session started: {session_id}")

    # 2. Get CDP WebSocket URL + SigV4-signed auth headers.
    # The WebSocket upstream requires bedrock-agentcore:ConnectBrowserAutomationStream
    # on the calling role. Without it, connect_over_cdp returns 403.
    ws_url, headers = bc.generate_ws_headers()

    # 3. Connect Playwright and navigate
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_url, headers=headers)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        response = page.goto(URL, wait_until="networkidle", timeout=30000)
        final_url = page.url
        http_status = response.status if response else 0
        page_title = page.title()

        # 4. Screenshot
        screenshot_bytes = page.screenshot(full_page=True)
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode()

        # Claude-safe copy for the agent's own visual reasoning. Full-page
        # screenshots at default viewport often exceed Bedrock's image cap
        # and produce "API Error: 400 Could not process image".
        from evidence_store import shrink_for_claude

        claude_safe = shrink_for_claude(screenshot_bytes)
        if claude_safe:
            with open(f"/tmp/screenshot_{session_id}.png", "wb") as fh:
                fh.write(claude_safe)

        # 5. Extract visible text
        visible_text = page.inner_text("body")

        # 6. Detect forms
        forms_raw = page.evaluate("""
            Array.from(document.querySelectorAll('form')).map(f => ({
                action: f.action,
                method: f.method,
                fields: Array.from(f.querySelectorAll('input')).map(i => ({
                    name: i.name, type: i.type, hidden: i.type === 'hidden'
                }))
            }))
        """)

        page.close()
        browser.close()

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
    print(f"Verdict input ready: {len(forms_raw)} forms, {len(visible_text)} chars text")

finally:
    # 8. Always stop the session (SDK wraps stop_browser_session, idempotent)
    try:
        bc.stop()
        print(f"Session stopped: {session_id}")
    except Exception as e:
        print(f"Session cleanup failed (will auto-terminate): {session_id} — {e}")
