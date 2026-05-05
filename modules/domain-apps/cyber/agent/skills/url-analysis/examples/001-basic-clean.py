"""
Example orchestration script: Basic clean URL analysis (example.com).

This script demonstrates the standard flow for analyzing a benign URL
using Playwright via CDP WebSocket. Produced a "clean" verdict.

Run context: executed by the agent inside the analysis pod.
"""

import base64
import sys
import time
from datetime import datetime, timezone

import boto3
from playwright.sync_api import sync_playwright

# -- Config --
URL = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
REGION = "us-east-1"
SESSION_TIMEOUT = 300

# -- Helpers --
client = boto3.client("bedrock-agentcore", region_name=REGION)


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- Main --
run_started_at = iso_now()
session_id = None

try:
    # 1. Start browser session
    resp = client.start_browser_session(
        browserIdentifier="aws.browser.v1",
        name=f"url-analysis-{int(time.time())}",
        sessionTimeoutSeconds=SESSION_TIMEOUT,
        viewPort={"height": 819, "width": 1456},
    )
    session_id = resp["sessionId"]
    print(f"Session started: {session_id}")

    # 2. Get CDP WebSocket endpoint
    session_info = client.get_browser_session(
        browserIdentifier="aws.browser.v1",
        sessionId=session_id,
    )
    ws_url = session_info["streams"]["automationStream"]["streamEndpoint"]

    # 3. Connect Playwright and navigate
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        response = page.goto(URL, wait_until="networkidle", timeout=30000)
        final_url = page.url
        http_status = response.status if response else 0
        page_title = page.title()

        # 4. Screenshot
        screenshot_bytes = page.screenshot(full_page=True)
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode()

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
    # 8. Always stop the session
    if session_id:
        try:
            client.stop_browser_session(
                browserIdentifier="aws.browser.v1",
                sessionId=session_id,
            )
            print(f"Session stopped: {session_id}")
        except client.exceptions.ResourceNotFoundException:
            print(f"Session already terminated: {session_id}")
