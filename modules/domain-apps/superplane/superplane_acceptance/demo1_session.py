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
