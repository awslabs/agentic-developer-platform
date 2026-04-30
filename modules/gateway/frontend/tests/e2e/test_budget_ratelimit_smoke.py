"""
Gateway admin UI smoke test — Budget + Rate Limit sections.

Issue #282: Playwright-driven exercise of deployed admin UI.
Authenticates via Cognito admin-initiate-auth, injects tokens via init_script,
then navigates Budget and Rate Limit pages to capture errors.

Usage:
    # Set required env vars
    export COGNITO_USER_POOL_ID="us-east-1_JEhv9xSGG"
    export COGNITO_CLIENT_ID="6cg7ba3hb4v41vbhm0cg8pl17j"
    export TEST_EMAIL="admin@example.com"
    export TEST_PASSWORD="..."
    export GATEWAY_URL="https://d1g6cal2ts4iis.cloudfront.net"

    python3 tests/e2e/test_budget_ratelimit_smoke.py
"""

from playwright.sync_api import sync_playwright
import json
import os
import subprocess
import time
from pathlib import Path

# Configuration from environment
URL = os.environ.get("GATEWAY_URL", "https://d1g6cal2ts4iis.cloudfront.net")
POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "us-east-1_JEhv9xSGG")
CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "6cg7ba3hb4v41vbhm0cg8pl17j")
REGION = os.environ.get("AWS_REGION", "us-east-1")

OUT_DIR = Path(os.environ.get("EVIDENCE_DIR", "/tmp/gateway-ui-evidence"))
OUT_DIR.mkdir(exist_ok=True)

findings: list[dict] = []


