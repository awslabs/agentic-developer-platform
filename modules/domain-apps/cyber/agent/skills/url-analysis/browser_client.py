"""Direct AgentCore browser client; explicit broker mode supports legacy rollouts."""

from __future__ import annotations

import json
import ipaddress
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from browser_guard import DestinationRefused
from case_contract import MAX_RESPONSE_BYTES
from denylist import DenylistResult

DEFAULT_BROKER_URL = (
    "http://url-analysis-browser-broker.adp-agents.svc.cluster.local:8765"
)
DEFAULT_TIMEOUT_SECONDS = 360
MAX_ERROR_BYTES = 64 * 1024


class BrowserBrokerError(RuntimeError):
    """Raised when the trusted browser broker cannot complete an analysis."""

    def __init__(
        self,
        message,
        *,
        code="broker_unavailable",
        retry_after=None,
        cleanup=None,
        browser_start_unattempted=False,
    ):
        super().__init__(message)
        self.code, self.retry_after, self.cleanup = code, retry_after, cleanup
        self.browser_start_unattempted = browser_start_unattempted


def _decode_json(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BrowserBrokerError(
            "browser broker returned an invalid response"
        ) from error
    if not isinstance(value, dict):
        raise BrowserBrokerError("browser broker returned an invalid response")
    return value


def analyze_url(
    url: str,
    *,
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 30_000,
    ignore_https_errors: bool = False,
    broker_url: str | None = None,
    request_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Analyze a URL in an ephemeral AgentCore session."""
    return _request(
        "analyze",
        {
            "url": url,
            "wait_until": wait_until,
            "timeout_ms": timeout_ms,
            "ignore_https_errors": ignore_https_errors,
        },
        broker_url,
        request_timeout_seconds,
    )


def capture_url(
    url: str,
    *,
    profile: str = "desktop",
    wait_seconds: int = 0,
    timeout_ms: int = 30000,
    broker_url: str | None = None,
    request_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Collect a versioned research bundle with direct AgentCore browsing."""
    return _request(
        "capture",
        {
            "url": url,
            "profile": profile,
            "wait_seconds": wait_seconds,
            "timeout_ms": timeout_ms,
        },
        broker_url,
        request_timeout_seconds,
    )


def investigation_request(operation, payload, *, broker_url=None):
    """One reasoning-selected operation; never replay a timed-out browser action."""
    if operation not in {"start", "step", "close"}:
        raise ValueError("Unsupported investigation operation")
    if (
        broker_url is None
        and os.environ.get("URL_ANALYSIS_BROWSER_MODE", "native") == "native"
    ):
        from local_browser import investigation_request as direct_request

        return direct_request(operation, payload)
    token = payload.get("session_token", "")
    if operation != "start" and "~" in token:
        owner, capability = token.split("~", 1)
        address = ipaddress.IPv4Address(owner)
        if (
            not address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_unspecified
        ):
            raise ValueError("Invalid broker session owner")
        # Only the fixed broker port; worker NetworkPolicy restricts it to broker pods.
        broker_url = f"http://{address}:8765"
        payload = {**payload, "session_token": capability}
    from runtime_limits import STARTUP_SECONDS, ACTION_SECONDS

    budget = STARTUP_SECONDS if operation == "start" else ACTION_SECONDS
    return _request("investigation/" + operation, payload, broker_url, budget + 20)


def _request(operation, payload, broker_url, request_timeout_seconds):
    if (
        broker_url is None
        and os.environ.get("URL_ANALYSIS_BROWSER_MODE", "native") == "native"
    ):
        from direct_capture import capture

        return capture(operation, payload)
    url = payload.get("url", "")
    endpoint = (
        broker_url
        or os.environ.get("URL_ANALYSIS_BROWSER_BROKER")
        or DEFAULT_BROKER_URL
    ).rstrip("/")
    body = json.dumps(payload).encode()
    request = Request(
        f"{endpoint}/v1/{operation}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=request_timeout_seconds) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise BrowserBrokerError("browser response exceeded byte budget")
            result = _decode_json(body)
    except HTTPError as error:
        payload = _decode_json(error.read(MAX_ERROR_BYTES))
        if error.code == 403 and payload.get("error") == "destination_refused":
            decision = DenylistResult(
                allowed=False,
                reason=str(payload.get("reason", "destination refused")),
                reason_code=str(payload.get("reason_code", "blocked_address")),
            )
            refusal = DestinationRefused(url, decision)
            refusal.browser_start_unattempted = (
                payload.get("browser_start_unattempted") is True
            )
            raise refusal from error
        message = str(payload.get("message") or "browser broker request failed")
        raise BrowserBrokerError(
            message,
            code=payload.get("error", "broker_unavailable"),
            retry_after=payload.get("retry_after_seconds"),
            cleanup=payload.get("cleanup"),
            browser_start_unattempted=payload.get("browser_start_unattempted") is True,
        ) from error
    except (URLError, TimeoutError) as error:
        raise BrowserBrokerError("browser broker is unavailable") from error

    if result.get("status") != "ok" or not isinstance(result.get("analysis"), dict):
        raise BrowserBrokerError("browser broker returned an invalid response")
    return result["analysis"]
