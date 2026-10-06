"""Import one private requester session without persisting tokens in local storage."""

from copy import deepcopy
from urllib.parse import urlsplit

from .demo1_evidence import EvidenceError

TOKEN_KEYS = frozenset(
    {
        "cognito_access_token",
        "cognito_id_token",
        "cognito_refresh_token",
        "cognito_token_expiry",
    }
)


def _entries(value):
    if not isinstance(value, list) or len(value) > 200:
        raise EvidenceError("live: browser storage entries unavailable")
    result = {}
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "value"}
            or not isinstance(item["name"], str)
            or not 0 < len(item["name"]) <= 256
            or item["name"] in result
            or not isinstance(item["value"], str)
            or len(item["value"]) > 65536
        ):
            raise EvidenceError("live: malformed or duplicate browser storage entry")
        result[item["name"]] = item["value"]
    return result


def browser_state_parts(value, origin):
    if not isinstance(value, dict) or set(value) != {"cookies", "origins"}:
        raise EvidenceError("live: private browser state unavailable")
    origins, cookies = value["origins"], value["cookies"]
    if (
        not isinstance(origins, list)
        or len(origins) != 1
        or not isinstance(origins[0], dict)
        or origins[0].get("origin") != origin
        or set(origins[0]) - {"origin", "localStorage", "sessionStorage"}
    ):
        raise EvidenceError("live: browser state belongs to another origin")
    local = _entries(origins[0].get("localStorage", []))
    session = _entries(origins[0].get("sessionStorage", []))
    legacy_tokens = {key: value for key, value in local.items() if key in TOKEN_KEYS}
    if legacy_tokens.keys() & session.keys():
        raise EvidenceError("live: ambiguous requester session tokens")
    session.update(legacy_tokens)
    if not session.get("cognito_access_token"):
        raise EvidenceError("live: authenticated requester browser state required")
    hostname = urlsplit(origin).hostname
    if not isinstance(cookies, list) or any(
        not isinstance(cookie, dict) or cookie.get("domain") != hostname
        for cookie in cookies
    ):
        raise EvidenceError("live: cross-origin browser cookies refused")
    state = {
        "cookies": deepcopy(cookies),
        "origins": [
            {
                "origin": origin,
                "localStorage": [
                    {"name": key, "value": value}
                    for key, value in local.items()
                    if key not in TOKEN_KEYS
                ],
            }
        ],
    }
    return state, session


def restore_browser_session(page, origin, session, *, timeout):
    if page.url != "about:blank":
        raise EvidenceError("journey: requester session requires a fresh browser page")
    bootstrap = origin + "/.well-known/adp-demo1-session"

    def serve(route):
        route.fulfill(status=200, content_type="text/html", body="<!doctype html>")

    page.route(bootstrap, serve)
    try:
        page.goto(bootstrap, wait_until="domcontentloaded", timeout=timeout)
        if page.url != bootstrap:
            raise EvidenceError("journey: requester session origin changed")
        page.evaluate(
            """entries => {
              for (const [name, value] of Object.entries(entries)) {
                sessionStorage.setItem(name, value);
              }
            }""",
            session,
        )
    finally:
        page.unroute(bootstrap, serve)


def observe_in_browser(selected, envelope, session, callback, *, max_runtime_seconds):
    """Restore the requester browser; callback retains explicit effect gates."""
    import time
    from datetime import UTC, datetime

    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError:
        raise EvidenceError(
            "browser observation: maintained browser runtime unavailable"
        ) from None
    from .demo1_browser import PREFIX, PlaywrightBrowserTransport, _response

    expires = time.monotonic() + min(max_runtime_seconds, envelope.max_runtime_seconds)

    def remaining_ms():
        now = datetime.now(UTC)
        remaining = min(
            expires - time.monotonic(), (selected.deadline - now).total_seconds()
        )
        if not selected.authorized_at <= now < selected.deadline or remaining <= 0:
            raise EvidenceError("browser observation: authorization window exhausted")
        return max(1, int(remaining * 1000))

    state, tokens = browser_state_parts(session, envelope.origin)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, timeout=remaining_ms())
        try:
            context = browser.new_context(storage_state=state, service_workers="block")
            page = context.new_page()
            page.set_default_timeout(min(30_000, remaining_ms()))
            restore_browser_session(
                page, envelope.origin, tokens, timeout=remaining_ms()
            )
            page.goto(
                envelope.origin + "/superplane",
                wait_until="domcontentloaded",
                timeout=remaining_ms(),
            )
            transport = PlaywrightBrowserTransport(
                page,
                envelope.origin,
                release_id=envelope.runtime_target.release_id,
                remaining_ms=remaining_ms,
                selected=selected,
            )
            _response(transport, "GET", PREFIX + "/capabilities")
            identity = _response(transport, "GET", "/api/auth/me")
            if (
                identity.get("user_id") != selected.requester_id
                or identity.get("org_id") != selected.org_id
            ):
                raise EvidenceError(
                    "browser observation: requester or organization differs"
                )
            return callback(transport)
        except (PlaywrightError, OSError, RuntimeError, ValueError) as error:
            if isinstance(error, EvidenceError):
                raise
            raise EvidenceError(
                "browser observation unavailable; retain original checkpoint for recovery"
            ) from None
        finally:
            browser.close()
