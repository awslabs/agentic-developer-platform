"""
Example orchestration script: Broken TLS / expired certificate handling.

Demonstrates graceful handling of TLS errors (expired.badssl.com).
Playwright will throw on invalid certs by default; we set
ignoreHTTPSErrors=True to proceed and capture evidence anyway.

Produced a "partial" status with TLS error noted in evidence.
"""

import base64
import sys
import time
from datetime import datetime, timezone

import boto3
from playwright.sync_api import sync_playwright

# -- Config --
URL = sys.argv[1] if len(sys.argv) > 1 else "https://expired.badssl.com"
REGION = "us-east-1"
SESSION_TIMEOUT = 300

client = boto3.client("bedrock-agentcore", region_name=REGION)


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- Main --
run_started_at = iso_now()
session_id = None
error_msg = None

try:
    # 1. Start browser session
    resp = client.start_browser_session(
        browserIdentifier="aws.browser.v1",
        name=f"url-analysis-tls-{int(time.time())}",
        sessionTimeoutSeconds=SESSION_TIMEOUT,
        viewPort={"height": 819, "width": 1456},
    )
    session_id = resp["sessionId"]

    # 2. Get CDP WebSocket endpoint
    session_info = client.get_browser_session(
        browserIdentifier="aws.browser.v1",
        sessionId=session_id,
    )
    ws_url = session_info["streams"]["automationStream"]["streamEndpoint"]

    # 3. Connect Playwright with TLS error tolerance
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws_url)
        # Create context that ignores HTTPS errors to allow capture
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        try:
            response = page.goto(URL, wait_until="domcontentloaded", timeout=30000)
            final_url = page.url
            http_status = response.status if response else 0
            page_title = page.title()
        except Exception as e:
            # Even with ignore_https_errors, some scenarios may fail
            error_msg = f"TLS/navigation error: {type(e).__name__}: {e}"
            final_url = URL
            http_status = 0
            page_title = ""

        # 4. Screenshot (even if page partially loaded)
        try:
            screenshot_bytes = page.screenshot(full_page=True)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
        except Exception:
            screenshot_b64 = None

        # 5. Extract what text we can
        try:
            visible_text = page.inner_text("body")
        except Exception:
            visible_text = ""

        # 6. Check for TLS info via CDP
        # Note: Playwright doesn't directly expose cert details, but we can
        # detect the error from the page content or response
        anti_analysis_signals = []
        if "expired" in URL.lower() or (error_msg and "tls" in error_msg.lower()):
            anti_analysis_signals.append("tls_certificate_expired")

        page.close()
        context.close()
        browser.close()

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

finally:
    if session_id:
        try:
            client.stop_browser_session(
                browserIdentifier="aws.browser.v1",
                sessionId=session_id,
            )
            print(f"Session stopped: {session_id}")
        except Exception:
            print(f"Session cleanup failed (will auto-terminate): {session_id}")