def record(section: str, action: str, severity: str, evidence: dict) -> None:
    findings.append({
        "section": section,
        "action": action,
        "severity": severity,
        "evidence": evidence,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


def get_tokens() -> dict:
    """Get auth tokens via Cognito admin-initiate-auth."""
    email = os.environ["TEST_EMAIL"]
    password = os.environ["TEST_PASSWORD"]

    result = subprocess.run(
        [
            "aws", "cognito-idp", "admin-initiate-auth",
            "--user-pool-id", POOL_ID,
            "--client-id", CLIENT_ID,
            "--auth-flow", "ADMIN_USER_PASSWORD_AUTH",
            "--auth-parameters", f"USERNAME={email},PASSWORD={password}",
            "--region", REGION,
            "--query", "AuthenticationResult",
            "--output", "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def build_init_script(tokens: dict) -> str:
    """Build JS init script to inject auth tokens into sessionStorage."""
    expiry_time = int(time.time() * 1000) + (tokens["ExpiresIn"] * 1000)
    return f"""
    (() => {{
        window.sessionStorage.setItem('cognito_access_token', `{tokens["AccessToken"]}`);
        window.sessionStorage.setItem('cognito_id_token', `{tokens["IdToken"]}`);
        window.sessionStorage.setItem('cognito_refresh_token', `{tokens["RefreshToken"]}`);
        window.sessionStorage.setItem('cognito_token_expiry', '{expiry_time}');
    }})();
    """


def dismiss_toasts(page) -> str | None:
    """Dismiss toast notifications and return their text."""
    container = page.query_selector('[role="region"][aria-label="Notifications"]')
    if container:
        text = container.inner_text().strip()
        # Try to dismiss
        buttons = container.query_selector_all("button")
        for btn in buttons:
            try:
                btn.click(force=True, timeout=500)
            except Exception:
                pass
        if text:
            return text
    return None


def run_smoke_test() -> None:
    print("Obtaining auth tokens...")
    tokens = get_tokens()
    init_script = build_init_script(tokens)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
        )
        context.add_init_script(init_script)
        page = context.new_page()

        # Event collectors
        console_errors: list[dict] = []
        bad_responses: list[dict] = []
        failed_requests: list[dict] = []

        page.on("console", lambda msg: console_errors.append(
            {"type": msg.type, "text": msg.text}
        ) if msg.type in ("error", "warning") else None)

        page.on("requestfailed", lambda r: failed_requests.append({
            "url": r.url, "method": r.method, "failure": r.failure
        }))

        def on_response(r):
            if r.status >= 400:
                bad_responses.append({
                    "url": r.url, "status": r.status, "method": r.request.method
                })
        page.on("response", on_response)

        # Verify auth
        page.goto(f"{URL}/budgets", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        if "amazoncognito.com" in page.url or "/login" in page.url:
            record("auth", "login", "blocker", {"msg": "Auth failed", "url": page.url})
            browser.close()
            return

        print("Auth OK.")

        # ── BUDGET MANAGEMENT ──
        print("\n=== Budget Management ===")
        console_errors.clear()
        bad_responses.clear()
        failed_requests.clear()

        page.goto(f"{URL}/budgets", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        page.screenshot(path=str(OUT_DIR / "budget-landing.png"), full_page=True)

        toast = dismiss_toasts(page)
        if toast or console_errors or bad_responses or failed_requests:
            record("budget", "page_load", "high", {
                "toast_text": toast,
                "console_errors": console_errors[:10],
                "bad_responses": bad_responses[:10],
                "failed_requests": failed_requests[:10],
            })

        # Add Budget
        console_errors.clear()
        bad_responses.clear()
        btn = page.query_selector('button:has-text("Add Budget")')
        if btn:
            btn.click(force=True)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(OUT_DIR / "budget-create-modal.png"), full_page=True)
            toast = dismiss_toasts(page)
            if toast or console_errors or bad_responses:
                record("budget", "open_create_modal", "high", {
                    "toast_text": toast,
                    "console_errors": console_errors[:10],
                    "bad_responses": bad_responses[:10],
                })
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)

        # Edit Budget
        console_errors.clear()
        bad_responses.clear()
        edit_btns = page.query_selector_all('button:has-text("Edit")')
        if edit_btns:
            edit_btns[0].click(force=True)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(OUT_DIR / "budget-edit-modal.png"), full_page=True)
            toast = dismiss_toasts(page)
            if toast or console_errors or bad_responses:
                record("budget", "open_edit_modal", "high", {
                    "toast_text": toast,
                    "console_errors": console_errors[:10],
                    "bad_responses": bad_responses[:10],
                })
            page.keyboard.press("Escape")
        else:
            record("budget", "find_edit_button", "info", {
                "msg": "No Edit buttons - table empty or failed to load",
            })

        # ── RATE LIMIT MANAGEMENT ──
        print("\n=== Rate Limit Management ===")
        console_errors.clear()
        bad_responses.clear()
        failed_requests.clear()

        page.goto(f"{URL}/ratelimits", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        page.screenshot(path=str(OUT_DIR / "ratelimit-landing.png"), full_page=True)

        toast = dismiss_toasts(page)
        if toast or console_errors or bad_responses or failed_requests:
            record("ratelimit", "page_load", "high", {
                "toast_text": toast,
                "console_errors": console_errors[:10],
                "bad_responses": bad_responses[:10],
                "failed_requests": failed_requests[:10],
            })

        # Add Rate Limit
        console_errors.clear()
        bad_responses.clear()
        btn = page.query_selector('button:has-text("Add Rate Limit")')
        if btn:
            btn.click(force=True)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(OUT_DIR / "ratelimit-create-modal.png"), full_page=True)
            toast = dismiss_toasts(page)
            if toast or console_errors or bad_responses:
                record("ratelimit", "open_create_modal", "high", {
                    "toast_text": toast,
                    "console_errors": console_errors[:10],
                    "bad_responses": bad_responses[:10],
                })
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)

        # Edit Rate Limit
        console_errors.clear()
        bad_responses.clear()
        edit_btns = page.query_selector_all('button:has-text("Edit")')
        if edit_btns:
            edit_btns[0].click(force=True)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(OUT_DIR / "ratelimit-edit-modal.png"), full_page=True)
            toast = dismiss_toasts(page)
            if toast or console_errors or bad_responses:
                record("ratelimit", "open_edit_modal", "high", {
                    "toast_text": toast,
                    "console_errors": console_errors[:10],
                    "bad_responses": bad_responses[:10],
                })
            page.keyboard.press("Escape")
        else:
            record("ratelimit", "find_edit_button", "info", {
                "msg": "No Edit buttons - table empty or failed to load",
            })

        browser.close()

    # Write output
    findings_path = OUT_DIR / "findings.json"
    findings_path.write_text(json.dumps(findings, indent=2))
    print(f"\nTest complete. {len(findings)} findings → {findings_path}")


if __name__ == "__main__":
    run_smoke_test()
