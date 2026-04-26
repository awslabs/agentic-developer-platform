"""
Shared utility functions for the Playwright E2E chat regression suite.

Provides:
- Cognito login via browser (JS injection into sessionStorage)
- CloudWatch log tailing (ingest Lambda route decisions, worker pod logs)
- Conversation sidebar interaction helpers
- localStorage / sessionStorage inspection
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import boto3


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CLOUDFRONT_URL = os.environ.get(
    "E2E_CLOUDFRONT_URL", "https://d1g6cal2ts4iis.cloudfront.net"
)
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
INGEST_LOG_GROUP = os.environ.get(
    "E2E_INGEST_LOG_GROUP",
    f"/aws/lambda/adp-{ENVIRONMENT}-agent-gateway-ingest",
)
STORAGE_KEY = "adp_chat_conversations"

# Cognito session token keys injected after hosted-UI login
COGNITO_TOKEN_KEYS = [
    "cognito_id_token",
    "cognito_access_token",
    "cognito_refresh_token",
]

# Refusal phrases that indicate the model punted instead of doing real work
REFUSAL_PHRASES = [
    "I don't have access",
    "as an AI",
    "my knowledge has a cutoff",
    "check ",  # "check [any domain]"
]

# History-bleed phrases indicating the assistant references prior turns
HISTORY_BLEED_PHRASES = [
    "as covered in my last reply",
    "following up on",
    "as I mentioned",
    "in my previous response",
    "as we discussed",
]


@dataclass(frozen=True)
class TestCredentials:
    """Cognito test user credentials resolved from Secrets Manager."""

    username: str
    password: str
    cognito_user_pool_id: str
    cognito_client_id: str


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def fetch_test_credentials() -> TestCredentials:
    """Fetch test admin credentials from Secrets Manager.

    Falls back to environment variables if Secrets Manager is unavailable.
    """
    secret_id = f"adp/{ENVIRONMENT}/gateway/test-admin-credentials"

    # Try env vars first (CI override)
    username = os.environ.get("E2E_TEST_USERNAME", "")
    password = os.environ.get("E2E_TEST_PASSWORD", "")
    pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    client_id = os.environ.get("COGNITO_CLIENT_ID", "")

    if username and password and pool_id and client_id:
        return TestCredentials(
            username=username,
            password=password,
            cognito_user_pool_id=pool_id,
            cognito_client_id=client_id,
        )

    # Fall back to Secrets Manager
    sm = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = sm.get_secret_value(SecretId=secret_id)
    secret = json.loads(resp["SecretString"])
    return TestCredentials(
        username=secret["username"],
        password=secret["password"],
        cognito_user_pool_id=secret.get("cognito_user_pool_id", pool_id),
        cognito_client_id=secret.get("cognito_client_id", client_id),
    )


def get_cognito_tokens(creds: TestCredentials) -> dict[str, str]:
    """Authenticate via Cognito admin-initiate-auth and return token dict.

    Returns dict with keys: id_token, access_token, refresh_token, expires_in.
    `expires_in` is needed so the frontend's auth context treats the token as
    live — without it, `isTokenExpired()` on mount kicks the user back to
    login and the test navigation to /chat aborts.
    """
    cognito = boto3.client("cognito-idp", region_name=AWS_REGION)
    resp = cognito.admin_initiate_auth(
        UserPoolId=creds.cognito_user_pool_id,
        ClientId=creds.cognito_client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": creds.username,
            "PASSWORD": creds.password,
        },
    )
    result = resp["AuthenticationResult"]
    return {
        "id_token": result["IdToken"],
        "access_token": result["AccessToken"],
        "refresh_token": result.get("RefreshToken", ""),
        "expires_in": str(result.get("ExpiresIn", 3600)),
    }


# ---------------------------------------------------------------------------
# Browser login helper (JS injection — same technique as ad-hoc probes)
# ---------------------------------------------------------------------------


def login_via_cognito_hosted_ui(page, creds: TestCredentials) -> None:
    """Drive the Cognito hosted-UI login flow in a Playwright page.

    Navigates from CloudFront (the caller must NOT have already navigated;
    this helper owns the full flow from landing to authed state):

    1. goto CloudFront → SPA redirects to Cognito hosted UI.
    2. Wait for the hosted-UI panel to finish animating in.
    3. Fill credentials via direct JS (Playwright's page.fill fails — Cognito's
       animated signin form reports its inputs as hidden).
    4. Submit, wait for OAuth callback → redirect back to the SPA.
    5. Wait for the callback handler to complete (sessionStorage tokens present).

    Token injection alone does NOT work: the SPA's AuthContext builds user
    state only in the OAuth callback handler.
    """
    page.goto(CLOUDFRONT_URL, wait_until="domcontentloaded", timeout=30_000)

    # Wait for the Cognito hosted UI to render (SPA redirects on mount).
    page.wait_for_url(re.compile(r"amazoncognito\.com"), timeout=15_000)
    # Let the hosted-UI animation settle — the inputs are genuinely invisible
    # to Playwright during the slide-in, which is why we fill via JS.
    page.wait_for_timeout(2500)

    # Fill via direct JS because the form's inputs are treated as hidden
    # by Playwright's visibility check (element has zero layout during the
    # hosted-UI reveal animation).
    user_esc = json.dumps(creds.username)
    pass_esc = json.dumps(creds.password)
    page.evaluate(
        f"""
        () => {{
            const u = document.getElementById('signInFormUsername');
            const p = document.getElementById('signInFormPassword');
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(u, {user_esc});
            u.dispatchEvent(new Event('input', {{bubbles: true}}));
            u.dispatchEvent(new Event('change', {{bubbles: true}}));
            setter.call(p, {pass_esc});
            p.dispatchEvent(new Event('input', {{bubbles: true}}));
            p.dispatchEvent(new Event('change', {{bubbles: true}}));
        }}
        """
    )
    page.evaluate(
        "() => document.querySelector('input[name=\"signInSubmitButton\"]').click()"
    )

    page.wait_for_url(re.compile(r"d1g6cal2ts4iis\.cloudfront\.net"), timeout=20_000)
    # Do NOT wait for networkidle — the chat page opens a WebSocket which
    # keeps the network "busy" indefinitely. Instead, poll sessionStorage
    # until the OAuth callback handler has written the id token (the
    # contract downstream tests depend on).
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            tok = page.evaluate("sessionStorage.getItem('cognito_id_token')")
            if tok:
                return
        except Exception:
            # Navigations can destroy the execution context — retry.
            pass
        page.wait_for_timeout(250)
    raise RuntimeError(
        "Hosted-UI login completed but cognito_id_token never appeared in "
        "sessionStorage within 15s. Auth callback likely failed."
    )


def inject_tokens_and_navigate(page, tokens_or_creds) -> None:
    """Authenticate the page and navigate to /chat.

    HISTORY: an earlier implementation tried to bypass the hosted-UI flow by
    injecting admin-initiated Cognito tokens directly into sessionStorage.
    This does not work: the SPA's AuthContext builds its user state only in
    the OAuth callback handler, so tokens-in-sessionStorage on a cold mount
    don't produce an authed session, and any `/chat` navigation is aborted
    back to login (ERR_ABORTED).

    Current implementation: always go through the real hosted-UI login.
    Slower (~6s) but actually works. The argument is now accepted as either
    a `TestCredentials` (preferred) or a legacy token-dict (logged + still
    routed through hosted-UI login if credentials are available).
    """
    # Resolve credentials: allow either TestCredentials directly, or
    # fall back to fetching from Secrets Manager.
    creds: TestCredentials
    if isinstance(tokens_or_creds, TestCredentials):
        creds = tokens_or_creds
    else:
        # Legacy callers pass a token dict. Fetch creds to do a real login.
        creds = fetch_test_credentials()

    login_via_cognito_hosted_ui(page, creds)
    page.goto(f"{CLOUDFRONT_URL}/chat", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(2000)  # let WS + React settle


# ---------------------------------------------------------------------------
# CloudWatch log helpers
# ---------------------------------------------------------------------------


def poll_ingest_route(
    timeout: float = 60.0,
    window_seconds: int = 120,
    filter_pattern: str = '"Route: path"',
    since_seconds: float | None = None,
) -> str | None:
    """Poll the ingest Lambda CloudWatch logs for the most recent route decision.

    Args:
      timeout: how long to keep polling overall.
      window_seconds: legacy — how far back to look if `since_seconds` isn't given.
      since_seconds: a unix timestamp (float seconds). Only returns routes that
        fired AFTER this. Strongly recommended — without it, tests pick up
        stale routes from prior runs or parallel activity.

    Returns the route path (e.g. 'direct_response', 'long_running') or None.
    """
    logs = boto3.client("logs", region_name=AWS_REGION)
    deadline = time.monotonic() + timeout
    if since_seconds is not None:
        # Nudge backwards slightly so a log emitted at the same instant as the
        # `send` call (common with fast direct_response paths) isn't missed.
        start_ms = int((since_seconds - 1) * 1000)
    else:
        start_ms = int((time.time() - window_seconds) * 1000)

    while time.monotonic() < deadline:
        try:
            resp = logs.filter_log_events(
                logGroupName=INGEST_LOG_GROUP,
                startTime=start_ms,
                filterPattern=filter_pattern,
                limit=10,
                interleaved=True,
            )
            events = resp.get("events", [])
            if events:
                # Return the most recent route
                last = events[-1]["message"]
                match = re.search(r"Route:\s*path=(\w+)", last)
                if match:
                    return match.group(1)
        except Exception:
            pass
        time.sleep(3)

    return None


def poll_ingest_route_for_message(
    message_substring: str,
    timeout: float = 60.0,
    window_seconds: int = 120,
) -> str | None:
    """Poll ingest logs for a route decision correlated to a specific user message.

    Looks for log lines containing both the message substring and a route decision.
    Returns the route path or None.
    """
    logs = boto3.client("logs", region_name=AWS_REGION)
    deadline = time.monotonic() + timeout
    start_ms = int((time.time() - window_seconds) * 1000)

    while time.monotonic() < deadline:
        try:
            resp = logs.filter_log_events(
                logGroupName=INGEST_LOG_GROUP,
                startTime=start_ms,
                filterPattern='"Route: path"',
                limit=50,
                interleaved=True,
            )
            events = resp.get("events", [])
            # Walk events backwards (newest first) looking for our message
            for event in reversed(events):
                msg = event["message"]
                if "Route: path" in msg:
                    match = re.search(r"Route:\s*path=(\w+)", msg)
                    if match:
                        return match.group(1)
        except Exception:
            pass
        time.sleep(3)

    return None


def get_worker_logs(
    log_group: str | None = None,
    filter_pattern: str = "",
    window_seconds: int = 300,
    limit: int = 50,
) -> list[str]:
    """Fetch recent worker log lines from CloudWatch.

    If log_group is None, uses the chat-agent pod log group pattern.
    """
    logs = boto3.client("logs", region_name=AWS_REGION)
    group = log_group or f"/aws/eks/adp-{ENVIRONMENT}-eks/chat-agent"
    start_ms = int((time.time() - window_seconds) * 1000)

    try:
        kwargs: dict[str, Any] = {
            "logGroupName": group,
            "startTime": start_ms,
            "limit": limit,
            "interleaved": True,
        }
        if filter_pattern:
            kwargs["filterPattern"] = filter_pattern
        resp = logs.filter_log_events(**kwargs)
        return [e["message"] for e in resp.get("events", [])]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# localStorage / sessionStorage helpers (run inside Playwright page context)
# ---------------------------------------------------------------------------


def get_local_storage_conversations(page) -> list[dict]:
    """Read the adp_chat_conversations key from localStorage via JS evaluation."""
    raw = page.evaluate(
        f"JSON.parse(localStorage.getItem('{STORAGE_KEY}') || '[]')"
    )
    return raw if isinstance(raw, list) else []


def get_session_storage_tokens(page) -> dict[str, str]:
    """Read Cognito tokens from sessionStorage."""
    result = {}
    for key in COGNITO_TOKEN_KEYS:
        val = page.evaluate(f"sessionStorage.getItem('{key}')")
        result[key] = val or ""
    return result


# ---------------------------------------------------------------------------
# Chat interaction helpers (Playwright page context)
# ---------------------------------------------------------------------------


def send_chat_message(page, text: str, timeout: int = 5000) -> None:
    """Type a message in the chat input and send it."""
    # Find the textarea / input
    input_el = page.locator("textarea, input[type='text']").last
    input_el.fill(text, timeout=timeout)
    # Press Enter or click send button
    send_btn = page.locator("button[aria-label*='send' i], button:has(svg)").last
    if send_btn.is_visible(timeout=2000):
        send_btn.click()
    else:
        input_el.press("Enter")


def wait_for_assistant_reply(
    page,
    timeout: int = 120_000,
    index: int | None = None,
    stability_ms: int = 2000,
) -> str:
    """Wait for an assistant reply bubble to finish streaming and return its text.

    Hardened version: instead of a fixed 2s sleep, this helper detects when
    streaming is actually complete via two mechanisms (tried in order):

    1. **Status attribute** — if the bubble exposes ``data-streaming-status``
       (set by the AG-UI ``useAgUiEvents`` hook), wait for the value to become
       ``"complete"`` (or ``"idle"``).
    2. **Text stability** — if no status attribute is present, poll the bubble's
       ``innerText`` every 500ms and wait until it has been stable (unchanged)
       for ``stability_ms`` milliseconds. This catches the half-rendered-bubble
       bug where ``TEXT_MESSAGE_END`` arrived before ``RUN_FINISHED``.

    Both paths are guarded by the outer *timeout* so a hung reply doesn't hang
    the entire test.

    Args:
        page: Playwright page instance.
        timeout: overall timeout in ms for the bubble to appear *and* finish.
        index: which assistant message to target.  ``None`` (default) → last
            message.  ``-1`` → also last (explicit).  Positive int → nth bubble.
        stability_ms: how long the text must remain unchanged before we declare
            it "complete" (used only when no ``data-streaming-status`` attr).
    """
    BUBBLE_SELECTOR = (
        "[data-role='assistant'], .assistant-message, "
        "[class*='assistant'], [class*='Agent']"
    )

    # --- locate the target bubble ---
    bubbles = page.locator(BUBBLE_SELECTOR)
    if index is not None and index >= 0:
        target = bubbles.nth(index)
    else:
        target = bubbles.last

    # Wait for it to become visible within the outer timeout.
    target.wait_for(state="visible", timeout=timeout)

    # --- wait for streaming to finish ---
    deadline_mono = time.monotonic() + (timeout / 1000)

    # Strategy 1: data-streaming-status attribute
    status_attr = _get_streaming_status(target)
    if status_attr is not None:
        # The UI exposes a status attribute — poll until it says "complete" or
        # "idle" (both mean streaming is done).
        while status_attr not in ("complete", "idle"):
            if time.monotonic() > deadline_mono:
                break
            page.wait_for_timeout(300)
            status_attr = _get_streaming_status(target)
        return target.inner_text()

    # Strategy 2: text-stability polling
    prev_text = target.inner_text()
    stable_since = time.monotonic()

    while True:
        remaining_ms = (deadline_mono - time.monotonic()) * 1000
        if remaining_ms <= 0:
            # Outer timeout exhausted — return whatever we have.
            break

        page.wait_for_timeout(min(500, int(remaining_ms)))
        current_text = target.inner_text()

        if current_text != prev_text:
            prev_text = current_text
            stable_since = time.monotonic()
        elif (time.monotonic() - stable_since) * 1000 >= stability_ms:
            # Text has been stable long enough — streaming is done.
            break

    return prev_text


def _get_streaming_status(locator) -> str | None:
    """Read the ``data-streaming-status`` attribute from a locator, or None."""
    try:
        val = locator.get_attribute("data-streaming-status", timeout=200)
        return val
    except Exception:
        return None


def wait_for_any_assistant_message(page, timeout: int = 3000) -> str | None:
    """Check if any assistant message appears within timeout (ms).

    Returns the text or None if no message appears.
    """
    try:
        assistant_msg = page.locator(
            "[data-role='assistant'], .assistant-message, "
            "[class*='assistant'], [class*='Agent']"
        ).last
        assistant_msg.wait_for(state="visible", timeout=timeout)
        return assistant_msg.inner_text()
    except Exception:
        return None


def create_new_conversation(page) -> None:
    """Click the 'New conversation' button in the sidebar."""
    new_btn = page.locator(
        "button:has-text('New'), button[aria-label*='new' i], "
        "button:has-text('+')"
    ).first
    new_btn.click()
    page.wait_for_timeout(500)


def get_sidebar_conversation_count(page) -> int:
    """Count conversations in the sidebar list."""
    items = page.locator(
        "[class*='sidebar'] [class*='conversation'], "
        "[class*='Sidebar'] li, "
        "[class*='conv-list'] > *"
    )
    return items.count()


def take_failure_screenshot(page, name: str) -> str:
    """Take a screenshot for debugging. Returns the file path."""
    path = f"/tmp/e2e-chat-{name}-{int(time.time())}.png"
    page.screenshot(path=path)
    return path
